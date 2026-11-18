"""DeployGuard: safe deployment with *differential behaviour verification*,
explainable risk scoring, blue-green switching and automatic rollback."""
import os, re, json, time, uuid, threading, statistics, collections
from concurrent.futures import ThreadPoolExecutor

import docker
import httpx
try:
    import pika
except Exception:      # RabbitMQ is optional: falls back to direct execution
    pika = None
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse

NETWORK = os.getenv("NETWORK_NAME", "deployguard_net")
DEMO_IMAGE = os.getenv("DEMO_IMAGE", "deployguard-demo-app:latest")
RISK_THRESHOLD = int(os.getenv("RISK_THRESHOLD", "50"))
MONITOR_SECONDS = int(os.getenv("MONITOR_SECONDS", "30"))
RABBIT_URL = os.getenv("RABBIT_URL", "amqp://guest:guest@rabbitmq:5672/%2F?heartbeat=0&connection_attempts=3&retry_delay=1")
QUEUE = "deploy_jobs"
LABEL = "deployguard.managed"
DEFAULT_PROBES = ["/api/products", "/api/login", "/api/cart"]
HERE = os.path.dirname(__file__)

_client = None
def dc():
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client

SCENARIOS = {
    "v1_stable": {"label": "Deploy v1.0 (stable baseline)", "version": "1.0",
                  "desc": "First release. Becomes live.", "env": {"VERSION": "1.0", "MODE": "good"}},
    "v2_good": {"label": "v2.0 - good release (adds a field)", "version": "2.0",
                "desc": "Safe additive change. Expected: approved + switched.",
                "env": {"VERSION": "2.0", "MODE": "good", "ADD_FIELD": "1"}},
    "crash": {"label": "v2.1 - crashes on startup", "version": "2.1-crash",
              "desc": "Container exits. Expected: blocked.", "env": {"VERSION": "2.1", "MODE": "crash", "ADD_FIELD": "1"}},
    "login500": {"label": "v2.2 - login API broken", "version": "2.2-login-bug",
                 "desc": "App starts, /api/login returns 500. Expected: blocked.",
                 "env": {"VERSION": "2.2", "MODE": "login500", "ADD_FIELD": "1"}},
    "slow": {"label": "v2.3 - slow responses", "version": "2.3-slow",
             "desc": "Works but 1.5s per call. Expected: blocked.", "env": {"VERSION": "2.3", "MODE": "slow", "ADD_FIELD": "1"}},
    "schema": {"label": "v2.4 - breaking API change", "version": "2.4-schema",
               "desc": "Status 200 but 'price' renamed to 'cost'. A plain health check would miss it. Expected: blocked.",
               "env": {"VERSION": "2.4", "MODE": "schema", "ADD_FIELD": "1"}},
    "latent": {"label": "v2.5 - fails AFTER going live", "version": "2.5-latent",
               "desc": "Passes validation, breaks ~15s later. Expected: switched, then auto-rollback.",
               "env": {"VERSION": "2.5", "MODE": "latent", "FAIL_AFTER": "15", "ADD_FIELD": "1"}},
}

app = FastAPI(title="DeployGuard")
state = {"active": None, "standby": None, "deployments": [], "busy": False, "monitoring": None}
traffic = collections.deque(maxlen=200)
queue_state = {"connected": False, "processed": 0, "url": "http://localhost:15672"}

# ---------------------------------------------------------------- helpers
def log(dep, msg, level="info"):
    dep["events"].append({"t": time.strftime("%H:%M:%S"), "msg": msg, "level": level})

def remove(info):
    if not info: return
    try:
        dc().containers.get(info["name"]).remove(force=True)
    except Exception:
        pass

def start_container(version, image, env, port):
    name = "dg-" + re.sub(r"[^a-z0-9-]", "-", version.lower()) + "-" + uuid.uuid4().hex[:4]
    dc().containers.run(image, detach=True, name=name, environment=env, network=NETWORK,
                        labels={LABEL: "true"}, mem_limit="128m", nano_cpus=500_000_000)
    return {"name": name, "version": version, "url": f"http://{name}:{port}"}

