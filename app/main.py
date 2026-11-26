"""FleetCare: a small fleet-maintenance backend (synthetic data).

This is the *original app*. It knows nothing about agents or MCP.
Synapse reads its HTTP routes and generates the MCP server.
"""
import sqlite3
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .db import get_conn, init_db

app = FastAPI(title="FleetCare", description="Fleet maintenance: vehicles, alerts, parts, service centers, work orders.")
init_db()


@app.middleware("http")
async def log_activity(request, call_next):
    """Record every API call an agent makes, so the dashboard can show what the agent is doing live."""
    resp = await call_next(request)
    p = request.url.path
    if not (p == "/" or p.startswith("/dashboard") or p in ("/docs", "/openapi.json", "/favicon.ico")):
        q = request.url.query
        with get_conn() as c:
            c.execute("INSERT INTO activity(method,path,status) VALUES(?,?,?)", (request.method, p + ("?" + q if q else ""), resp.status_code))
    return resp


def rows(sql: str, args: tuple = ()):
    with get_conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


@app.get("/vehicles", operation_id="list_vehicles", summary="List vehicles, optionally filtered by status or region")
def list_vehicles(status: Optional[str] = None, region: Optional[str] = None):
    sql, args = "SELECT * FROM vehicles WHERE 1=1", []
    if status:
        sql += " AND status=?"; args.append(status)
    if region:
        sql += " AND region=?"; args.append(region)
    return rows(sql, tuple(args))


@app.get("/vehicles/{vehicle_id}", operation_id="get_vehicle", summary="Get one vehicle with its model and mileage")
def get_vehicle(vehicle_id: int):
    r = rows("SELECT * FROM vehicles WHERE id=?", (vehicle_id,))
    if not r:
        raise HTTPException(404, "vehicle not found")
    return r[0]


@app.get("/alerts", operation_id="list_alerts", summary="List maintenance alerts, filter by status (open/closed) and severity (low/medium/high/critical)")
def list_alerts(status: Optional[str] = None, severity: Optional[str] = None):
    sql, args = "SELECT * FROM alerts WHERE 1=1", []
    if status:
        sql += " AND status=?"; args.append(status)
    if severity:
        sql += " AND severity=?"; args.append(severity)
    return rows(sql + " ORDER BY id", tuple(args))


@app.get("/parts", operation_id="list_parts", summary="List parts inventory with stock level, reorder point and unit cost")
def list_parts(low_stock_only: bool = False):
    sql = "SELECT * FROM parts"
    if low_stock_only:
        sql += " WHERE stock <= reorder_point"
    return rows(sql + " ORDER BY id")


@app.get("/service-centers", operation_id="list_service_centers", summary="List service centers; filter by region and specialty")
def list_service_centers(region: Optional[str] = None, specialty: Optional[str] = None):
    sql, args = "SELECT * FROM service_centers WHERE 1=1", []
    if region:
        sql += " AND region=?"; args.append(region)
    if specialty:
        sql += " AND specialties LIKE ?"; args.append(f"%{specialty}%")
    return rows(sql, tuple(args))


@app.get("/work-orders", operation_id="list_work_orders", summary="List work orders")
def list_work_orders():
    return rows("SELECT * FROM work_orders ORDER BY id")


class WorkOrderIn(BaseModel):
    alert_id: int
    service_center_id: int
    summary: str
    needs_human_review: Optional[bool] = False
    review_reason: Optional[str] = None


def reject(action: str, code: int, reason: str):
    """Record a refused action (so a human can see what the guardrails stopped), then refuse it."""
    with get_conn() as c:
        c.execute("INSERT INTO rejections(action,code,reason) VALUES(?,?,?)", (action, code, reason))
    raise HTTPException(code, reason)


def _rank_centers(c, alert_id: int):
    """Rank service centers for an alert: must have the needed specialty; prefer same region, then open capacity."""
    a = c.execute("SELECT a.*, v.region AS vehicle_region, v.plate FROM alerts a JOIN vehicles v ON v.id=a.vehicle_id WHERE a.id=?", (alert_id,)).fetchone()
    if not a:
        return None, []
    out = []
    for s in c.execute("SELECT * FROM service_centers").fetchall():
        specs = [x.strip() for x in s["specialties"].split(",")]
        if a["needed_specialty"] not in specs:
            continue
        same = s["region"] == a["vehicle_region"]
        out.append({"service_center_id": s["id"], "name": s["name"], "region": s["region"], "capacity_open": s["capacity_open"],
                    "same_region": same, "needs_human_review": (not same) or s["capacity_open"] <= 0,
                    "review_reason": None if same and s["capacity_open"] > 0 else
                    (f"Center is in {s['region']} but vehicle {a['plate']} is in {a['vehicle_region']}" if not same else "Center has no open capacity")})
    out.sort(key=lambda r: (not r["same_region"], -r["capacity_open"], r["service_center_id"]))
    return dict(a), out


@app.get("/alerts/{alert_id}/recommendation", operation_id="recommend_service_center",
         summary="Rank capable service centers for an alert (specialty must match; same region first) and say whether a human must review")
def recommend(alert_id: int):
    with get_conn() as c:
        a, ranked = _rank_centers(c, alert_id)
    if a is None:
        raise HTTPException(404, "alert not found")
    return {"alert_id": alert_id, "needed_specialty": a["needed_specialty"], "vehicle_region": a["vehicle_region"],
            "recommended": ranked[0] if ranked else None, "alternatives": ranked[1:],
            "note": None if ranked else "No service center has this specialty: escalate to a human"}


