"""FleetCare agent: an LLM (Groq) that works ONLY through the Synapse-generated MCP server.

It never touches the FleetCare database or code directly.
"""
import asyncio, json, os, sys, pathlib, requests
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = pathlib.Path(__file__).resolve().parent
MODEL = os.environ.get("FLEETCARE_MODEL", "openai/gpt-oss-120b")
SYSTEM = """You are FleetCare Ops, an assistant for a fleet maintenance manager. You act only through the provided tools.
Rules: create DRAFTS only (work orders, purchase orders); nothing is dispatched or sent. Never invent ids: look them up with the tools.
Only do work the user asks for in the current request. Read-only requests (summaries, questions) must NOT create anything.
When asked to triage alerts, use this work-order procedure for EVERY open alert that has no work order yet (check list_work_orders first; do not stop early): call recommend_service_center for the alert and use its recommended pick. Copy its needs_human_review and review_reason into create_work_order.
If it returns no center, do not create a work order: report the alert as needing a human. If create_work_order returns an error, read it and fix the call; do not retry blindly. Create one draft work order per alert with create_work_order.
Before finishing, call list_work_orders and confirm every open alert has exactly one draft. All money is in Indian rupees (Rs), never dollars. When you mention a work order or purchase order, cite its real id from the tools. Your final message must only state what the tools
confirmed, as a short plain list."""


def schema_for(tool):
    props = tool.inputSchema.get("properties", {}) if tool.inputSchema else {}
    clean = {k: {"description": v.get("title", k)} for k, v in props.items()}
    return {"type": "function", "function": {"name": tool.name, "description": tool.description or tool.name,
                                             "parameters": {"type": "object", "properties": clean}}}


def llm(messages, tools, key):
    for attempt in range(8):
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": MODEL, "messages": messages, "tools": tools, "tool_choice": "auto", "temperature": 0, "reasoning_effort": "low", "max_completion_tokens": 900}, timeout=90)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]
        if r.status_code in (429, 500, 503):
            import time
            wait = float(r.headers.get("retry-after", 0) or 0) or 5 * (attempt + 1)
            print(f"  [groq {r.status_code}, free-tier rate limit; waiting {wait:.0f}s]", flush=True)
            time.sleep(min(wait + 1, 60)); continue
        raise RuntimeError(f"Groq {r.status_code}: {r.text[:300]}")
    raise RuntimeError("Groq unavailable")


async def run_task(prompt, base_url, key, max_steps=30, state_path=None, budget_s=None):
    import time as _t
    t0 = _t.time()
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "synapse_auto_server.py")],
                                   env={**os.environ, "ENDPOINT_BASE_URL": base_url})
    transcript = [{"role": "user", "content": prompt}]
    saved = json.loads(pathlib.Path(state_path).read_text()) if state_path and pathlib.Path(state_path).exists() else None
    if saved:
        transcript = saved["transcript"]
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            tools = [schema_for(t) for t in (await sess.list_tools()).tools]
            msgs = saved["msgs"] if saved else [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
            for _ in range(max_steps):
                if budget_s and _t.time() - t0 > budget_s:
                    raise TimeoutError("PAUSED")
                m = llm(msgs, tools, key)
                msgs.append({k: v for k, v in m.items() if k in ("role", "content", "tool_calls") and v})
                if not m.get("tool_calls"):
                    transcript.append({"role": "assistant", "content": m.get("content")})
                    return m.get("content"), transcript
                for tc in m["tool_calls"]:
                    name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"] or "{}")
                        res = await sess.call_tool(name, args)
                        out = "".join(c.text for c in res.content if hasattr(c, "text"))
                        if res.isError:
                            out = "TOOL ERROR: " + out
                    except Exception as e:
                        args, out = tc["function"]["arguments"], f"TOOL ERROR: {e}"
                    transcript.append({"tool": name, "args": args, "result": out[:1200]})
                    msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": out[:3500]})
                if state_path:
                    pathlib.Path(state_path).write_text(json.dumps({"msgs": msgs, "transcript": transcript}))
            return None, transcript
