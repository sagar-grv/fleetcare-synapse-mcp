# FleetCare: making a fleet-maintenance backend agent-ready with Synapse

Track 1 (make an existing backend agent-ready). All data is synthetic.

## What it is
- `app/` is the original FleetCare FastAPI backend (SQLite, 10 routes). It has no agent or MCP code and Synapse did not change it.
- `synapse_auto_server.generated.py` is the MCP server exactly as the Synapse CLI (`synapse build --auto --local`, v0.1.9) generated it from the app's HTTP routes.
- `synapse_auto_server.py` is that file with small, listed edits (below). Synapse says generated files are ours to edit.
- `agent.py` is an LLM agent (Groq, `openai/gpt-oss-120b`, free tier) that works only through the MCP server. It never touches the DB.
- `run_demo.py` runs the three tasks against a fresh DB and asserts on the resulting DB state.
- Demo clip (earlier version): 84s terminal replay of the recorded run (captioned as a replay, no audio): https://github.com/sagar-grv/fleetcare-synapse-mcp/releases/download/demo-v1/2-fleetcare-terminal-demo.mp4
- `evidence/` has the full tool-call transcripts of the latest real run; `DEMO.md` renders them readably.

## Update (Nov 25 night): hardening + dashboard
- Server-side guardrails in the FastAPI app (the agent no longer has to get these right): a work order is rejected if the center lacks the alert's specialty (422), if the alert already has one (409) or is closed (409); the human-review flag is forced on when the center is outside the vehicle's region. A purchase order is rejected if a part repeats or is already on a draft PO (409).
- New `GET /alerts/{id}/recommendation` (MCP tool `recommend_service_center`): ranks capable centers, same region first, and says whether a human must review. The agent prompt now uses it.
- MCP errors now include the API's own message (before, the agent only saw a bare HTTP status and could not self-correct).
- Bug fixed: omitted optional fields were sent as null and failed validation.
- Live dashboard at `/` (alerts with recommended center, work orders, stock, draft POs). Run: `uvicorn app.main:app` and open http://127.0.0.1:8000
- `demo_scripted.py`: deterministic MCP client run (no LLM, no key) that exercises all three tasks plus the guardrails and asserts on DB state. `evidence/` is a real Groq agent run against this hardened backend (all DB assertions passed). Task 3 was re-run once after a prompt tweak (rupees, cite real ids). The earlier pre-hardening run is in git history.
- Tests: 10 (was 5).

## Regenerate the MCP server
    npm i -g @2ndbrainlabs-ai/synapse-cli
    synapse build --auto --local --provider custom --model none --llm-base-url http://127.0.0.1:9/v1 \
        --all-endpoints --base-url http://127.0.0.1:8000 --server-name fleetcare
(Auto mode is a passthrough and calls no model, but the CLI insists on a configured provider, so a dummy one is given. No Synapse account or hosted build was used.)

## Edits to the generated file (diff against `.generated.py`)
0. New `recommend_service_center` tool added by hand (11 tools now), errors carry API detail, null fields dropped.
1. Nested double quotes inside f-strings (invalid before Python 3.12) replaced by a `BASE()` helper.
2. The generated tools had no query parameters, so filters were lost. Added `status`, `region`, `severity`, `low_stock_only`, `specialty` pass-through params to the four list tools.
3. Return type `dict` changed to `Any` (list responses).
4. Tool descriptions rewritten from "GET /path" to usable descriptions.

## Endpoints exposed, and why
All 10 original endpoints are exposed, plus the new recommendation endpoint (11 MCP tools). Reads (vehicles, alerts, parts, service centers, work orders, purchase orders, fleet summary) give the agent context. The two POSTs only create drafts (`create_work_order_draft`, `create_purchase_order_draft`), so nothing is dispatched or sent. A human stays in the loop.

## The three tasks (latest real run, `evidence/*.json`)
1. Alert triage: 5 open alerts, 5 draft work orders. Alert 3 (critical engine fault, Bengaluru) has no engine-capable center in its region, so the agent picks Mumbai Heavy Garage and flags it for human review. This is the borderline decision.
2. Parts reorder: 3 parts at or below reorder point, one draft purchase order, total Rs 61,400 (verified by hand: 12x1450 + 16x650 + 6x5600).
3. Fleet summary: vehicles by status, open alerts by severity, low-stock count, drafts that exist, and what needs a human decision. Read-only.

## Tests
    pip install fastapi uvicorn httpx pytest "mcp<2" requests
    pytest tests          # 10 tests: MCP server over real stdio against the live API
    GROQ_API_KEY=... python run_demo.py task1_alert_triage   # then task2_..., task3_...; then `python run_demo.py check`
`run_demo.py` is resumable (the free Groq tier allows 8,000 tokens/minute, so runs wait on rate limits).

## Honest limits
- LLM output varies between runs. An earlier run missed alerts and another created duplicate work orders until the system prompt was tightened. The final run passed the DB assertions, but this is a prototype, not a reliability guarantee.
- Synthetic data, no auth, drafts only.
