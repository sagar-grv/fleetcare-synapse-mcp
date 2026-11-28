"""LLM variant of the case harness. The model works ONLY through MCP: stdio client -> guarded_server.py
-> synapse_auto_server.generated.py -> untouched app. No direct HTTP from the model path.

Usage: GROQ_API_KEY=... python run_case_llm.py case.json
       MOCK_LLM=1 python run_case_llm.py case.json   # offline plumbing test, no network LLM
Same case file and output shape as run_case.py (variant "llm"), plus tokens, llm_calls, tool_args, transcript.
Note: the instruction text is given to the model; the rules variant ignores it. Compare execution only on
structured task/targets cases; report instruction-phrasing cases separately.
"""
import asyncio, json, os, shutil, socket, subprocess, sys, tempfile, time, pathlib
import httpx, requests
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = pathlib.Path(__file__).resolve().parent
MODEL = os.environ.get("FLEETCARE_MODEL", "openai/gpt-oss-120b")
from intent import resolve   # allowed actions come from an explicit choice, never from the task type or the text
SYSTEM = """You are FleetCare Ops, an assistant for a fleet maintenance manager. You act only through the provided tools.
Rules: create DRAFTS only (work orders, purchase orders); nothing is dispatched or sent. Never invent ids: look them up with the tools.
Only do what the user asks in the current request. Read-only requests (summaries, questions) must NOT create anything.
Triage: for EVERY open alert without a work order (check list_work_orders first), call recommend_service_center and use its pick;
copy its needs_human_review and review_reason into create_work_order_draft. If it returns no center, create nothing for that alert and
report it as needing a human. If a tool returns "rejected", read the reason, do not retry the same call, and report it.
Restock: use list_parts with low_stock_only and the backend's reorder_qty; create_purchase_order_draft returns the real total.
Briefing: call fleet_briefing and report its figures exactly as returned; never add, multiply or estimate numbers yourself, and never create anything. Money is in Indian rupees (Rs). Cite real ids. Your final message must state only what the tools confirmed, as a short plain list."""


def schema_for(t):
    props = (t.inputSchema or {}).get("properties", {})
    return {"type": "function", "function": {"name": t.name, "description": t.description or t.name,
            "parameters": {"type": "object", "properties": {k: {"description": v.get("title", k)} for k, v in props.items()}}}}


USAGE = {"calls": 0, "tokens": 0}


PACE = {"waits": 0, "waited_s": 0.0, "429s": 0}
MAX_WAIT = float(os.environ.get("PACE_MAX_WAIT_S", "75"))   # longest single wait we accept
MAX_429 = int(os.environ.get("PACE_MAX_429", "4"))           # total 429s tolerated per case, then stop


def _secs(v):
    """Parse Groq reset values like '7.66s', '1m26.4s', '250ms'."""
    import re
    if not v: return 0.0
    tot = 0.0
    for n, u in re.findall(r"([\d.]+)(ms|s|m|h)", v):
        tot += float(n) * {"ms": .001, "s": 1, "m": 60, "h": 3600}[u]
    return tot


def _wait(sec, why):
    sec = min(sec + 0.5, MAX_WAIT); PACE["waits"] += 1; PACE["waited_s"] = round(PACE["waited_s"] + sec, 1)
    print(f"  [pacing: waiting {sec:.1f}s ({why})]", file=sys.stderr, flush=True); time.sleep(sec)


def llm(msgs, tools, key):
    """One chat call. Header-aware pacing only: waits for the token window to refill, never retries blindly.
    Stops (raises RATE_LIMITED) if a wait would exceed MAX_WAIT or after MAX_429 429s."""
    if os.environ.get("MOCK_LLM"):
        return mock(msgs)
    est = sum(len(json.dumps(m, default=str)) for m in msgs) // 3 + 1500   # rough tokens needed for this call
    while True:
        r = requests.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                          json={"model": MODEL, "messages": msgs, "tools": tools, "tool_choice": "auto", "temperature": 0,
                                "reasoning_effort": "low", "max_completion_tokens": 900}, timeout=90)
        if r.status_code == 200:
            j = r.json(); USAGE["calls"] += 1; USAGE["tokens"] += j.get("usage", {}).get("total_tokens", 0)
            rem = r.headers.get("x-ratelimit-remaining-tokens")
            if rem is not None and int(float(rem)) < est:       # next call likely would not fit: let the window refill
                need = _secs(r.headers.get("x-ratelimit-reset-tokens"))
                if need > MAX_WAIT:
                    raise RuntimeError(f"RATE_LIMITED reset-in={need:.0f}s exceeds PACE_MAX_WAIT_S")
                _wait(need, f"{rem} tokens left, next call needs about {est}")
            return j["choices"][0]["message"]
        if r.status_code == 429:
            PACE["429s"] += 1
            wait = float(r.headers.get("retry-after") or 0) or _secs(r.headers.get("x-ratelimit-reset-tokens"))
            if PACE["429s"] > MAX_429 or wait > MAX_WAIT:
                raise RuntimeError(f"RATE_LIMITED retry-after={wait:.0f}s (429s this case: {PACE['429s']})")
            _wait(wait, "429"); continue
        raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:200]}")


