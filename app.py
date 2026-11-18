"""Sample 'shopping app'. Its behaviour is controlled by env vars so we can
simulate good and bad releases without writing many apps."""
import os, sys, time
from flask import Flask, jsonify

VERSION = os.getenv("VERSION", "1.0")
MODE = os.getenv("MODE", "good")          # good | crash | login500 | slow | schema | latent
ADD_FIELD = os.getenv("ADD_FIELD") == "1"  # harmless additive change (new field)
FAIL_AFTER = int(os.getenv("FAIL_AFTER", "15"))
START = time.time()

if MODE == "crash":
    print("FATAL: cannot connect to database at startup", flush=True)
    sys.exit(1)

app = Flask(__name__)

@app.after_request
def tag(resp):
    resp.headers["X-App-Version"] = VERSION
    return resp

def guard(name):
    if MODE == "slow":
        time.sleep(1.5)
    if MODE == "latent" and time.time() - START > FAIL_AFTER:
        return jsonify(error="internal error"), 500
    if MODE == "login500" and name == "login":
        return jsonify(error="db connection failed"), 500
    return None

@app.get("/health")
def health():
    return jsonify(status="ok", version=VERSION)

@app.get("/api/products")
def products():
    bad = guard("products")
    if bad: return bad
    price = "cost" if MODE == "schema" else "price"   # breaking rename in 'schema' mode
    items = [{"id": 1, "name": "Laptop", price: 55000}, {"id": 2, "name": "Phone", price: 20000}]
    if ADD_FIELD:
        for i in items: i["rating"] = 4.5
    return jsonify(items)

@app.get("/api/login")
def login():
    bad = guard("login")
    if bad: return bad
    return jsonify(token="abc123", user="demo")

@app.get("/api/cart")
def cart():
    bad = guard("cart")
    if bad: return bad
    return jsonify(items=[{"id": 1, "qty": 2}], total=110000)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), threaded=True)
