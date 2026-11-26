"""Exercise the Synapse-generated MCP server over real stdio MCP against a live FleetCare API."""
import asyncio, json, os, socket, subprocess, sys, time, pathlib, tempfile
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def api():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    db = tempfile.mktemp(suffix=".db")
    env = {**os.environ, "FLEETCARE_DB": db}
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import httpx
    for _ in range(50):
        try:
            httpx.get(f"http://127.0.0.1:{port}/fleet/summary"); break
        except Exception:
            time.sleep(0.2)
    yield f"http://127.0.0.1:{port}"
    p.terminate()


def call(base, tool, args=None):
    async def go():
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "synapse_auto_server.py")],
                                       env={**os.environ, "ENDPOINT_BASE_URL": base})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                res = await sess.call_tool(tool, args or {})
                return res
    res = asyncio.run(go())
    parts = [c.text for c in res.content if hasattr(c, "text")]
    vals = []
    for t in parts:
        try:
            vals.append(json.loads(t))
        except ValueError:
            vals.append(t)
    # FastMCP returns one content block per list element
    out = vals[0] if len(vals) == 1 and not isinstance(vals[0], dict) is False else vals
    if len(vals) == 1 and isinstance(vals[0], dict):
        out = vals[0]
    if isinstance(out, dict) and out.get("id") is not None and "vehicle_id" in out:
        out = [out]
    return res.isError, out


def test_lists_ten_tools(api):
    async def go():
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "synapse_auto_server.py")],
                                       env={**os.environ, "ENDPOINT_BASE_URL": api})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                return [t.name for t in (await sess.list_tools()).tools]
    names = asyncio.run(go())
    assert len(names) == 11 and "recommend_service_center" in names and "create_work_order" in names


def test_alert_filters(api):
    err, out = call(api, "list_alerts", {"status": "open", "severity": "critical"})
    assert not err and len(out) == 1 and out[0]["code"] == "COOLANT_TEMP"


def test_low_stock(api):
    err, out = call(api, "list_parts", {"low_stock_only": True})
    assert not err and {p["name"] for p in out} == {"Brake pad set (front)", "Coolant 5L", "Battery 12V 65Ah"}


def test_work_order_draft_and_validation(api):
    err, out = call(api, "create_work_order", {"alert_id": 3, "service_center_id": 2, "summary": "Coolant spikes", "needs_human_review": False})
    assert not err and out["status"] == "draft"
    assert out["needs_human_review"] is True and "Mumbai" in out["review_reason"]  # server forces the flag
    err, out = call(api, "create_work_order", {"alert_id": 999, "service_center_id": 4, "summary": "x"})
    assert err  # 404 surfaces as tool error


def test_purchase_order_total(api):
    err, out = call(api, "create_purchase_order", {"lines": [{"part_id": 1, "quantity": 12}, {"part_id": 3, "quantity": 16}]})
    assert not err and out["total"] == 12 * 1450.0 + 16 * 650.0
    err, out = call(api, "create_purchase_order", {"lines": [{"part_id": 1, "quantity": 0}]})
    assert err


def test_guardrails(api):
    # center 4 has no brakes specialty: rejected, and the reason is visible to the agent
    err, out = call(api, "create_work_order", {"alert_id": 1, "service_center_id": 4, "summary": "x"})
    assert err
    res = asyncio.run(_raw(api, "create_work_order", {"alert_id": 1, "service_center_id": 4, "summary": "x"}))
    assert "lacks the needed specialty" in res
    # duplicate for alert 3 (created in an earlier test) is rejected
    err, out = call(api, "create_work_order", {"alert_id": 3, "service_center_id": 2, "summary": "again"})
    assert err
    # closed alert is rejected
    err, out = call(api, "create_work_order", {"alert_id": 6, "service_center_id": 4, "summary": "closed"})
    assert err


def test_recommendation(api):
    err, out = call(api, "recommend_service_center", {"alert_id": 5})
    assert not err and out["recommended"]["service_center_id"] == 3 and out["recommended"]["needs_human_review"] is False
    err, out = call(api, "recommend_service_center", {"alert_id": 3})
    assert out["recommended"]["service_center_id"] == 2 and out["recommended"]["needs_human_review"] is True


def test_duplicate_po_part_rejected(api):
    err, out = call(api, "create_purchase_order", {"lines": [{"part_id": 1, "quantity": 5}]})
    assert err  # part 1 is already on the draft PO from test_purchase_order_total
    err, out = call(api, "create_purchase_order", {"lines": [{"part_id": 5, "quantity": 2}, {"part_id": 5, "quantity": 2}]})
    assert err


def test_dashboard_served(api):
    import httpx
    r = httpx.get(api + "/")
    assert r.status_code == 200 and "FleetCare" in r.text
    d = httpx.get(api + "/dashboard/data").json()
    assert {"summary", "alerts", "work_orders", "parts", "purchase_orders"} <= set(d)


def test_omitted_optional_fields_ok(api):
    # an agent that leaves out the optional flag must not hit a type error (regression: None was sent as null)
    err, out = call(api, "create_work_order", {"alert_id": 4, "service_center_id": 1, "summary": "tyres"})
    assert not err and out["needs_human_review"] is False


async def _raw(base, tool, args):
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "synapse_auto_server.py")], env={**os.environ, "ENDPOINT_BASE_URL": base})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            res = await sess.call_tool(tool, args)
            return "".join(c.text for c in res.content if hasattr(c, "text"))
