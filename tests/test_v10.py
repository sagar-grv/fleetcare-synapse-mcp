import json, os, subprocess, sys, time, socket, pathlib, urllib.request as u
import pytest
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from render import render_envelope


def test_mixed_rendered_and_raw_never_claims_completion():
    pairs = [("fleet_briefing", {}, json.dumps({"fleet_summary": {"vehicles_by_status": [{"status": "active", "n": 3}], "open_alerts_by_severity": [{"severity": "high", "n": 2}], "draft_work_orders": 0}, "low_stock_parts": [], "low_stock_reorder_cost_total": 0})),
             ("list_alerts", {}, json.dumps([{"id": 1, "code": "X"}]))]
    e = render_envelope(pairs)
    assert e["overall_completion"] == "NOT VERIFIED"
    assert e["evidence_disclosure"] and e["evidence"], "raw evidence must carry the disclaimer even under rendered status"
    assert "coverage is unknown" in e["request_coverage"]
    assert e["verified_results"] and all("list_alerts" not in v for v in e["verified_results"])


def test_no_tools_no_answer_still_not_verified():
    e = render_envelope([], error="boom")
    assert e["status"] == "no_supported_answer" and e["overall_completion"] == "NOT VERIFIED"


@pytest.fixture()
def viewer(tmp_path):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = {**os.environ, "RUNS_DIR": str(tmp_path), "ALLOW_MODEL_RUN": "0", "ENDPOINT_BASE_URL": "http://127.0.0.1:1"}
    p = subprocess.Popen([sys.executable, str(ROOT / "viewer.py"), str(port)], env=env)
    for _ in range(50):
        try: u.urlopen(f"http://127.0.0.1:{port}/", timeout=1); break
        except Exception: time.sleep(.1)
    yield port, tmp_path
    p.terminate()


def test_result_is_bound_to_request_and_misbound_files_are_ignored(viewer):
    port, runs = viewer
    post = lambda: json.load(u.urlopen(u.Request(f"http://127.0.0.1:{port}/request", json.dumps({"text": "x", "mode": "read"}).encode())))
    a, b = post()["request_id"], post()["request_id"]
    assert a != b
    get = lambda i: json.load(u.urlopen(f"http://127.0.0.1:{port}/result?id={i}"))
    assert get(a)["envelope"] is None and get(a)["state"] == "not_started"   # model runs disabled here: no result, no stale file
    (runs / a / "result.json").write_text(json.dumps({"request_id": b, "status": "rendered"}))   # old/other completion
    assert get(a)["envelope"] is None
    for bad in ("", "nope", "../x", "abcdef012345"):
        with pytest.raises(Exception): get(bad)


def test_viewer_has_no_unescaped_inserts():
    src = (ROOT / "viewer.py").read_text()
    assert "<td>'+y+'</td>" not in src and "innerHTML=r." not in src


def test_state_shown_only_from_polled_result():
    src = (ROOT / "viewer.py").read_text()
    assert "r.state" not in src.split("run.onclick")[1].split("go();")[0]
    assert "if((e.verified_results||[]).length)" in src
