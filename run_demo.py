"""Run the three FleetCare tasks end to end against a fresh synthetic DB and assert on the resulting DB state."""
import asyncio, json, os, socket, subprocess, sys, tempfile, time, pathlib, requests
from agent import run_task, ROOT

TASKS = {
 "task1_alert_triage": "Triage every OPEN alert, most severe first, and create a draft work order for each one. Flag any that need human review.",
 "task2_parts_reorder": "Find parts at or below their reorder point and create ONE draft purchase order that restocks each by its reorder_qty. Report the total cost.",
 "task3_fleet_summary": "Write my morning fleet summary with four labelled sections: (1) vehicles by status, (2) open alerts by severity, (3) low-stock parts, (4) draft work orders and purchase orders that exist now, with total PO cost. End with a list of items that need a human decision and why.",
}


def main(only=None, check=False):
    key = os.environ["GROQ_API_KEY"]  # free-tier Groq key, from the environment
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    DB = os.environ.get("DEMO_DB", "/tmp/fleetcare_demo.db")
    if only in (None, "task1_alert_triage") and os.path.exists(DB) and not (ROOT / "evidence" / "state_task1_alert_triage.json").exists():
        os.remove(DB)  # fresh synthetic data at the start of a demo run
    env = {**os.environ, "FLEETCARE_DB": DB}
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try: requests.get(base + "/fleet/summary"); break
        except Exception: time.sleep(0.2)
    out = {}
    try:
        for name, prompt in TASKS.items():
            if (only and name != only) or check:
                continue
            sp = str(ROOT / "evidence" / f"state_{name}.json")
            (ROOT / "evidence").mkdir(exist_ok=True)
            if name == "task1_alert_triage" and only and os.path.exists(sp) and not os.path.exists(DB):
                os.remove(sp)
            try:
                final, tr = asyncio.run(run_task(prompt, base, key, state_path=sp, budget_s=85))
            except BaseException as e:
                if "PAUSED" in repr(e):
                    print("PAUSED (resume by re-running the same command)"); sys.exit(3)
                raise
            out[name] = {"prompt": prompt, "final": final, "steps": tr}
            (ROOT / "evidence").mkdir(exist_ok=True)
            (ROOT / "evidence" / f"{name}.json").write_text(json.dumps(out[name], indent=2))
            print(f"== {name}: {len([t for t in tr if 'tool' in t])} tool calls\n{final}\n")
        wos = requests.get(base + "/work-orders").json(); pos = requests.get(base + "/purchase-orders").json()
        out["db_state"] = {"work_orders": wos, "purchase_orders": pos}
    finally:
        p.terminate()
    if only and not check:
        return
    # assertions on the real DB state
    alerts_covered = {w["alert_id"] for w in wos}
    assert alerts_covered == {1, 2, 3, 4, 5}, f"expected drafts for the 5 open alerts, got {alerts_covered}"
    assert all(w["status"] == "draft" for w in wos)
    flagged = {w["alert_id"] for w in wos if w["needs_human_review"]}
    assert 3 in flagged, "critical engine alert in Bengaluru has no engine center: must be flagged for review"
    assert len(pos) >= 1 and all(po["status"] == "draft" for po in pos)
    ordered = {int(x.split("x")[0]) for po in pos for x in po["lines"].split(",")}
    assert ordered == {1, 3, 5}, f"low-stock parts are 1,3,5; ordered {ordered}"
    print("ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else None
    main(only=None if a in (None, "all") else (None if a == "check" else a), check=(a == "check"))
