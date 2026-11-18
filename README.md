# DeployGuard
Cloud-native safe deployment platform. Every new version runs in its own Docker container, is **compared against the live version**, goes live only if safe, and is **rolled back automatically** if it fails after going live. Deployment requests flow through a **RabbitMQ** queue.

## Run it (Windows PowerShell, one action per ENTER)
1. Install and start **Docker Desktop** (Linux containers, the default).
2. Unzip, open PowerShell in the `deployguard` folder (the one containing `docker-compose.yml` and `demo.ps1`).
3. Run:
```
powershell -ExecutionPolicy Bypass -File .\demo.ps1
```
The script pauses before every step. Press ENTER and it runs ONE action and shows the result (compose up, deploy v1.0, traffic, good release, crash, broken login, slow, breaking change, auto-rollback, RabbitMQ proof, history, cleanup).
Open a second PowerShell window and run `docker ps` during the demo: `dg-*` containers appear and disappear in real time.

Manual alternative: `docker compose up --build`, then open http://localhost:8000 and click the scenarios.
RabbitMQ console: http://localhost:15672 (guest / guest). Stop everything: `docker compose down`.

## Novelty
Argo Rollouts, Flagger and Spinnaker already do blue-green, canary and rollback. We do NOT claim those are new. Our contribution:
1. **Zero-test differential verification**: no test suite needed. The same requests are replayed against the live and the new version; status, JSON structure and latency are compared with the live baseline.
2. **Breaking vs additive change detection**: a removed or retyped field blocks the release; a new field passes.
   Example: v2.4 returns HTTP 200 everywhere but renames `price` to `cost`. A plain health check approves it, DeployGuard blocks it.
3. **Explainable risk score** (0-100), plain-English verdict and per-endpoint table.
4. **Built-in failure simulator**: 7 reproducible release scenarios (good, crash, broken login, slow, breaking API change, fails after going live).
5. Lightweight: Docker + Compose, no Kubernetes or GitOps setup.

## Architecture
Dashboard / PowerShell -> DeployGuard API (FastAPI) -> **RabbitMQ queue `deploy_jobs`** -> Deployment worker (one validation at a time) -> Docker Engine (via Docker SDK) -> containers `live` / `candidate` / `standby` on network `deployguard_net`.
Inside the API: Verification Engine (differential comparison) · Decision Engine (risk score, threshold 50) · Traffic Gateway (`/live/*`, switches versions instantly) · Post-deploy Monitor (auto-rollback).

## Cloud-native principles shown
Containerization and immutable versions, microservice-style separation (broker, API, app containers), asynchronous processing (RabbitMQ), blue-green deployment, fault tolerance (a bad release never replaces a good one), self-healing (automatic rollback), service discovery (container DNS on a user-defined network), observability (dashboard, event log, history), resource limits (128 MB, 0.5 CPU per app container), automation.

## Limitations (say these before they are asked)
- The traffic gateway is built into the API instead of Nginx/Envoy, to make switching instant and simple.
- History is in memory (resets on restart). No authentication.
- Probes are GET endpoints. The sample app simulates good/bad releases through environment variables.
- The API mounts `/var/run/docker.sock`, which is powerful: run locally with trusted images only.
- If RabbitMQ is unreachable the API falls back to running deployments directly (the dashboard badge turns yellow).
- Future work: Kubernetes, Prometheus/Grafana, PostgreSQL history, canary percentages, POST probes.

## Files
`demo.ps1` (step-by-step demo) · `docker-compose.yml` (RabbitMQ + API + sample image) · `demo-app/` (sample shopping app) · `deployguard/main.py` (all logic) · `deployguard/static/index.html` (dashboard)
Config in compose: `RISK_THRESHOLD` (default 50), `MONITOR_SECONDS` (default 30).

## 5-minute faculty script
1. Run `demo.ps1`, explain the idea at step 1-4 (platform + RabbitMQ containers visible in `docker ps`).
2. v1.0 live, then good v2.0 approved (new field = safe change).
3. Crash, broken login, slow: all blocked, v2.0 never touched.
4. **v2.4 breaking change**: HTTP 200 everywhere, still blocked. This is the novelty slide.
5. v2.5: goes live, fails 15 s later, rolled back automatically while traffic keeps flowing.
6. Show the RabbitMQ queue proof and the dashboard at http://localhost:8000.
