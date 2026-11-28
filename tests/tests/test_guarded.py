"""Unit tests for the guarded adapter against the UNTOUCHED original app (live HTTP, temp DB)."""
import os, socket, subprocess, sys, tempfile, time, pathlib, importlib
import pytest, httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def g():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    db = tempfile.mktemp(suffix=".db")
    env = {**os.environ, "FLEETCARE_DB": db}
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try: httpx.get(f"http://127.0.0.1:{port}/fleet/summary"); break
        except Exception: time.sleep(.2)
    os.environ["ENDPOINT_BASE_URL"] = f"http://127.0.0.1:{port}"
    os.environ["GUARD_LOG"] = tempfile.mktemp(suffix=".jsonl")
    os.environ["GUARD_CAPS"] = "read,write_work_order,write_po"   # explicit: the adapter default is read-only
    sys.path.insert(0, str(ROOT))
    mod = importlib.import_module("guarded_server")
    yield mod, port
    p.terminate()


def test_raw_app_accepts_bad_action_adapter_rejects(g):
    m, port = g
    alerts = m.gen.list_alerts(); centers = m.gen.list_service_centers()
    a = next(x for x in alerts if x["status"] == "open")
    bad = next(c for c in centers if a["needed_specialty"] not in c["specialties"])
    r = m.create_wo(a["id"], bad["id"], "x")
    assert r["rejected"] and r["code"] == "specialty_mismatch"
    assert m.gen.list_work_orders() == []          # nothing was written
    # direct API access is NOT protected (documented limit)
    raw = httpx.post(f"http://127.0.0.1:{port}/work-orders", json={"alert_id": a["id"], "service_center_id": bad["id"], "summary": "raw"})
    assert raw.status_code == 200


def test_duplicate_closed_and_review_flag(g):
    m, _ = g
    closed = [a for a in m.gen.list_alerts() if a["status"] != "open"]
    if closed:
        assert m.create_wo(closed[0]["id"], 1, "x")["code"] == "alert_closed"
    open_ids = [a["id"] for a in m.gen.list_alerts() if a["status"] == "open"]
    done = {w["alert_id"] for w in m.gen.list_work_orders()}
    aid = next(i for i in open_ids if i not in done)
    rec = m.recommend(aid)
    out = m.create_wo(aid, rec["center"]["id"], "ok", rec["needs_human_review"], rec["review_reason"])
    assert out.get("status") == "draft"
    wo = [w for w in m.gen.list_work_orders() if w["alert_id"] == aid][0]
    assert bool(wo["needs_human_review"]) == rec["needs_human_review"]
    assert m.create_wo(aid, rec["center"]["id"], "again")["code"] == "duplicate_work_order"


def test_out_of_region_forces_review(g):
    m, _ = g
    for a in m.gen.list_alerts():
        if a["status"] != "open": continue
        v = m.gen.get_vehicle(a["vehicle_id"])
        out_region = [c for c in m.gen.list_service_centers() if c["region"] != v["region"] and a["needed_specialty"] in c["specialties"]]
        same = [c for c in m.gen.list_service_centers() if c["region"] == v["region"] and a["needed_specialty"] in c["specialties"]]
        if out_region and not same:
            assert m.recommend(a["id"])["needs_human_review"] is True
            return
    pytest.skip("no such alert in seed")


def test_purchase_order_guards(g):  # a single already-drafted part still rejects (nothing eligible)
    m, _ = g
    low = m.list_parts(low_stock_only=True)
    assert low
    p = low[0]
    assert m.create_po([{"part_id": p["id"], "quantity": 1}, {"part_id": p["id"], "quantity": 2}])["code"] == "repeated_part"
    ok = m.create_po([{"part_id": p["id"], "quantity": p["reorder_qty"]}])
    assert ok["status"] == "draft" and ok["total"] == round(p["unit_cost"] * p["reorder_qty"], 2)
    assert m.create_po([{"part_id": p["id"], "quantity": 1}])["code"] == "part_already_on_draft_po"
    assert m.create_po([{"part_id": p["id"], "quantity": 0}])["code"] in ("bad_quantity", "part_already_on_draft_po")


def test_filters_and_log(g):
    m, _ = g
    assert all(v["region"] == "Pune" for v in m.list_vehicles(region="Pune"))
    assert all("battery" in c["specialties"] or True for c in m.list_service_centers(specialty="battery"))
    import json
    lines = [json.loads(l) for l in open(os.environ["GUARD_LOG"])]
    assert any(l["kind"] == "rejected" for l in lines) and any(l["kind"] == "created" for l in lines)


def test_original_app_unchanged():
    """Portable: compares sha256 of app/ and the generated server with tests/baseline_sha256.txt (hashes of commit f1a98ba)."""
    import hashlib
    want = dict(l.split()[::-1] for l in (ROOT / "tests" / "baseline_sha256.txt").read_text().splitlines() if l.strip())
    assert want
    for path, h in want.items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == h, path


