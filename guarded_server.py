"""Guarded MCP adapter for FleetCare (agent layer, not part of the app).

The original app (app/) is untouched. The Synapse-generated server
(synapse_auto_server.generated.py, unedited Auto output) does the HTTP calls.
This adapter imports those generated tools and puts deterministic checks in
front of the two write tools. It is plain code, no LLM judgment.

Limit: these checks protect actions taken through the agent. Anyone calling
the FleetCare HTTP API directly bypasses them.
"""
from __future__ import annotations
import importlib.util, json, os, pathlib, time
from typing import Any
from mcp.server.fastmcp import FastMCP

ROOT = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("gen", ROOT / "synapse_auto_server.generated.py")
gen = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(gen)

LOG = pathlib.Path(os.environ.get("GUARD_LOG", ROOT / "guard_log.jsonl"))
mcp = FastMCP("fleetcare-guarded")

# Operation capabilities, set by the harness/operator per request, never by the model.
#   read            read tools only
#   write_work_order  may create draft work orders
#   write_po          may create draft purchase orders
# Unset = read-only (fail closed). Tests and runners must pass explicit capabilities. A tool outside the granted set is not listed to the
# model AND is refused inside the adapter if called anyway.
CAPS = set((os.environ.get("GUARD_CAPS") or "read").split(",")) | {"read"}   # fail closed: unset = read-only


def _need(cap: str):
    if cap not in CAPS:
        return _reject("capability_not_granted", f"this request has capabilities {sorted(CAPS)}; {cap} is not granted")
    return None


def _log(kind: str, **kw):
    with LOG.open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%H:%M:%S"), "kind": kind, **kw}) + "\n")


def _reject(code: str, reason: str, **kw):
    _log("rejected", code=code, reason=reason, **kw)
    return {"rejected": True, "code": code, "reason": reason}


def _has(center: dict, specialty: str) -> bool:
    return specialty in [s.strip() for s in (center.get("specialties") or "").split(",")]


def _flt(rows, **kw):
    return [r for r in rows if all(v is None or str(r.get(k)) == str(v) for k, v in kw.items())]


@mcp.tool(name="list_vehicles", description="List fleet vehicles. Optional filters: status (active|in_service), region (Pune|Mumbai|Bengaluru).")
def list_vehicles(status: Any = None, region: Any = None) -> Any:
    return _flt(gen.list_vehicles(), status=status, region=region)


@mcp.tool(name="get_vehicle", description="Get one vehicle (plate, model, region, status, mileage) by numeric vehicle_id.")
def get_vehicle(vehicle_id: Any = None) -> Any:
    return gen.get_vehicle(vehicle_id)


@mcp.tool(name="list_alerts", description="List maintenance alerts with vehicle_id, code, description, severity, status (open|closed) and needed_specialty. Optional filters: status, severity.")
def list_alerts(status: Any = None, severity: Any = None) -> Any:
    return _flt(gen.list_alerts(), status=status, severity=severity)


@mcp.tool(name="list_parts", description="List parts with stock, reorder_point, reorder_qty, unit_cost, supplier. low_stock_only=true returns parts at or below reorder point.")
def list_parts(low_stock_only: Any = None) -> Any:
    rows = gen.list_parts()
    return [p for p in rows if p["stock"] <= p["reorder_point"]] if low_stock_only in (True, "true", "True") else rows


@mcp.tool(name="list_service_centers", description="List service centers (name, region, specialties, capacity_open). Optional filters: region, specialty.")
def list_service_centers(region: Any = None, specialty: Any = None) -> Any:
    rows = _flt(gen.list_service_centers(), region=region)
    return [c for c in rows if specialty is None or _has(c, str(specialty))]


@mcp.tool(name="list_work_orders", description="List all work orders (drafts).")
def list_work_orders() -> Any:
    return gen.list_work_orders()


@mcp.tool(name="list_purchase_orders", description="List all purchase order drafts.")
def list_purchase_orders() -> Any:
    return gen.list_purchase_orders()


@mcp.tool(name="fleet_summary", description="Raw backend counts: vehicles by status, open alerts by severity, low-stock part count, draft work orders.")
def fleet_summary() -> Any:
    return gen.fleet_summary()


def recommend(alert_id: int) -> dict:
    alerts = [a for a in gen.list_alerts() if a["id"] == alert_id]
    if not alerts:
        return {"error": "alert not found"}
    a = alerts[0]
    v = gen.get_vehicle(a["vehicle_id"])
    cands = [c for c in gen.list_service_centers() if _has(c, a["needed_specialty"])]
    same = [c for c in cands if c["region"] == v["region"]]
    open_same = [c for c in same if c["capacity_open"] > 0]
    pick = (open_same or same or sorted(cands, key=lambda c: -c["capacity_open"]) or [None])[0]
    if pick is None:
        return {"alert_id": alert_id, "center": None, "needs_human_review": True,
                "review_reason": f"no center offers {a['needed_specialty']}"}
    review, why = False, None
    if v.get("status") == "in_service":
        review, why = True, f"vehicle is already in_service ({v['plate']})"
    if pick["region"] != v["region"]:
        why = "; ".join(x for x in [why, f"no {a['needed_specialty']} center in {v['region']}; nearest option is in {pick['region']}"] if x); review = True
    elif pick["capacity_open"] <= 0:
        why = "; ".join(x for x in [why, f"{pick['name']} has no open capacity"] if x); review = True
    return {"alert_id": alert_id, "vehicle_region": v["region"], "needed_specialty": a["needed_specialty"],
            "center": {"id": pick["id"], "name": pick["name"], "region": pick["region"]},
            "needs_human_review": review, "review_reason": why}


