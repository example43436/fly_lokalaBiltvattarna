from flask import Flask, jsonify, request, send_from_directory
import sqlite3
import csv
import os
import json
from datetime import datetime
from contextlib import contextmanager

BASE_DIR = os.path.dirname(__file__)
app = Flask(__name__)

# ── config ────────────────────────────────────────────────────────────────────
# On Fly.io, mount your volume at /data and set APP_DATA_DIR=/data
# Locally it just uses ./data/ as before
DATA_DIR = os.environ.get("APP_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_FILE  = os.environ.get("APP_DB_FILE",  os.path.join(DATA_DIR, "sparkwash.db"))
CSV_FILE = os.environ.get("APP_CSV_FILE", os.path.join(DATA_DIR, "bookings.csv"))

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")  # set via env var in production

os.makedirs(DATA_DIR, exist_ok=True)

# ── database setup ────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row          # rows behave like dicts
    conn.execute("PRAGMA journal_mode=WAL") # safe for concurrent reads
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS slots (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                date      TEXT    NOT NULL,
                time      TEXT    NOT NULL,
                capacity  INTEGER NOT NULL DEFAULT 1,
                district  TEXT    NOT NULL DEFAULT '',
                UNIQUE(date, time)
            );

            CREATE TABLE IF NOT EXISTS bookings (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                phone      TEXT NOT NULL,
                address    TEXT NOT NULL,
                district   TEXT NOT NULL DEFAULT '',
                service    TEXT NOT NULL,
                date       TEXT NOT NULL,
                time       TEXT NOT NULL,
                notes      TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
        """)

def migrate_from_json():
    """One-time import of old JSON data into SQLite. Safe to call repeatedly."""
    legacy_data = os.path.join(DATA_DIR, "app-data.json")
    legacy_slots = os.path.join(DATA_DIR, "slots.json")
    legacy_bookings = os.path.join(DATA_DIR, "bookings.json")

    data = {"slots": {}, "bookings": []}

    if os.path.exists(legacy_data):
        with open(legacy_data) as f:
            data = json.load(f)
    elif os.path.exists(legacy_slots):
        with open(legacy_slots) as f:
            data["slots"] = json.load(f)
        if os.path.exists(legacy_bookings):
            with open(legacy_bookings) as f:
                data["bookings"] = json.load(f)
    else:
        return  # nothing to migrate

    with get_db() as db:
        # Only migrate if tables are empty
        if db.execute("SELECT COUNT(*) FROM slots").fetchone()[0] > 0:
            return

        for date, slot_list in data["slots"].items():
            for s in slot_list:
                db.execute(
                    "INSERT OR IGNORE INTO slots (date, time, capacity, district) VALUES (?,?,?,?)",
                    (date, s["time"], s.get("capacity", 1), s.get("district", ""))
                )

        for b in data["bookings"]:
            db.execute(
                """INSERT OR IGNORE INTO bookings
                   (id, name, phone, address, district, service, date, time, notes, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (b["id"], b["name"], b["phone"], b["address"],
                 b.get("district",""), b["service"], b["date"], b["time"],
                 b.get("notes",""), b.get("created_at", datetime.now().isoformat()))
            )

# ── helpers ───────────────────────────────────────────────────────────────────

def booking_to_dict(row):
    return dict(row)

def export_csv():
    with get_db() as db:
        rows = db.execute("SELECT * FROM bookings ORDER BY date, time").fetchall()
    if not rows:
        return
    keys = ["id","name","phone","address","district","service","date","time","notes","created_at"]
    with open(CSV_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([dict(r) for r in rows])

# ── static ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

# ── slots API ─────────────────────────────────────────────────────────────────

@app.route("/api/slots", methods=["GET"])
def get_slots():
    """Returns slots grouped by date, same shape as before: {date: [{time, capacity, district}]}"""
    with get_db() as db:
        rows = db.execute("SELECT date, time, capacity, district FROM slots ORDER BY date, time").fetchall()

    slots = {}
    for r in rows:
        slots.setdefault(r["date"], []).append({
            "time": r["time"],
            "capacity": r["capacity"],
            "district": r["district"],
        })
    return jsonify(slots)

@app.route("/api/slots", methods=["POST"])
def set_slots():
    """Replace all slots for the submitted dates (admin only)."""
    data = request.json or {}
    if data.get("admin_password") != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403

    new_slots = data.get("slots", {})  # {date: [{time, capacity, district}]}

    with get_db() as db:
        for date, slot_list in new_slots.items():
            db.execute("DELETE FROM slots WHERE date = ?", (date,))
            for s in slot_list:
                db.execute(
                    "INSERT INTO slots (date, time, capacity, district) VALUES (?,?,?,?)",
                    (date, s["time"], s.get("capacity", 1), s.get("district", ""))
                )

    return jsonify({"success": True})

# ── bookings API ──────────────────────────────────────────────────────────────

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.json or {}
    if data.get("admin_password") != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    return jsonify({"success": True})

@app.route("/api/bookings", methods=["GET"])
def get_bookings():
    if request.args.get("admin_password") != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    with get_db() as db:
        rows = db.execute("SELECT * FROM bookings ORDER BY date, time").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/bookings", methods=["POST"])
def create_booking():
    data = request.json or {}
    required = ["name", "phone", "address", "service", "date", "time"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"Missing field: {field}"}), 400

    date, time = data["date"], data["time"]

    with get_db() as db:
        slot = db.execute(
            "SELECT * FROM slots WHERE date=? AND time=?", (date, time)
        ).fetchone()

        if not slot:
            return jsonify({"error": "This time slot is not available"}), 400

        taken = db.execute(
            "SELECT COUNT(*) FROM bookings WHERE date=? AND time=?", (date, time)
        ).fetchone()[0]

        if taken >= slot["capacity"]:
            return jsonify({"error": "This time slot is fully booked"}), 400

        booking_id = f"BK{datetime.now().strftime('%Y%m%d%H%M%S%f')[:18]}"
        db.execute(
            """INSERT INTO bookings
               (id, name, phone, address, district, service, date, time, notes, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (booking_id, data["name"], data["phone"], data["address"],
             slot["district"], data["service"], date, time,
             data.get("notes", ""), datetime.now().isoformat())
        )

    export_csv()
    return jsonify({"success": True, "booking_id": booking_id})

@app.route("/api/bookings/<booking_id>", methods=["DELETE"])
def delete_booking(booking_id):
    data = request.json or {}
    if data.get("admin_password") != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    with get_db() as db:
        db.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    export_csv()
    return jsonify({"success": True})

@app.route("/api/availability", methods=["GET"])
def get_availability():
    date = request.args.get("date")
    if not date:
        return jsonify({"error": "date param required"}), 400

    with get_db() as db:
        slots = db.execute(
            "SELECT * FROM slots WHERE date=? ORDER BY time", (date,)
        ).fetchall()

        result = []
        for s in slots:
            taken = db.execute(
                "SELECT COUNT(*) FROM bookings WHERE date=? AND time=?", (date, s["time"])
            ).fetchone()[0]
            result.append({
                "time": s["time"],
                "capacity": s["capacity"],
                "district": s["district"],
                "booked": taken,
                "available": s["capacity"] - taken,
            })

    return jsonify(result)

@app.route("/api/csv", methods=["GET"])
def download_csv():
    if request.args.get("admin_password") != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403
    export_csv()
    return send_from_directory(DATA_DIR, os.path.basename(CSV_FILE), as_attachment=True)

# ── startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    migrate_from_json()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

# Gunicorn entrypoint (called by Procfile / Fly.io)
init_db()
migrate_from_json()