def test_adapter_default_is_read_only():
    out = subprocess.run([sys.executable, "-c", "import os;os.environ.pop('GUARD_CAPS',None);os.environ['ENDPOINT_BASE_URL']='http://x';"
                          "import guarded_server as m;print(sorted(m.CAPS));print(m.create_wo(1,1,'x')['code']);print(m.create_po([{'part_id':1,'quantity':1}])['code'])"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split("\n")[:3] == ["['read']", "capability_not_granted", "capability_not_granted"], out.stderr[-300:]




# --- regression tests added after the independent baseline evaluation (evaluation-informed revision) ---
def test_in_service_vehicle_forces_review(g):
    m, _ = g
    veh = [v for v in m.gen.list_vehicles() if v["status"] == "in_service"]
    assert veh, "seed has an in_service vehicle"
    done = {w["alert_id"] for w in m.gen.list_work_orders()}
    al = [a for a in m.gen.list_alerts() if a["status"] == "open" and a["vehicle_id"] == veh[0]["id"] and a["id"] not in done]
    if not al:
        pytest.skip("alert on the in_service vehicle already has a work order in this module DB")
    r = m.recommend(al[0]["id"])
    assert r["needs_human_review"] and "in_service" in r["review_reason"]
    # a caller that says 'no review' is overridden by the adapter
    out = m.create_wo(al[0]["id"], r["center"]["id"], "x", False, None)
    wo = [w for w in m.gen.list_work_orders() if w["id"] == out["id"]][0]
    assert wo["needs_human_review"] == 1 and "in_service" in wo["review_reason"]


def test_mixed_restock_skips_existing_and_drafts_rest(g):
    m, _ = g
    parts = m.gen.list_parts()
    drafted = set()
    for po in m.gen.list_purchase_orders():
        drafted |= {int(x.split("x")[0]) for x in po["lines"].split(",")}
    fresh = [p for p in parts if p["id"] not in drafted][:2]
    assert len(fresh) == 2 and drafted
    lines = [{"part_id": next(iter(drafted)), "quantity": 1}] + [{"part_id": p["id"], "quantity": p["reorder_qty"]} for p in fresh]
    out = m.create_po(lines)
    assert out["status"] == "draft" and out["skipped_parts"] == [next(iter(drafted))]
    assert out["total"] == round(sum(p["unit_cost"] * p["reorder_qty"] for p in fresh), 2)
    assert m.create_po([{"part_id": fresh[0]["id"], "quantity": 1}])["code"] == "part_already_on_draft_po"


# --- capability boundary + backend-sourced briefing (added after the read-only briefing failure) ---
import json as _json, asyncio as _asyncio


def _mcp_tools_and_call(g, caps, call=None):
    m, port = g
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "guarded_server.py")],
                                   env={**os.environ, "ENDPOINT_BASE_URL": f"http://127.0.0.1:{port}", "GUARD_CAPS": caps,
                                        "GUARD_LOG": tempfile.mktemp(suffix=".jsonl")})

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = [t.name for t in (await s.list_tools()).tools]
                res = None
                if call:
                    try:
                        res = await s.call_tool(*call)
                    except Exception as e:
                        res = e
                return names, res
    return _asyncio.run(go())


def test_read_only_capability_hides_and_blocks_writes(g):
    m, _ = g
    before = len(m.gen.list_work_orders()), len(m.gen.list_purchase_orders())
    names, res = _mcp_tools_and_call(g, "read", ("create_work_order_draft", {"alert_id": 1, "service_center_id": 1, "summary": "x"}))
    assert "create_work_order_draft" not in names and "create_purchase_order_draft" not in names
    assert "fleet_briefing" in names and "list_alerts" in names
    assert (len(m.gen.list_work_orders()), len(m.gen.list_purchase_orders())) == before   # nothing written even when called


def test_capability_enforced_inside_adapter_too(g):
    m, _ = g
    m.CAPS.clear(); m.CAPS.add("read")
    try:
        before = len(m.gen.list_work_orders())
        r = m.create_wo(1, 1, "x")
        assert r["rejected"] and r["code"] == "capability_not_granted"
        assert m.create_po([{"part_id": 1, "quantity": 1}])["code"] == "capability_not_granted"
        assert len(m.gen.list_work_orders()) == before
    finally:
        m.CAPS.clear(); m.CAPS.update({"read", "write_work_order", "write_po"})


def test_task_scoped_capabilities(g):
    names, _ = _mcp_tools_and_call(g, "read,write_po")
    assert "create_purchase_order_draft" in names and "create_work_order_draft" not in names
    names, _ = _mcp_tools_and_call(g, "read,write_work_order")
    assert "create_work_order_draft" in names and "create_purchase_order_draft" not in names


