# FleetCare: making a fleet-maintenance backend agent-ready with Synapse

Track 1 (make an existing backend agent-ready). All data is synthetic.

## What it is
- `app/` is the original FleetCare FastAPI backend (SQLite, 10 routes). It has no agent or MCP code and Synapse did not change it.
- `synapse_auto_server.generated.py` is the MCP server exactly as the Synapse CLI (`synapse build --auto --local`, v0.1.9) generated it from the app's HTTP routes.
- `synapse_auto_server.py` is that file with small, listed edits (below). Synapse says generated files are ours to edit.
- `agent.py` is an LLM agent (Groq, `openai/gpt-oss-120b`, free tier) that works only through the MCP server. It never touches the DB.
- `run_demo.py` runs the three tasks against a fresh DB and asserts on the resulting DB state.
- Demo video: 95 s screen recording of the real viewer UI with a synthetic demo voiceover (not a recording of the author). It shows (1) the no-model adapter demo, a rules script with no AI model, running through the guarded adapter, including two refused requests, and (2) three clearly labeled cards of the recorded real-model (Groq) results from the first submission, which were not re-run for the video. Link: https://github.com/sagar-grv/fleetcare-synapse-mcp/releases/download/demo-v1/2-fleetcare-terminal-demo.mp4
- `evidence/` has the full tool-call transcripts of the last run; `DEMO.md` renders them readably.

## Regenerate the MCP server
    npm i -g @2ndbrainlabs-ai/synapse-cli
    synapse build --auto --local --provider custom --model none --llm-base-url http://127.0.0.1:9/v1 \
        --all-endpoints --base-url http://127.0.0.1:8000 --server-name fleetcare
(Auto mode is a passthrough and calls no model, but the CLI insists on a configured provider, so a dummy one is given. No Synapse account or hosted build was used.)

## Edits to the generated file (diff against `.generated.py`)
1. Nested double quotes inside f-strings (invalid before Python 3.12) replaced by a `BASE()` helper.
2. The generated tools had no query parameters, so filters were lost. Added `status`, `region`, `severity`, `low_stock_only`, `specialty` pass-through params to the four list tools.
3. Return type `dict` changed to `Any` (list responses).
4. Tool descriptions rewritten from "GET /path" to usable descriptions.

## Endpoints exposed, and why
All 10 are exposed. Reads (vehicles, alerts, parts, service centers, work orders, purchase orders, fleet summary) give the agent context. The two POSTs only create drafts (`create_work_order_draft`, `create_purchase_order_draft`), so nothing is dispatched or sent. A human stays in the loop.

## The three tasks (last run, `evidence/*.json`)
1. Alert triage: 5 open alerts, 5 draft work orders. Alert 3 (critical engine fault, Bengaluru) has no engine-capable center in its region, so the agent picks Mumbai Heavy Garage and flags it for human review. This is the borderline decision.
2. Parts reorder: 3 parts at or below reorder point, one draft purchase order, total Rs 61,400 (checked arithmetic: 12x1450 + 16x650 + 6x5600).
3. Fleet summary: vehicles by status, open alerts by severity, low-stock count, drafts that exist, and what needs a human decision. Read-only.


## Agent-layer harness (added after the first submission, app/ untouched)
`app/` and `synapse_auto_server.generated.py` are byte-identical to the originally submitted commit (`tests/baseline_sha256.txt`, checked by a test). Nothing below modifies the app.
- `guarded_server.py`: an MCP adapter in front of the generated server. Work orders and purchase orders are only available when a person explicitly grants them (`intent.py`; request text never grants anything). It refuses duplicate work orders, specialty mismatches and parts already on a draft PO, forces human review for out-of-region centers and for vehicles already in service, and logs every action.
- `viewer.py`: read-only page that shows alerts, parts, work orders and the adapter log. `demo_local.py` runs a no-model rules baseline through the adapter so the viewer has real state to show.
- `run_case.py` (rules baseline) and `run_case_llm.py` (LLM runner) execute the dev cases in `cases/`.
- Tests: `pytest tests` (35 tests: original MCP tests plus adapter, intent, viewer and generated-server tests).

### What each piece of evidence is
1. `evidence/` and `DEMO.md`: three real LLM runs (Groq, free tier) of the three tasks through the original app and the hand-edited MCP server, from the first submission.
2. `demo_local.py`: no model. A rules script showing adapter behavior, including two refused duplicate attempts.
3. One real-model, read-only briefing through the adapter, run once locally (2 API calls). Not a test of reliability.

### Limits
- Synthetic data, small. The adapter checks bind only actions that go through it; direct HTTP calls to the app bypass them. Read-then-write is not atomic.
- No evidence that the LLM does better than the rules baseline on matched cases. The adapter has not been run with a model through all three tasks.
- Free-tier model limits stopped longer runs more than once.

## Tests
    pip install fastapi uvicorn httpx pytest "mcp<2" requests
    pytest tests          # 35 tests (see above); the original 5 run the MCP server over real stdio against the live API
    GROQ_API_KEY=... python run_demo.py task1_alert_triage   # then task2_..., task3_...; then `python run_demo.py check`
`run_demo.py` is resumable (the free Groq tier allows 8,000 tokens/minute, so runs wait on rate limits).

## Honest limits
- LLM output varies between runs. An earlier run missed alerts and another created duplicate work orders until the system prompt was tightened. The final run passed the DB assertions, but this is a prototype, not a reliability guarantee.
- Synthetic data, no auth, drafts only.