@app.post("/work-orders", operation_id="create_work_order_draft", summary="Create a DRAFT work order for an alert at a service center")
def create_work_order(body: WorkOrderIn):
    """Server-side guardrails: the API does not trust the caller (human or agent) to get these right."""
    with get_conn() as c:
        a, ranked = _rank_centers(c, body.alert_id)
        if a is None:
            raise HTTPException(404, "alert not found")
        if not c.execute("SELECT 1 FROM service_centers WHERE id=?", (body.service_center_id,)).fetchone():
            raise HTTPException(404, "service center not found")
        if a["status"] != "open":
            reject("work order", 409, f"alert {body.alert_id} is {a['status']}, not open")
        match = next((r for r in ranked if r["service_center_id"] == body.service_center_id), None)
        if match is None:
            reject("work order", 422, f"service center {body.service_center_id} lacks the needed specialty '{a['needed_specialty']}'; "
                                     f"use recommend_service_center for alert {body.alert_id}")
        dup = c.execute("SELECT id FROM work_orders WHERE alert_id=? AND status!='cancelled'", (body.alert_id,)).fetchone()
        if dup:
            reject("work order", 409, f"alert {body.alert_id} already has work order {dup['id']}; not creating a duplicate")
        review, reason = bool(body.needs_human_review), body.review_reason
        if match["needs_human_review"]:  # server enforces the flag even if the caller forgot it
            review, reason = True, reason or match["review_reason"]
        cur = c.execute(
            "INSERT INTO work_orders(alert_id,service_center_id,summary,status,needs_human_review,review_reason) VALUES(?,?,?,?,?,?)",
            (body.alert_id, body.service_center_id, body.summary, "draft", int(review), reason))
        return {"id": cur.lastrowid, "status": "draft", "needs_human_review": bool(review), "review_reason": reason}


class PurchaseLine(BaseModel):
    part_id: int
    quantity: int


class PurchaseOrderIn(BaseModel):
    lines: list[PurchaseLine]
    note: Optional[str] = None


@app.get("/purchase-orders", operation_id="list_purchase_orders", summary="List purchase order drafts")
def list_purchase_orders():
    return rows("SELECT * FROM purchase_orders ORDER BY id")


@app.post("/purchase-orders", operation_id="create_purchase_order_draft", summary="Create a DRAFT parts purchase order (never sent)")
def create_purchase_order(body: PurchaseOrderIn):
    if not body.lines:
        raise HTTPException(422, "no lines")
    with get_conn() as c:
        total = 0.0
        ids = [l.part_id for l in body.lines]
        if len(ids) != len(set(ids)):
            reject("purchase order", 422, "each part may appear on one line only")
        for pid in ids:
            if c.execute("SELECT 1 FROM purchase_orders WHERE status='draft' AND (','||lines||',') LIKE ?", (f"%,{pid}x%",)).fetchone():
                reject("purchase order", 409, f"part {pid} is already on a draft purchase order; not creating a duplicate")
        for ln in body.lines:
            p = c.execute("SELECT unit_cost FROM parts WHERE id=?", (ln.part_id,)).fetchone()
            if not p:
                raise HTTPException(404, f"part {ln.part_id} not found")
            if ln.quantity <= 0:
                raise HTTPException(422, "quantity must be positive")
            total += p["unit_cost"] * ln.quantity
        cur = c.execute("INSERT INTO purchase_orders(lines,total,status,note) VALUES(?,?,?,?)",
                        (",".join(f"{l.part_id}x{l.quantity}" for l in body.lines), round(total, 2), "draft", body.note))
        return {"id": cur.lastrowid, "status": "draft", "total": round(total, 2)}


@app.get("/fleet/summary", operation_id="fleet_summary", summary="Raw fleet counts: vehicles by status, open alerts by severity, low-stock parts")
def fleet_summary():
    return {
        "vehicles_by_status": rows("SELECT status, COUNT(*) n FROM vehicles GROUP BY status"),
        "open_alerts_by_severity": rows("SELECT severity, COUNT(*) n FROM alerts WHERE status='open' GROUP BY severity"),
        "low_stock_parts": rows("SELECT COUNT(*) n FROM parts WHERE stock <= reorder_point")[0]["n"],
        "draft_work_orders": rows("SELECT COUNT(*) n FROM work_orders WHERE status='draft'")[0]["n"],
        "work_orders_needing_review": rows("SELECT COUNT(*) n FROM work_orders WHERE needs_human_review=1")[0]["n"],
        "draft_purchase_orders": rows("SELECT COUNT(*) n FROM purchase_orders WHERE status='draft'")[0]["n"],
        "draft_purchase_total": rows("SELECT COALESCE(SUM(total),0) n FROM purchase_orders WHERE status='draft'")[0]["n"],
    }


@app.get("/dashboard/data", include_in_schema=False)
def dashboard_data():
    with get_conn() as c:
        al = [dict(r) for r in c.execute("SELECT a.*, v.plate, v.model, v.region FROM alerts a JOIN vehicles v ON v.id=a.vehicle_id ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, a.id")]
        for a in al:
            _, ranked = _rank_centers(c, a["id"])
            a["recommended"] = ranked[0] if ranked else None
        wo = [dict(r) for r in c.execute("SELECT w.*, s.name center, s.region center_region FROM work_orders w JOIN service_centers s ON s.id=w.service_center_id ORDER BY w.id")]
    return {"summary": fleet_summary(), "alerts": al, "work_orders": wo, "parts": list_parts(False), "purchase_orders": list_purchase_orders(), "rejections": rows("SELECT * FROM rejections ORDER BY id DESC LIMIT 6"), "activity": rows("SELECT * FROM activity ORDER BY id DESC LIMIT 9")}


from fastapi.responses import HTMLResponse
from pathlib import Path


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return (Path(__file__).parent / "dashboard.html").read_text()
