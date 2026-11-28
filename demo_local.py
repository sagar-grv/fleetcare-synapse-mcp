"""Local no-model demo driver (NOT a model run): rules baseline through the guarded adapter, against ONE shared app + guard log,
so viewer.py shows the same state the run produced. Usage: python demo_local.py [workdir]"""
import json, os, pathlib, socket, subprocess, sys, tempfile, time
import httpx
ROOT = pathlib.Path(__file__).resolve().parent
wd = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()); wd.mkdir(exist_ok=True)
(wd / "fleet.db").unlink(missing_ok=True); (wd / "guard.jsonl").unlink(missing_ok=True)
def port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
ap, vp = port(), port()
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(ap)], cwd=ROOT,
                       env={**os.environ, "FLEETCARE_DB": str(wd / "fleet.db")}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try: httpx.get(f"http://127.0.0.1:{ap}/fleet/summary"); break
    except Exception: time.sleep(.2)
os.environ.update(ENDPOINT_BASE_URL=f"http://127.0.0.1:{ap}", GUARD_LOG=str(wd / "guard.jsonl"), GUARD_CAPS="read,write_work_order,write_po")
sys.path.insert(0, str(ROOT)); import guarded_server as m
for a in m.gen.list_alerts():
    if a["status"] != "open": continue
    r = m.recommend(a["id"])
    if not r.get("center"): m._log("rejected", code="no_center", reason=r["review_reason"], alert_id=a["id"]); continue
    m.create_wo(a["id"], r["center"]["id"], f"{a['code']}: {a['description']}", r["needs_human_review"], r["review_reason"])
low = [p for p in m.gen.list_parts() if p["stock"] <= p["reorder_point"]]
m.create_po([{"part_id": p["id"], "quantity": p["reorder_qty"]} for p in low], note="Auto restock draft")
# deliberate repeat attempts, to show the adapter refusing duplicates
m.create_wo(1, 2, "duplicate attempt", False, None)
m.create_po([{"part_id": low[0]["id"], "quantity": 1}], note="duplicate attempt")
print(json.dumps({"summary": m.gen.fleet_summary(), "work_orders": m.gen.list_work_orders(), "purchase_orders": m.gen.list_purchase_orders()}, indent=1, default=str))
env = {**os.environ, "VIEWER_LABEL": "NO-MODEL rules baseline, fresh local demo run (not an LLM result)"}
v = subprocess.Popen([sys.executable, "viewer.py", str(vp)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2)
print("VIEWER", vp, flush=True)
if os.environ.get("SHOT"):
    subprocess.run(["google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=1250,1000", f"--screenshot={os.environ['SHOT']}", f"http://127.0.0.1:{vp}"], capture_output=True)
v.terminate(); srv.terminate()