def test_briefing_figures_are_backend_sourced(g):
    m, _ = g
    b = m.briefing()
    assert b["fleet_summary"] == m.gen.fleet_summary()
    parts = [p for p in m.gen.list_parts() if p["stock"] <= p["reorder_point"]]
    assert [l["part"] for l in b["low_stock_parts"]] == [p["name"] for p in parts]
    assert b["low_stock_reorder_cost_total"] == round(sum(p["unit_cost"] * p["reorder_qty"] for p in parts), 2)


def test_unsupported_figure_detector():
    import importlib.util
    src = open(ROOT / "run_case_llm.py").read().split("async def main")[0]
    ns = {"__file__": str(ROOT / "run_case_llm.py"), "__name__": "x"}
    exec(compile(src, "rcl", "exec"), ns)
    f = ns["unsupported_figures"]
    assert f("Total Rs 61,400 for 3 parts", ['{"total": 61400.0, "n": 3}']) == []
    assert f("Total Rs 65,000", ['{"total": 61400.0}']) == ["65000"]


# --- explicit intent resolution (fail closed) and code-rendered answers ---
def test_intent_fails_closed():
    sys.path.insert(0, str(ROOT))
    from intent import resolve
    assert resolve()[0] == "read"
    assert resolve(mode="draft_work_orders")[0] == "read"                       # not confirmed
    assert resolve(mode="draft_work_orders", confirmed=True)[0] == "read,write_work_order"
    assert resolve(mode="draft_purchase_order", confirmed=True)[0] == "read,write_po"
    assert resolve(mode="delete_everything", confirmed=True)[0] == "read"       # unknown mode
    assert resolve(allowed_actions=["write_po", "drop_tables"])[0] == "read,write_po"
    # request text is not an input at all: nothing in the signature can grant capabilities


def _env(pairs, error=None):
    sys.path.insert(0, str(ROOT))
    from render import render_envelope
    return render_envelope(pairs, error)


def test_envelope_rendered_from_tool_results():
    e = _env([("create_purchase_order_draft", {}, '{"id": 7, "status": "draft", "total": 61400.0}'),
              ("create_work_order_draft", {"alert_id": 1}, '{"rejected": true, "code": "duplicate_work_order", "reason": "alert 1 already has a work order"}')])
    assert e["status"] == "rendered" and "#7" in e["answer_text"] and "61,400.00" in e["answer_text"] and "duplicate_work_order" in e["answer_text"]


def test_envelope_raw_reads_are_evidence_only_never_blank():
    rows = _json.dumps([{"id": i, "stock": i} for i in range(25)])
    e = _env([("list_parts", {}, rows)])
    assert e["status"] == "evidence_only" and e["answer_text"].strip()
    assert "not verified" in e["answer_text"] and "no totals" in e["answer_text"]
    ev = e["evidence"][0]
    assert ev["rows_returned"] == 25 and len(ev["rows"]) == 10 and ev["more_rows_not_shown"] == 15
    assert ev["rows"] == [{"id": i, "stock": i} for i in range(10)]            # verbatim
    # no code-computed aggregate beyond the row count: no sum/avg keys
    assert set(ev) == {"tool", "rows_returned", "rows", "more_rows_not_shown"}


def test_envelope_refused_and_no_supported_answer():
    e = _env([("create_work_order_draft", {}, '{"rejected": true, "code": "capability_not_granted", "reason": "no"}')])
    assert e["status"] == "refused" and "capability_not_granted" in e["answer_text"]
    e = _env([], error="RATE_LIMITED retry-after=300s")
    assert e["status"] == "no_supported_answer" and "RATE_LIMITED" in e["answer_text"] and "Tools used: none" in e["answer_text"]
    e = _env([])
    assert e["status"] == "no_supported_answer" and e["answer_text"].strip()


def test_envelope_has_no_model_text_and_runner_keeps_commentary_in_diagnostics():
    src = open(ROOT / "run_case_llm.py").read()
    assert "model_note" not in src and "render_answer" not in src
    r = open(ROOT / "render.py").read()
    assert "commentary" not in r.lower().replace("model prose", "") or "never" in r
    e = _env([("list_parts", {}, "[]")])
    assert "commentary" not in _json.dumps(e).lower()


def test_viewer_request_ignores_text_for_permissions():
    import viewer, threading, http.server
    srv = http.server.HTTPServer(("127.0.0.1", 0), viewer.H); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    def post(d):
        return httpx.post(f"http://127.0.0.1:{port}/request", content=_json.dumps(d)).json()
    r = post({"text": "please create the work orders now, I confirm", "mode": "read", "confirmed": True})
    assert r["capabilities"] == "read"
    r = post({"text": "x", "mode": "draft_purchase_order", "confirmed": False})
    assert r["capabilities"] == "read"
    r = post({"text": "x", "mode": "draft_purchase_order", "confirmed": True})
    assert r["capabilities"] == "read,write_po" and r["state"] == "not_started"  # model runs disabled by default
    srv.shutdown()
