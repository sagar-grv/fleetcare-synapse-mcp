"""Display layer. Builds the user-facing result envelope from tool results ONLY (code, no model text).

Statuses:
  rendered             a code-backed answer exists (fleet_briefing figures, draft ids, backend PO totals)
  refused              the adapter refused the action(s); nothing else code-backed
  evidence_only        only reads ran: rows are shown as returned, the requested answer/calculation is NOT verified
  no_supported_answer  nothing renderable ran (no tool calls, an error, or a rate limit)
Model prose is not an input here and never appears in the envelope.
"""
import json

ROW_LIMIT = 10
READS = {"list_vehicles", "get_vehicle", "list_alerts", "list_parts", "list_service_centers", "list_work_orders",
         "list_purchase_orders", "fleet_summary", "recommend_service_center"}
COVERAGE = "Request coverage is unknown: the system verifies each tool result above, not that the whole request was answered or that every part was done."
EVIDENCE_DISCLOSURE = ("Evidence only. The requested answer or calculation is not verified by the system. "
                       "These are the rows the backend returned, shown as returned; no totals or conclusions were computed.")


def _rows(j):
    return j if isinstance(j, list) else [j]


def render_envelope(pairs, error=None, caps="read"):
    rendered, refusals, evidence, basis = [], [], [], []
    for n, a, o in pairs:
        try:
            j = json.loads(o)
        except Exception:
            continue
        basis.append(n)
        if n == "fleet_briefing" and isinstance(j, dict) and "fleet_summary" in j:
            s = j["fleet_summary"]
            rendered.append("Fleet: " + ", ".join(f"{x['n']} {x['status']}" for x in s["vehicles_by_status"]) + "; open alerts: " +
                            ", ".join(f"{x['n']} {x['severity']}" for x in s["open_alerts_by_severity"]) + f"; draft work orders: {s['draft_work_orders']}.")
            rendered.append("Low stock: " + "; ".join(f"{l['part']} (stock {l['stock']}, reorder {l['reorder_qty']} = Rs {l['reorder_cost']:,.2f})" for l in j["low_stock_parts"]) +
                            f". Reorder total Rs {j['low_stock_reorder_cost_total']:,.2f} (computed by code from backend fields).")
        elif n == "create_work_order_draft" and isinstance(j, dict) and j.get("status") == "draft":
            rendered.append(f"Draft work order #{j['id']} for alert {a.get('alert_id')} at service center {a.get('service_center_id')}.")
        elif n == "create_purchase_order_draft" and isinstance(j, dict) and j.get("status") == "draft":
            sk = f"; skipped parts already on a draft: {j['skipped_parts']}" if j.get("skipped_parts") else ""
            rendered.append(f"Draft purchase order #{j['id']}, backend total Rs {j['total']:,.2f}{sk}.")
        elif isinstance(j, dict) and j.get("rejected"):
            refusals.append(f"Refused: {j['code']} - {j['reason']}")
        elif n in READS:
            rows = _rows(j)
            evidence.append({"tool": n, "rows_returned": len(rows), "rows": rows[:ROW_LIMIT],
                             "more_rows_not_shown": max(0, len(rows) - ROW_LIMIT)})
    if rendered:
        status, text = "rendered", "\n".join(rendered + refusals)
    elif refusals:
        status, text = "refused", "\n".join(refusals)
    elif evidence:
        status, text = "evidence_only", EVIDENCE_DISCLOSURE
    else:
        why = f"Reason: {error}." if error else "Reason: no tool results were produced."
        status, text = "no_supported_answer", f"No verified answer. {why} Tools used: {', '.join(basis) or 'none'}."
    # Always disclosed, whatever the status: individual tool results can be verified, request coverage cannot.
    return {"status": status, "answer_text": text, "basis": basis, "capabilities": caps,
            "evidence": evidence if status in ("evidence_only", "refused", "rendered") else [], "row_limit": ROW_LIMIT,
            "verified_results": rendered + refusals,
            "evidence_disclosure": EVIDENCE_DISCLOSURE if evidence else None,
            "request_coverage": COVERAGE, "overall_completion": "NOT VERIFIED"}
