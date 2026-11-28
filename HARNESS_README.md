# FleetCare strict-path harness (local, unpublished)
Requires Python 3.12+ (the unedited Synapse output uses 3.12-only f-string syntax) and: pip install "mcp<2" requests httpx fastapi uvicorn pytest

Layers: untouched app/ (commit f1a98ba) <- synapse_auto_server.generated.py (unedited Auto output, imported as is)
<- guarded_server.py (agent-layer MCP adapter: filters, recommend_service_center, deterministic write checks, JSONL log) <- LLM or rules runner.
Limit: checks bind only actions through the adapter. Direct HTTP to the app bypasses them; read-then-write is not atomic; the API has no delete.

Tests:        python -m pytest -q tests/test_guarded.py       
Rules run:    python run_case.py cases/dev_triage.json          (no LLM)
LLM run:      GROQ_API_KEY=... python run_case_llm.py cases/dev_triage.json
Plumbing:     MOCK_LLM=1 python run_case_llm.py cases/dev_triage.json   (scripted stand-in, NOT a model result)
Case file: {"id","task":"triage|restock|briefing","instruction","targets":{"alert_ids":[],"part_ids":[]},"fixture_db":null|path}
fixture_db is copied and used through env FLEETCARE_DB (no app change). Output: tool calls, tool args, rejections with reasons, work/purchase orders, wall clock, llm_calls, tokens.
The rules runner ignores "instruction"; report instruction-phrasing results for the LLM separately, not as a head-to-head score.
The LLM runner paces on Groq rate-limit headers (waits for the token window to refill before a call that would not fit; on 429 waits retry-after). It stops with RATE_LIMITED if a wait exceeds PACE_MAX_WAIT_S (default 75) or after PACE_MAX_429 (default 4) 429s in one case. Waits are reported in the output field "pacing". Pacing only delays; it changes no prompt, tool, case or app behavior.
Viewer: ENDPOINT_BASE_URL=http://127.0.0.1:8000 GUARD_LOG=guard_log.jsonl VIEWER_LABEL='rules baseline (not a model run)' python viewer.py 8090  (read-only, separate process, uses only the app's GET endpoints and the adapter log)

Revision note (evaluation-informed): after an independent baseline evaluation, two general defects were fixed in the shared adapter policy, with regression tests:
(1) an open alert on a vehicle already in_service now forces human review with a reason; (2) a purchase order that mixes parts already on a draft PO with new parts now skips and lists the existing ones (skipped_parts) and drafts the rest; if nothing remains it is rejected. Both variants use this policy.

Revision note 2 (evaluation-informed, after a read-only briefing run created two draft work orders and quoted self-computed arithmetic):
- Operation capabilities (read / write_work_order / write_po) are set by the harness per case task (briefing=read, triage=read+write_work_order, restock=read+write_po), never by the model. A write tool outside the granted set is not listed to the model and is also refused inside the adapter (capability_not_granted). Prompt wording is not the control.
- New read tool fleet_briefing returns backend fleet summary plus per-part and total reorder cost computed by code. The prompt tells the model to quote it, but the output also gets "unsupported_figures": numbers in the final text that appear in no tool result (by value). That check detects, it does not prevent, and a coincidental match passes.
- Limit: the capability is chosen from the case task field. A case whose task type grants writes still allows them, even if the instruction text is a question.

Revision note 3 (after review of v6): capabilities no longer come from the task type.
- intent.py resolves what a request may do, failing closed: only an explicit choice grants writes. Entry points: (a) case file field "allowed_actions" (list of read, write_work_order, write_po; absent = read-only, so old case files without it become read-only); (b) CLI/viewer: mode (read | draft_work_orders | draft_purchase_order) plus an explicit confirmation for any write mode; unconfirmed, unknown or missing = read-only. Request text is not an input to permissions.
- viewer.py has a request panel: radio for Ask only (default) / Allow draft work orders / Allow a draft purchase order, and a confirm checkbox. POST /request returns the resolved capabilities. Model runs from the viewer are disabled unless ALLOW_MODEL_RUN=1.
- Figures: the user-facing "answer" in run_case_llm output is rendered by code from tool results only (fleet_briefing figures, backend PO totals, draft ids, adapter refusals). The model's prose is "model_note" and is withheld (model_note_withheld=true) if it contains any number absent from the tool results. unsupported_figures remains a detector for the record, not the control.
- Limits: the human's choice is the only intent control; a person who selects and confirms a write mode allows writes. Rendering covers briefing, work-order, PO and refusal results; any other free-text claim in model_note is unverified.

Revision note 4: the adapter default is now read-only (GUARD_CAPS unset = read). run_case.py also resolves capabilities through intent.py, so a case file without allowed_actions runs read-only in both runners (the rules baseline then creates nothing; its rejections are capability refusals). The byte-identity test is portable: it compares sha256 of app/ and synapse_auto_server.generated.py with tests/baseline_sha256.txt (hashes of the files at commit f1a98ba, equal to the git blobs).

Revision note 5 (v9, display layer only): run_case_llm.py now returns "envelope" (built by render.py from tool results only) instead of "answer"/"model_note". Statuses: rendered | refused | evidence_only | no_supported_answer. Raw reads are shown as returned with a fixed disclosure that the requested answer or calculation is not verified (no totals, no conclusions; rows_returned is the only counted figure; 10 rows per tool, the rest counted as "more not shown"). No model text appears in the envelope or in the viewer, collapsed or not. Model output is kept only in "dev_diagnostics" (labelled UNVERIFIED, developer use only); do not export it with answers or summaries. The viewer has a Result panel that reads the envelope file (RESULT_FILE, written by the runner when RESULT_OUT is set). Adapter, intent, capability enforcement, app and generated server are unchanged.

## Revision note 6 (v10)
- POST /request now creates a request id and its own folder (request.json, state.json, result.json). The launcher sets RESULT_OUT and REQUEST_ID and points the runner at the viewer's app (ENDPOINT_BASE_URL_EXTERNAL) and shared guard log. The runner writes the envelope atomically (temp file + rename) with its request_id; GET /result?id= returns 404 for no/unknown id, and ignores any result file whose request_id does not match. There is no global result file.
- Envelope: always carries overall_completion = NOT VERIFIED and a request-coverage-unknown line. Verified tool results are listed separately from raw evidence rows, and the evidence disclaimer shows under rendered status too. Coverage is not inferred from the tool list.
- Viewer escapes every backend/log/server string (esc / raw only for fixed markup).
- tests/test_mcp_tools.py (shipped from f1a98ba, targeted a hand-edited server that is not in this package) is replaced by tests/test_generated_server.py, which tests the unedited generated server's real contract (10 tools, create_work_order / create_purchase_order, no list filters, 404 and 422 surface as tool errors). Filter / low-stock behaviour is tested at the adapter in test_guarded.py. No generated/app/enforcement edits; no assertion was weakened (the 404, total and zero-quantity checks are kept).
- run_kind is MOCK when MOCK_LLM is set. The no-model exercise in this revision is plumbing only.
- v10.1: the submit line shows only the permission decision; request state comes only from the polled /result. The 'Verified tool results' heading appears only when verified results exist.
