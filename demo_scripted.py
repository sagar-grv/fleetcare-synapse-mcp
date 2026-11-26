"""Deterministic MCP client run (NO LLM, no API key): drives the same MCP server the Groq agent uses.

Shows the whole flow incl. the server-side guardrails, and asserts on DB state. Optionally saves
dashboard screenshots after each stage:  python demo_scripted.py --shots out_dir
"""
import asyncio, json, os, socket, subprocess, sys, tempfile, time, pathlib, requests
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = pathlib.Path(__file__).resolve().parent
CHROME = os.environ.get("CHROME", "google-chrome")


def shot(url, path):
    url = url
    subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--window-size=1280,1250",
                    "--virtual-time-budget=4000", f"--screenshot={path}", url], capture_output=True, timeout=60)


async def main(shots=None):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    db = tempfile.mktemp(suffix=".db"); base = f"http://127.0.0.1:{port}"
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT,
                         env={**os.environ, "FLEETCARE_DB": db}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try: requests.get(base + "/fleet/summary"); break
        except Exception: time.sleep(0.2)
    log, stage = [], 0
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "synapse_auto_server.py")], env={**os.environ, "ENDPOINT_BASE_URL": base})

    def snap(name, caption=""):
        if shots:
            import urllib.parse
            pathlib.Path(shots).mkdir(parents=True, exist_ok=True)
            shot(base + "/?caption=" + urllib.parse.quote(caption), f"{shots}/{name}.png")

    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as sess:
                await sess.initialize()

                async def call(tool, args=None, stage_name=""):
                    res = await sess.call_tool(tool, args or {})
                    blocks = [c.text for c in res.content if hasattr(c, "text")]  # one block per list element
                    out = blocks[0] if len(blocks) == 1 else "[" + ",".join(blocks) + "]"
                    entry = {"stage": stage_name, "tool": tool, "args": args or {}, "error": bool(res.isError), "result": out[:300]}
                    log.append(entry)
                    print(("ERR " if res.isError else "ok  ") + f"{tool}({json.dumps(args or {})}) -> {out[:110]}")
                    return res.isError, out

                snap("0_start", "Synthetic demo data. Before: 5 open alerts. Each shows the recommended capable center; the critical engine alert has no engine center in its own region, so a human must review it.")
                # Task 1: triage every open alert, most severe first, using the server's recommendation
                await call("list_alerts", {"status": "open"}, "triage")
                order = [3, 1, 5, 4, 2]  # critical, high, medium, medium, low
                for aid in order:
                    _, rec = await call("recommend_service_center", {"alert_id": aid}, "triage")
                    rec = json.loads(rec)["recommended"]
                    await call("create_work_order", {"alert_id": aid, "service_center_id": rec["service_center_id"], "summary": f"Draft work order for alert {aid}",
                                                     "needs_human_review": rec["needs_human_review"], "review_reason": rec["review_reason"]}, "triage")
                snap("1_triage", "Agent drafted 5 work orders through the MCP tools. Only the Bengaluru engine case is flagged for human review. Drafts only, nothing dispatched.")
                # Guardrails: wrong specialty, duplicate, closed alert
                e1, _ = await call("create_work_order", {"alert_id": 1, "service_center_id": 4, "summary": "wrong center"}, "guardrails")
                e2, _ = await call("create_work_order", {"alert_id": 3, "service_center_id": 2, "summary": "duplicate"}, "guardrails")
                e3, _ = await call("create_work_order", {"alert_id": 6, "service_center_id": 4, "summary": "closed alert"}, "guardrails")
                assert e1 and e2 and e3, "guardrails must reject all three"
                snap("2_guardrails", "Bad calls refused by the backend and logged: wrong specialty (422), duplicate work order (409), closed alert (409).")
                # Task 2: one draft PO for low-stock parts
                _, parts = await call("list_parts", {"low_stock_only": True}, "reorder")
                low = json.loads(parts)
                lines = [{"part_id": x["id"], "quantity": x["reorder_qty"]} for x in (low if isinstance(low, list) else [low])]
                await call("create_purchase_order", {"lines": lines, "note": "Restock parts at or below reorder point"}, "reorder")
                e4, _ = await call("create_purchase_order", {"lines": [{"part_id": 1, "quantity": 3}]}, "guardrails")
                assert e4, "duplicate PO line must be rejected"
                snap("3_reorder", "One draft purchase order for the 3 low-stock parts: Rs 61,400. A repeated part is refused. Not sent to any supplier.")
                # Task 3: summary (read-only)
                await call("fleet_summary", {}, "summary")
        wos = requests.get(base + "/work-orders").json(); pos = requests.get(base + "/purchase-orders").json()
        assert {w["alert_id"] for w in wos} == {1, 2, 3, 4, 5} and all(w["status"] == "draft" for w in wos)
        assert [w["alert_id"] for w in wos if w["needs_human_review"]] == [3]
        assert len(pos) == 1 and pos[0]["total"] == 61400.0 and pos[0]["status"] == "draft"
        print("ALL ASSERTIONS PASSED: 5 draft work orders (1 flagged for review), 1 draft PO Rs 61,400, 4 guardrail rejections")
        if shots:
            pathlib.Path(shots, "log.json").write_text(json.dumps(log, indent=1))
    finally:
        p.terminate()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[sys.argv.index("--shots") + 1] if "--shots" in sys.argv else None))
