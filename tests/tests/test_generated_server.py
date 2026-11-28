"""Contract tests for the UNEDITED Synapse-generated server (synapse_auto_server.generated.py) over real stdio MCP,
against the untouched app. They assert what the generated server actually does: 10 tools, the real names
(create_work_order, create_purchase_order), no filter parameters on the list tools (filtering is the adapter's job), and
backend 404/422 surfacing as tool errors. Replaces the old test_mcp_tools.py, which targeted the hand-edited server
of commit f1a98ba (not shipped) and expected filters that Auto output does not have. Needs Python 3.12 (the generated file's syntax)."""
import asyncio, json, os, socket, subprocess, sys, time, pathlib, tempfile
import httpx, pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = pathlib.Path(__file__).resolve().parent.parent
GEN = str(ROOT / "synapse_auto_server.generated.py")


@pytest.fixture(scope="module")
def api():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT,
                         env={**os.environ, "FLEETCARE_DB": tempfile.mktemp(suffix=".db")}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try: httpx.get(f"http://127.0.0.1:{port}/fleet/summary"); break
        except Exception: time.sleep(0.2)
    yield f"http://127.0.0.1:{port}"
    p.terminate()


def session_run(base, fn):
    async def go():
        params = StdioServerParameters(command=sys.executable, args=[GEN], env={**os.environ, "ENDPOINT_BASE_URL": base})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                return await fn(sess)
    return asyncio.run(go())


def call(base, tool, args=None):
    async def fn(sess):
        return await sess.call_tool(tool, args or {})
    res = session_run(base, fn)
    vals = []
    for c in res.content:
        if hasattr(c, "text"):
            try: vals.append(json.loads(c.text))
            except ValueError: vals.append(c.text)
    return res.isError, vals


def test_lists_ten_tools_with_generated_names(api):
    async def fn(sess): return (await sess.list_tools()).tools
    tools = session_run(api, fn)
    names = [t.name for t in tools]
    assert len(names) == 10 and "create_work_order" in names and "create_purchase_order" in names
    # the contract that makes the adapter necessary: list tools have no filter parameters
    for t in tools:
        if t.name in ("list_alerts", "list_parts", "list_vehicles", "list_service_centers"):
            assert not (t.inputSchema or {}).get("properties"), t.name


def test_list_alerts_returns_all_rows_critical_present(api):
    err, vals = call(api, "list_alerts")
    rows = vals if len(vals) != 1 or not isinstance(vals[0], list) else vals[0]
    assert not err and len(rows) == 6
    assert [r["code"] for r in rows if r["severity"] == "critical"] == ["COOLANT_TEMP"]


def test_list_parts_low_stock_subset(api):
    err, vals = call(api, "list_parts")
    rows = vals if len(vals) != 1 or not isinstance(vals[0], list) else vals[0]
    assert not err and len(rows) == 6
    assert {p["name"] for p in rows if p["stock"] <= p["reorder_point"]} == {"Brake pad set (front)", "Coolant 5L", "Battery 12V 65Ah"}


def test_work_order_draft_and_validation(api):
    err, vals = call(api, "create_work_order", {"alert_id": 3, "service_center_id": 4, "summary": "Coolant spikes", "needs_human_review": True, "review_reason": "no engine center in region"})
    assert not err and vals[0]["status"] == "draft"
    err, vals = call(api, "create_work_order", {"alert_id": 999, "service_center_id": 4, "summary": "x"})
    assert err  # 404 surfaces as a tool error


def test_purchase_order_total(api):
    err, vals = call(api, "create_purchase_order", {"lines": [{"part_id": 1, "quantity": 12}, {"part_id": 3, "quantity": 16}]})
    assert not err and vals[0]["total"] == 12 * 1450.0 + 16 * 650.0
    err, vals = call(api, "create_purchase_order", {"lines": [{"part_id": 1, "quantity": 0}]})
    assert err
