"""Run one case file against the UNTOUCHED original app through the guarded adapter.

Usage: python run_case.py case.json [--variant rules]
Case file: {"id", "task": "triage|restock|briefing", "instruction": "...",
            "targets": {"alert_ids": [..], "part_ids": [..]},   # optional; omitted = all
            "fixture_db": "path or null"}                       # null = fresh seed (reset)
Output JSON: calls, rejections (+reasons), drafts, wall_clock_s, llm_calls, tokens.
Variant "rules" is a no-LLM baseline using the same adapter/API access.
"""
import json, os, shutil, socket, subprocess, sys, tempfile, time, pathlib, importlib
import httpx

ROOT = pathlib.Path(__file__).resolve().parent
case = json.load(open(sys.argv[1]))
tmp = pathlib.Path(tempfile.mkdtemp())
db = tmp / "fleet.db"
if case.get("fixture_db"):
    shutil.copy(case["fixture_db"], db)
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
env = {**os.environ, "FLEETCARE_DB": str(db)}
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try: httpx.get(f"http://127.0.0.1:{port}/fleet/summary"); break
    except Exception: time.sleep(.2)
os.environ["ENDPOINT_BASE_URL"] = f"http://127.0.0.1:{port}"
os.environ["GUARD_LOG"] = str(tmp / "guard.jsonl")
sys.path.insert(0, str(ROOT))
from intent import resolve
CAPS, caps_note = resolve(mode=case.get("mode"), confirmed=bool(case.get("confirmed")), allowed_actions=case.get("allowed_actions"))
os.environ["GUARD_CAPS"] = CAPS   # absent allowed_actions = read-only
sys.path.insert(0, str(ROOT))
m = importlib.import_module("guarded_server")

calls = []
for name in ("list_vehicles", "get_vehicle", "list_alerts", "list_parts", "list_service_centers", "list_work_orders",
             "list_purchase_orders", "fleet_summary", "create_work_order", "create_purchase_order"):
    f = getattr(m.gen, name)
    def wrap(*a, _f=f, _n=name, **k):
        calls.append(_n); return _f(*a, **k)
    setattr(m.gen, name, wrap)

t0 = time.time()
tg = case.get("targets") or {}
result = {}
if case["task"] == "triage":
    done = {w["alert_id"] for w in m.gen.list_work_orders()}
    for a in m.gen.list_alerts():
        if a["status"] != "open" or a["id"] in done: continue
        if tg.get("alert_ids") and a["id"] not in tg["alert_ids"]: continue
        r = m.recommend(a["id"])
        if not r.get("center"):
            m._log("rejected", code="no_center", reason=r["review_reason"], alert_id=a["id"]); continue
        m.create_wo(a["id"], r["center"]["id"], f"{a['code']}: {a['description']}", r["needs_human_review"], r["review_reason"])
elif case["task"] == "restock":
    low = [p for p in m.gen.list_parts() if p["stock"] <= p["reorder_point"]]
    if tg.get("part_ids"): low = [p for p in low if p["id"] in tg["part_ids"]]
    if low:
        m.create_po([{"part_id": p["id"], "quantity": p["reorder_qty"]} for p in low], note="Auto restock draft")
elif case["task"] == "briefing":
    s_ = m.gen.fleet_summary(); low = [p["name"] for p in m.gen.list_parts() if p["stock"] <= p["reorder_point"]]
    result["briefing"] = {"fleet_summary": s_, "low_stock_parts": low}
wall = round(time.time() - t0, 3)
log = [json.loads(l) for l in open(tmp / "guard.jsonl")] if (tmp / "guard.jsonl").exists() else []
out = {"id": case["id"], "variant": "rules", "task": case["task"], "instruction": case.get("instruction"),
       "tool_calls": calls, "n_calls": len(calls), "rejections": [l for l in log if l["kind"] == "rejected"], "skips": [l for l in log if l["kind"] == "skipped"],
       "work_orders": m.gen.list_work_orders(), "purchase_orders": m.gen.list_purchase_orders(),
       "capabilities": CAPS, "capabilities_source": caps_note, "llm_calls": 0, "tokens": 0, "wall_clock_s": wall, **result}
print(json.dumps(out, indent=1, default=str))
srv.terminate(); shutil.rmtree(tmp, ignore_errors=True)
