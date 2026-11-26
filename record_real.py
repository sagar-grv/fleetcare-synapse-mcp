"""Record the live dashboard while the REAL Groq agent works through the MCP server (no scripted steps).
Usage: GROQ_API_KEY=... python record_real.py OUT_DIR   (needs playwright + google-chrome)"""
import asyncio, json, os, socket, subprocess, sys, tempfile, time, pathlib, threading, requests
from playwright.sync_api import sync_playwright
from agent import run_task
from run_demo import TASKS

out = pathlib.Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
db = tempfile.mktemp(suffix=".db"); base = f"http://127.0.0.1:{port}"
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=os.path.dirname(os.path.abspath(__file__)),
                       env={**os.environ, "FLEETCARE_DB": db}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(50):
    try: requests.get(base + "/fleet/summary"); break
    except Exception: time.sleep(0.2)
ONLY = os.environ.get("ONLY_TASKS")  # e.g. task2_parts_reorder,task3_fleet_summary: start from the post-triage state
if ONLY:
    for aid in (3, 1, 5, 4, 2):
        rec = requests.get(f"{base}/alerts/{aid}/recommendation").json()["recommended"]
        requests.post(base + "/work-orders", json={"alert_id": aid, "service_center_id": rec["service_center_id"], "summary": f"Draft work order for alert {aid}",
                      "needs_human_review": rec["needs_human_review"], "review_reason": rec["review_reason"]})
    import sqlite3; _c = sqlite3.connect(db); _c.execute("DELETE FROM activity"); _c.commit(); _c.close()
events, T0 = [], None
def ev(name): events.append({"t": round(time.time() - T0, 2), "event": name}); print(events[-1], flush=True)

def agent_thread():
    key = os.environ["GROQ_API_KEY"]
    time.sleep(6)  # let the viewer see the starting state
    for name, prompt in TASKS.items():
        if ONLY and name not in ONLY.split(","): continue
        ev("start " + name)
        final, tr = asyncio.run(run_task(prompt, base, key))
        ev("done " + name)
        (out / f"{name}.json").write_text(json.dumps({"prompt": prompt, "final": final, "steps": tr}, indent=1))
        time.sleep(4)
    ev("all done")

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1920, "height": 1080}, record_video_dir=str(out), record_video_size={"width": 1920, "height": 1080})
    pg = ctx.new_page(); T0 = time.time()
    pg.goto(base + "/"); ev("page loaded")
    t = threading.Thread(target=agent_thread); t.start()
    t.join(timeout=900)
    time.sleep(6); ev("end")
    pathlib.Path(out / "events.json").write_text(json.dumps(events, indent=1))
    pg.screenshot(path=str(out / "final.png"))
    ctx.close(); b.close()
srv.terminate()
