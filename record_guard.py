"""Record the live dashboard while deliberate bad calls are sent through the MCP tools (guardrails), then a purchase order.
These calls are scripted on purpose (no LLM) so the refusals are guaranteed to show. Usage: python record_guard.py OUT_DIR"""
import asyncio, json, os, socket, subprocess, sys, tempfile, time, pathlib, requests
from playwright.sync_api import sync_playwright
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = pathlib.Path(__file__).resolve().parent
out = pathlib.Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
db = tempfile.mktemp(suffix=".db"); base = f"http://127.0.0.1:{port}"
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT, env={**os.environ, "FLEETCARE_DB": db},
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(50):
    try: requests.get(base + "/fleet/summary"); break
    except Exception: time.sleep(0.2)
# pre-state: the 5 draft work orders the agent created in the previous scene
for aid in (3, 1, 5, 4, 2):
    rec = requests.get(f"{base}/alerts/{aid}/recommendation").json()["recommended"]
    requests.post(base + "/work-orders", json={"alert_id": aid, "service_center_id": rec["service_center_id"], "summary": f"Draft work order for alert {aid}",
                  "needs_human_review": rec["needs_human_review"], "review_reason": rec["review_reason"]})
requests.delete  # no-op; clear activity so the feed starts empty
import sqlite3; c = sqlite3.connect(db); c.execute("DELETE FROM activity"); c.commit(); c.close()
events, T0 = [], None
def ev(n): events.append({"t": round(time.time() - T0, 2), "event": n}); print(events[-1], flush=True)

async def drive():
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "synapse_auto_server.py")], env={**os.environ, "ENDPOINT_BASE_URL": base})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            async def call(tool, args, name):
                ev("call " + name); await sess.call_tool(tool, args); await asyncio.sleep(3.2)
            await asyncio.sleep(3)
            await call("create_work_order", {"alert_id": 1, "service_center_id": 4, "summary": "Brake service"}, "wrong specialty")
            await call("create_work_order", {"alert_id": 3, "service_center_id": 2, "summary": "Engine again"}, "duplicate")
            await call("create_work_order", {"alert_id": 6, "service_center_id": 4, "summary": "Air filter"}, "closed alert")
            await asyncio.sleep(2)
            ev("restock")
            await call("list_parts", {"low_stock_only": True}, "low stock")
            await call("create_purchase_order", {"lines": [{"part_id": 1, "quantity": 12}, {"part_id": 3, "quantity": 16}, {"part_id": 5, "quantity": 6}], "note": "Restock parts at or below reorder point"}, "purchase order")
            await call("create_purchase_order", {"lines": [{"part_id": 1, "quantity": 3}]}, "repeat part")
            await asyncio.sleep(3)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1920, "height": 1080}, record_video_dir=str(out), record_video_size={"width": 1920, "height": 1080})
    pg = ctx.new_page(); T0 = time.time(); pg.goto(base + "/"); ev("loaded")
    import threading; th = threading.Thread(target=lambda: asyncio.run(drive())); th.start(); th.join(); ev("end")
    pathlib.Path(out / "events.json").write_text(json.dumps(events, indent=1))
    pg.screenshot(path=str(out / "final.png")); ctx.close(); b.close()
srv.terminate()
