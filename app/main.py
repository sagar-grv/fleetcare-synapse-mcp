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
    needs_human_review: bool = False
    review_reason: Optional[str] = None


@app.post("/work-orders", operation_id="create_work_order_draft", summary="Create a DRAFT work order for an alert at a service center")
def create_work_order(body: WorkOrderIn):
    with get_conn() as c:
        if not c.execute("SELECT 1 FROM alerts WHERE id=?", (body.alert_id,)).fetchone():
            raise HTTPException(404, "alert not found")
        if not c.execute("SELECT 1 FROM service_centers WHERE id=?", (body.service_center_id,)).fetchone():
            raise HTTPException(404, "service center not found")
        cur = c.execute(
            "INSERT INTO work_orders(alert_id,service_center_id,summary,status,needs_human_review,review_reason) VALUES(?,?,?,?,?,?)",
            (body.alert_id, body.service_center_id, body.summary, "draft", int(body.needs_human_review), body.review_reason))
        return {"id": cur.lastrowid, "status": "draft"}


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
    }