def wait_healthy(info, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        c = dc().containers.get(info["name"])
        c.reload()
        if c.status in ("exited", "dead"):
            tail = c.logs(tail=3).decode(errors="ignore").strip()
            return False, "container crashed on startup" + (f" ({tail})" if tail else "")
        try:
            if httpx.get(info["url"] + "/health", timeout=2).status_code == 200:
                return True, "healthy"
        except Exception:
            pass
        time.sleep(0.5)
    return False, "health check timed out"

def hit(url, path, n=3):
    out = []
    for _ in range(n):
        t = time.perf_counter()
        try:
            r = httpx.get(url + path, timeout=5)
            try: body = r.json()
            except Exception: body = None
            out.append({"status": r.status_code, "ms": (time.perf_counter() - t) * 1000, "body": body})
        except Exception:
            out.append({"status": 0, "ms": (time.perf_counter() - t) * 1000, "body": None})
    return out

def summarize(results):
    worst = next((r for r in results if r["status"] >= 500 or r["status"] == 0), results[-1])
    return {"status": worst["status"], "ms": statistics.median(r["ms"] for r in results),
            "body": worst["body"] if worst["status"] < 500 else None}

def shape(x):
    if isinstance(x, dict): return {k: shape(v) for k, v in x.items()}
    if isinstance(x, list): return [shape(x[0])] if x else []
    if isinstance(x, bool): return "bool"
    if isinstance(x, (int, float)): return "number"
    if x is None: return "null"
    return "string"

def diff_shapes(old, new, path=""):
    """Returns (breaking_changes, additive_changes). Removed fields / changed types are breaking."""
    breaking, added = [], []
    if isinstance(old, dict) and isinstance(new, dict):
        for k, v in old.items():
            if k not in new:
                breaking.append(f"field '{path}{k}' removed")
            else:
                b, a = diff_shapes(v, new[k], f"{path}{k}.")
                breaking += b; added += a
        added += [f"{path}{k}" for k in new if k not in old]
    elif isinstance(old, list) and isinstance(new, list):
        if old and new:
            b, a = diff_shapes(old[0], new[0], path)
            breaking += b; added += a
    elif type(old) != type(new):
        breaking.append(f"type of '{path.rstrip('.')}' changed")
    return breaking, added

def evaluate(cand, live, probes):
    """Differential verification: send identical requests to live and candidate, compare."""
    with ThreadPoolExecutor(max_workers=max(1, len(probes))) as ex:
        cres = list(ex.map(lambda p: summarize(hit(cand["url"], p)), probes))
        lres = list(ex.map(lambda p: summarize(hit(live["url"], p)), probes)) if live else [None] * len(probes)
    rows, score, reasons, notes = [], 0, [], []
    for p, c, l in zip(probes, cres, lres):
        row = {"path": p, "cand_status": c["status"], "cand_ms": round(c["ms"]),
               "live_status": l["status"] if l else None, "live_ms": round(l["ms"]) if l else None,
               "ok": True, "note": "same behaviour" if l else "baseline ok"}
        issues = []
        if c["status"] >= 500 or c["status"] == 0:
            issues.append((60, f"{p} returns HTTP {c['status'] or 'no response'} on the new version"))
        elif l and l["status"] != c["status"]:
            issues.append((40, f"{p} status changed {l['status']} -> {c['status']}"))
        elif l and l["body"] is not None and c["body"] is not None:
            br, ad = diff_shapes(shape(l["body"]), shape(c["body"]))
            if br:
                issues.append((50, f"{p} breaking response change: {', '.join(br[:3])}"))
            elif ad:
                row["note"] = "additive change, safe: new field(s) " + ", ".join(ad)
                notes.append(f"{p} gained {', '.join(ad)} (non-breaking)")
        limit = max(500, 3 * l["ms"]) if l else 1000
        if c["ms"] > limit:
            issues.append((50, f"{p} too slow: {round(c['ms'])} ms (limit {round(limit)} ms)"))
        if issues:
            row["ok"] = False
            row["note"] = "; ".join(w for _, w in issues)
            score += sum(pt for pt, _ in issues)
            reasons += [w for _, w in issues]
        rows.append(row)
    return rows, min(score, 100), reasons, notes

# ---------------------------------------------------------------- deployment workflow
def monitor(dep, cand, probes):
    end = time.time() + MONITOR_SECONDS
    bad_rounds = 0
    state["monitoring"] = dep
    dep["monitor"] = {"error_rate": 0, "remaining": MONITOR_SECONDS, "done": False}
    while time.time() < end and not dep.get("cancel"):
        time.sleep(2)
        if state["active"] is not cand:
            dep["stage"] = "done"
            return
        res = [summarize(hit(cand["url"], p, 1)) for p in probes]
        errs = sum(1 for r in res if r["status"] >= 500 or r["status"] == 0)
        dep["monitor"].update(error_rate=round(100 * errs / len(res)), remaining=max(0, int(end - time.time())))
        bad_rounds = bad_rounds + 1 if errs else 0
        if bad_rounds >= 2:
            prev = state["standby"]
            if prev:
                state["active"], state["standby"] = prev, None
                remove(cand)
                dep["status"] = "rolled_back"
                dep["verdict"] = f"Rolled back automatically: error rate {dep['monitor']['error_rate']}% after going live. Traffic is back on v{prev['version']}."
                log(dep, dep["verdict"], "error")
            else:
                log(dep, "Errors detected but no previous version to roll back to", "error")
            dep["stage"] = "done"
            return
    if not dep.get("cancel"):
        dep["monitor"]["done"] = True
        log(dep, f"Monitoring window ({MONITOR_SECONDS}s) passed. Version is stable.", "ok")
    dep["stage"] = "done"

def run_deploy(dep, image, env, port, probes, released=None):
    """Runs one deployment. `released` is set as soon as validation is finished (the queue
    worker can then take the next job while the post-deploy monitor keeps running)."""
    done = []
    def release():
        if done: return                  # only the first release counts (monitor may outlive validation)
        done.append(1)
        state["busy"] = False
        if released: released.set()
    cand = None
    try:
        prev_dep = state.get("monitoring")
        if prev_dep: prev_dep["cancel"] = True
        live = state["active"]
        log(dep, f"Starting v{dep['version']} in an isolated container. " +
                 (f"v{live['version']} keeps serving users." if live else "No live version yet."))
        dep["stage"] = "starting"
        cand = start_container(dep["version"], image, env, port)
        dep["stage"] = "health"
        ok, reason = wait_healthy(cand)
        if not ok:
            dep.update(status="blocked", score=100, stage="done", verdict=f"Blocked: {reason}")
            log(dep, dep["verdict"], "error")
            if live: log(dep, f"Traffic stays on v{live['version']}. No user impact.", "ok")
            remove(cand)
            return
        log(dep, "Container is healthy.", "ok")
        dep["stage"] = "differential"
        log(dep, ("Replaying identical requests against live and new version and comparing status, schema and latency..."
                  if live else "No live version to compare with: running baseline checks."))
        rows, score, reasons, notes = evaluate(cand, live, probes)
        dep["rows"], dep["score"] = rows, score
        if score >= RISK_THRESHOLD:
            dep.update(status="blocked", stage="done", verdict="Blocked: " + "; ".join(reasons))
            log(dep, f"Risk score {score}/100 >= threshold {RISK_THRESHOLD}. " + dep["verdict"], "error")
            if live: log(dep, f"Traffic stays on v{live['version']}. No user impact.", "ok")
            remove(cand)
            return
        dep["stage"] = "switch"
        old_standby = state["standby"]
        state["standby"], state["active"] = live, cand
        if old_standby: remove(old_standby)
        dep.update(status="live", verdict=f"Approved: risk score {score}/100. " + ("; ".join(notes) if notes else "Behaviour matches the live version."))
        log(dep, f"Risk score {score}/100. Traffic switched to v{dep['version']}." +
                 (f" v{live['version']} kept as rollback standby." if live else ""), "ok")
        dep["stage"] = "monitoring"
        release()
        monitor(dep, cand, probes)
    except Exception as e:
        dep.update(status="failed", stage="done", verdict=f"Internal error: {e}")
        log(dep, dep["verdict"], "error")
        remove(cand)
    finally:
        release()

# ---------------------------------------------------------------- RabbitMQ job queue
def publish_job(job):
    conn = pika.BlockingConnection(pika.URLParameters(RABBIT_URL))
    try:
        ch = conn.channel()
        ch.queue_declare(queue=QUEUE, durable=True)
        ch.basic_publish("", QUEUE, json.dumps(job), pika.BasicProperties(delivery_mode=2))
    finally:
        conn.close()

def on_job(ch, method, props, body):
    job = json.loads(body)
    dep = next((d for d in state["deployments"] if d["id"] == job["id"]), None)
    if dep is None:                      # stale job (e.g. demo was reset)
        ch.basic_ack(method.delivery_tag); return
    log(dep, "Worker picked the job up from RabbitMQ queue '%s'." % QUEUE)
    released = threading.Event()
    threading.Thread(target=run_deploy, args=(dep, job["image"], job["env"], job["port"], job["probes"], released),
                     daemon=True).start()
    released.wait(timeout=300)           # one validation at a time (prefetch=1)
    queue_state["processed"] += 1
    ch.basic_ack(method.delivery_tag)

def worker_loop():
    while True:
        try:
            conn = pika.BlockingConnection(pika.URLParameters(RABBIT_URL))
            ch = conn.channel()
            ch.queue_declare(queue=QUEUE, durable=True)
            ch.queue_purge(QUEUE)
            ch.basic_qos(prefetch_count=1)
            ch.basic_consume(QUEUE, on_job)
            queue_state["connected"] = True
            print("RabbitMQ worker connected", flush=True)
            ch.start_consuming()
        except Exception as e:
            print("RabbitMQ worker reconnecting:", e, flush=True)
        queue_state["connected"] = False
        time.sleep(3)

@app.post("/api/deploy")
def deploy(body: dict):
    if state["busy"]:
        raise HTTPException(409, "A deployment is already being validated")
    if body.get("scenario"):
        s = SCENARIOS.get(body["scenario"])
        if not s: raise HTTPException(400, "unknown scenario")
        version, env, image, port, probes = s["version"], s["env"], DEMO_IMAGE, 5000, DEFAULT_PROBES
    else:
        image, version = body.get("image"), body.get("version")
        if not image or not version: raise HTTPException(400, "image and version required")
        port = int(body.get("port") or 5000)
        probes = [p.strip() for p in (body.get("probes") or "/").split(",") if p.strip()]
        env = {}
    dep = {"id": uuid.uuid4().hex[:6], "version": version, "image": image, "status": "validating",
           "stage": "queued", "events": [], "score": None, "rows": [], "verdict": "",
           "started": time.strftime("%H:%M:%S"), "via": "direct"}
    state["busy"] = True
    state["deployments"].insert(0, dep)
    job = {"id": dep["id"], "image": image, "version": version, "env": env, "port": port, "probes": probes}
    if pika and queue_state["connected"]:
        try:
            publish_job(job)
            dep["via"] = "rabbitmq"
            log(dep, "Deployment job published to RabbitMQ queue '%s'." % QUEUE)
        except Exception as e:
            log(dep, "RabbitMQ publish failed (%s). Running directly." % e, "error")
    if dep["via"] == "direct":
        threading.Thread(target=run_deploy, args=(dep, image, env, port, probes), daemon=True).start()
    return {"id": dep["id"]}

@app.post("/api/reset")
def reset():
    for c in dc().containers.list(all=True, filters={"label": LABEL}):
        c.remove(force=True)
    state.update(active=None, standby=None, deployments=[], busy=False, monitoring=None)
    traffic.clear()
    return {"ok": True}

@app.get("/api/state")
def get_state():
    recent = list(traffic)[-30:]
    pub = lambda i: {"version": i["version"], "name": i["name"]} if i else None
    return {"active": pub(state["active"]), "standby": pub(state["standby"]), "busy": state["busy"],
            "threshold": RISK_THRESHOLD, "deployments": state["deployments"][:10],
            "scenarios": [{"key": k, **{x: v[x] for x in ("label", "desc")}} for k, v in SCENARIOS.items()],
            "queue": queue_state, "gateway": {"recent": [{"s": r["status"], "v": r["version"]} for r in recent]}}

# ---------------------------------------------------------------- gateway (traffic controller)
@app.api_route("/live/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def gateway(path: str, request: Request):
    act = state["active"]
    if not act:
        return Response('{"error":"no live version"}', status_code=503, media_type="application/json")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.request(request.method, f"{act['url']}/{path}", content=await request.body(),
                                params=request.query_params)
        status, body, ctype = r.status_code, r.content, r.headers.get("content-type", "application/json")
    except Exception:
        status, body, ctype = 502, b'{"error":"bad gateway"}', "application/json"
    traffic.append({"t": time.time(), "status": status, "version": act["version"]})
    return Response(body, status_code=status, media_type=ctype, headers={"X-App-Version": act["version"]})

@app.on_event("startup")
def cleanup():
    if pika:
        threading.Thread(target=worker_loop, daemon=True).start()
    try:
        for c in dc().containers.list(all=True, filters={"label": LABEL}):
            c.remove(force=True)
    except Exception as e:
        print("cleanup skipped:", e)

@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))