def briefing() -> dict:
    """Backend-sourced figures only. Every number is a backend field or a plain sum of backend fields."""
    parts = [p for p in gen.list_parts() if p["stock"] <= p["reorder_point"]]
    lines = [{"part": p["name"], "stock": p["stock"], "reorder_point": p["reorder_point"], "reorder_qty": p["reorder_qty"],
              "unit_cost": p["unit_cost"], "reorder_cost": round(p["unit_cost"] * p["reorder_qty"], 2)} for p in parts]
    return {"fleet_summary": gen.fleet_summary(), "low_stock_parts": lines,
            "low_stock_reorder_cost_total": round(sum(l["reorder_cost"] for l in lines), 2),
            "note": "figures computed by code from backend fields; quote them as given, do not recompute"}


@mcp.tool(name="fleet_briefing", description="Read-only morning briefing: backend fleet summary, low-stock parts with per-part reorder cost, and the total, all computed by code. Use these figures verbatim.")
def fleet_briefing() -> Any:
    return briefing()


@mcp.tool(name="recommend_service_center", description="Suggest a service center for an alert (by alert_id): matches specialty, prefers the vehicle's region, flags human review when it must go outside the region or capacity is zero. Read-only.")
def recommend_service_center(alert_id: Any = None) -> Any:
    return recommend(int(alert_id))


def create_wo(alert_id, service_center_id, summary, needs_human_review=False, review_reason=None):
    if (d := _need("write_work_order")): return d
    alert_id, service_center_id = int(alert_id), int(service_center_id)
    alerts = [a for a in gen.list_alerts() if a["id"] == alert_id]
    if not alerts:
        return _reject("alert_not_found", f"alert {alert_id} does not exist", alert_id=alert_id)
    a = alerts[0]
    if a["status"] != "open":
        return _reject("alert_closed", f"alert {alert_id} is {a['status']}", alert_id=alert_id)
    if any(w["alert_id"] == alert_id for w in gen.list_work_orders()):
        return _reject("duplicate_work_order", f"alert {alert_id} already has a work order", alert_id=alert_id)
    cs = [c for c in gen.list_service_centers() if c["id"] == service_center_id]
    if not cs:
        return _reject("center_not_found", f"service center {service_center_id} does not exist", alert_id=alert_id)
    c = cs[0]
    if not _has(c, a["needed_specialty"]):
        return _reject("specialty_mismatch", f"{c['name']} lacks {a['needed_specialty']}", alert_id=alert_id)
    v = gen.get_vehicle(a["vehicle_id"])
    extra = []
    if v.get("status") == "in_service":
        extra.append(f"vehicle is already in_service ({v['plate']})")
    caller_explained = bool(needs_human_review and review_reason)
    if not caller_explained:
        if c["region"] != v["region"]:
            extra.append(f"{c['name']} is in {c['region']}, vehicle is in {v['region']}")
        elif c["capacity_open"] <= 0:
            extra.append(f"{c['name']} has no open capacity")
    if extra:
        needs_human_review = True
        have = review_reason or ""
        review_reason = "; ".join([have] + [e for e in extra if e not in have] if have else extra)
    out = gen.create_work_order(alert_id=alert_id, service_center_id=service_center_id, summary=summary,
                                      needs_human_review=bool(needs_human_review), review_reason=review_reason)
    _log("created", what="work_order", alert_id=alert_id, id=out.get("id"), review=bool(needs_human_review))
    return out


def _when(cap):
    return mcp.tool if cap in CAPS else (lambda **kw: (lambda f: f))


@_when("write_work_order")(name="create_work_order_draft", description="Create a DRAFT work order for an open alert at a service center. Checked before sending: alert open, no existing work order, center has the needed specialty; out-of-region or no-capacity picks are forced to human review.")
def create_work_order_draft(alert_id: Any = None, service_center_id: Any = None, summary: Any = None,
                            needs_human_review: Any = None, review_reason: Any = None) -> Any:
    return create_wo(alert_id, service_center_id, summary, needs_human_review, review_reason)


def create_po(lines, note=None):
    if (d := _need("write_po")): return d
    if not lines:
        return _reject("empty", "no lines")
    ids = [int(l["part_id"]) for l in lines]
    if len(set(ids)) != len(ids):
        return _reject("repeated_part", "a part appears twice in the same order")
    if any(int(l["quantity"]) <= 0 for l in lines):
        return _reject("bad_quantity", "quantity must be positive")
    drafted = set()
    for po in gen.list_purchase_orders():
        if po["status"] == "draft":
            drafted |= {int(x.split("x")[0]) for x in po["lines"].split(",") if x}
    skipped = [i for i in ids if i in drafted]
    keep = [l for l in lines if int(l["part_id"]) not in drafted]
    if not keep:
        return _reject("part_already_on_draft_po", f"parts {skipped} are already on a draft purchase order")
    out = gen.create_purchase_order(lines=[{"part_id": int(l["part_id"]), "quantity": int(l["quantity"])} for l in keep], note=note)
    if skipped:
        out = {**out, "skipped_parts": skipped, "skipped_reason": "already on an existing draft purchase order"}
        _log("skipped", parts=skipped, reason="already on a draft purchase order")
    _log("created", what="purchase_order", id=out.get("id"), total=out.get("total"))
    return out


@_when("write_po")(name="create_purchase_order_draft", description="Create a DRAFT parts purchase order from lines [{part_id, quantity}]. Never sent. Rejected if a part repeats in the order. Parts already on a draft order are skipped and listed in skipped_parts; the rest are drafted.")
def create_purchase_order_draft(lines: Any = None, note: Any = None) -> Any:
    return create_po(lines, note)


if __name__ == "__main__":
    mcp.run()