_mock_step = {"i": 0}
def mock(msgs):
    """Scripted stand-in used ONLY to test plumbing offline. Not a model result."""
    i = _mock_step["i"]; _mock_step["i"] += 1; USAGE["calls"] += 1
    script = [("list_alerts", {"status": "open"}), ("create_work_order_draft", {"alert_id": 1, "service_center_id": 4, "summary": "mock bad center"}),
              ("recommend_service_center", {"alert_id": 1}), ("list_parts", {"low_stock_only": True})]
    if i < len(script):
        n, a = script[i]
        return {"role": "assistant", "content": None, "tool_calls": [{"id": f"m{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}]}
    return {"role": "assistant", "content": "mock done"}


def unsupported_figures(final, results):
    """Detective check: numbers in the final text that appear in no tool result (compared by value). Flags, does not prove error."""
    import re
    def nums(x):
        out = set()
        for n in re.findall(r"\d[\d,]*\.?\d*", x or ""):
            try: out.add(float(n.replace(",", "").rstrip(".")))
            except ValueError: pass
        return out
    have = nums(" ".join(results))
    return sorted(("%g" % n) for n in nums(final) if n not in have)


from render import render_envelope


async def main():
    case = json.load(open(sys.argv[1])); key = os.environ.get("GROQ_API_KEY", "")
    tmp = pathlib.Path(tempfile.mkdtemp()); db = tmp / "fleet.db"
    if case.get("fixture_db"): shutil.copy(case["fixture_db"], db)
    ext = os.environ.get("ENDPOINT_BASE_URL_EXTERNAL")   # viewer flow: use the running app, do not start or reset one
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = None if ext else subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT,
                           env={**os.environ, "FLEETCARE_DB": str(db)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(0 if ext else 60):
        try: httpx.get(f"http://127.0.0.1:{port}/fleet/summary"); break
        except Exception: time.sleep(.2)
    base = ext or f"http://127.0.0.1:{port}"; glog = pathlib.Path(os.environ.get("GUARD_LOG") or (tmp / "guard.jsonl"))
    CAPS, caps_note = resolve(mode=case.get("mode"), confirmed=bool(case.get("confirmed")), allowed_actions=case.get("allowed_actions"))
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "guarded_server.py")],
                                   env={**os.environ, "ENDPOINT_BASE_URL": base, "GUARD_LOG": str(glog), "GUARD_CAPS": CAPS})
    tg = case.get("targets") or {}
    prompt = case.get("instruction") or "Do the task."
    if tg: prompt += f"\nTarget ids: {json.dumps(tg)}"
    calls, targs, transcript, final, err, full, pairs = [], [], [], None, None, [], []
    t0 = time.time()
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                tools = [schema_for(t) for t in (await sess.list_tools()).tools]
                msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
                for _ in range(30):
                    m = llm(msgs, tools, key)
                    msgs.append({k: v for k, v in m.items() if k in ("role", "content", "tool_calls") and v})
                    if not m.get("tool_calls"):
                        final = m.get("content"); break
                    for tc in m["tool_calls"]:
                        n = tc["function"]["name"]; a = json.loads(tc["function"]["arguments"] or "{}")
                        res = await sess.call_tool(n, a)
                        o = "".join(c.text for c in res.content if hasattr(c, "text"))
                        calls.append(n); targs.append({"tool": n, "args": a})
                        msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": o[:3500]})
                        transcript.append({"tool": n, "args": a, "result": o[:600]}); full.append(o); pairs.append((n, a, o))
    except Exception as e:
        err = str(e)
    wall = round(time.time() - t0, 2)
    c = httpx.Client(base_url=base)
    log = [json.loads(l) for l in open(glog)] if glog.exists() else []
    out = {"id": case["id"], "variant": "llm" if not os.environ.get("MOCK_LLM") else "MOCK-plumbing-only", "task": case["task"],
           "instruction": case.get("instruction"), "tool_calls": calls, "n_calls": len(calls), "tool_args": targs,
           "rejections": [l for l in log if l["kind"] == "rejected"], "skips": [l for l in log if l["kind"] == "skipped"], "work_orders": c.get("/work-orders").json(),
           "purchase_orders": c.get("/purchase-orders").json(), "llm_calls": USAGE["calls"], "tokens": USAGE["tokens"], "pacing": PACE,
           "wall_clock_s": wall, "capabilities": CAPS, "capabilities_source": caps_note, "envelope": render_envelope(pairs, err, CAPS), "dev_diagnostics": {"WARNING": "UNVERIFIED model output; developer use only; never show to users or include in answers, summaries or exports", "model_commentary": final, "unsupported_figures": unsupported_figures(final, full)}, "error": err, "transcript": transcript}
    if os.environ.get("RESULT_OUT"):
        # Envelope only (no model text), bound to its request, written atomically (tmp file then rename).
        env = {**out["envelope"], "request_id": os.environ.get("REQUEST_ID"), "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "run_kind": "MOCK plumbing (scripted stand-in, not a model)" if os.environ.get("MOCK_LLM") else "model run"}
        tmpf = os.environ["RESULT_OUT"] + ".tmp"
        with open(tmpf, "w") as f:
            json.dump(env, f); f.flush(); os.fsync(f.fileno())
        os.replace(tmpf, os.environ["RESULT_OUT"])
    print(json.dumps(out, indent=1, default=str))
    if srv: srv.terminate()
    shutil.rmtree(tmp, ignore_errors=True)

asyncio.run(main())
