import os, sqlite3
DB_PATH = os.environ.get("FLEETCARE_DB", os.path.join(os.path.dirname(__file__), "..", "fleet.db"))


def get_conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return _Ctx(c)


class _Ctx:
    def __init__(self, c): self.c = c
    def __enter__(self): return self.c
    def __exit__(self, *a):
        self.c.commit(); self.c.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles(id INTEGER PRIMARY KEY, plate TEXT, model TEXT, type TEXT, region TEXT, status TEXT, mileage INTEGER);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, vehicle_id INTEGER, code TEXT, description TEXT, severity TEXT, status TEXT, needed_specialty TEXT);
CREATE TABLE IF NOT EXISTS parts(id INTEGER PRIMARY KEY, name TEXT, stock INTEGER, reorder_point INTEGER, reorder_qty INTEGER, unit_cost REAL, supplier TEXT);
CREATE TABLE IF NOT EXISTS service_centers(id INTEGER PRIMARY KEY, name TEXT, region TEXT, specialties TEXT, capacity_open INTEGER);
CREATE TABLE IF NOT EXISTS work_orders(id INTEGER PRIMARY KEY, alert_id INTEGER, service_center_id INTEGER, summary TEXT, status TEXT, needs_human_review INTEGER, review_reason TEXT);
CREATE TABLE IF NOT EXISTS purchase_orders(id INTEGER PRIMARY KEY, lines TEXT, total REAL, status TEXT, note TEXT);
"""

VEHICLES = [
    (1, "MH12-AB-1001", "Tata Ace", "van", "Pune", "active", 48210),
    (2, "MH12-AB-1002", "Tata Ace", "van", "Pune", "active", 91400),
    (3, "MH14-CD-2001", "Eicher Pro 2095", "truck", "Mumbai", "active", 120500),
    (4, "MH14-CD-2002", "Eicher Pro 2095", "truck", "Mumbai", "in_service", 133900),
    (5, "KA01-EF-3001", "Ashok Leyland Dost", "van", "Bengaluru", "active", 67300),
    (6, "KA01-EF-3002", "Ashok Leyland Dost", "van", "Bengaluru", "active", 15200),
]
ALERTS = [
    (1, 3, "BRAKE_PAD_WEAR", "Front brake pads below 15% life", "high", "open", "brakes"),
    (2, 2, "OIL_CHANGE_DUE", "Oil change overdue by 1,200 km", "low", "open", "general"),
    (3, 5, "COOLANT_TEMP", "Engine coolant temperature spikes", "critical", "open", "engine"),
    (4, 1, "TYRE_PRESSURE", "Rear-left tyre pressure low", "medium", "open", "tyres"),
    (5, 4, "BATTERY_VOLT", "Battery voltage low on cold start", "medium", "open", "electrical"),
    (6, 6, "FILTER_CHANGE", "Air filter service reminder", "low", "closed", "general"),
]
PARTS = [
    (1, "Brake pad set (front)", 2, 6, 12, 1450.0, "AutoSpares Pune"),
    (2, "Engine oil 5L", 40, 20, 30, 1900.0, "LubeCo"),
    (3, "Coolant 5L", 3, 8, 16, 650.0, "LubeCo"),
    (4, "Tyre 185R14", 9, 8, 8, 4200.0, "TyreHub"),
    (5, "Battery 12V 65Ah", 1, 4, 6, 5600.0, "PowerCell"),
    (6, "Air filter", 15, 10, 20, 380.0, "AutoSpares Pune"),
]
CENTERS = [
    (1, "Pune Quick Service", "Pune", "general,tyres", 5),
    (2, "Mumbai Heavy Garage", "Mumbai", "brakes,engine,general", 2),
    (3, "Mumbai Electricals", "Mumbai", "electrical", 1),
    (4, "Bengaluru Auto Care", "Bengaluru", "general,tyres,electrical", 4),
]


def init_db():
    with get_conn() as c:
        c.executescript(SCHEMA)
        if not c.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]:
            c.executemany("INSERT INTO vehicles VALUES(?,?,?,?,?,?,?)", VEHICLES)
            c.executemany("INSERT INTO alerts VALUES(?,?,?,?,?,?,?)", ALERTS)
            c.executemany("INSERT INTO parts VALUES(?,?,?,?,?,?,?)", PARTS)
            c.executemany("INSERT INTO service_centers VALUES(?,?,?,?,?)", CENTERS)
