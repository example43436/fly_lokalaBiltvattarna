from flask import Flask, jsonify, request, send_from_directory
import sqlite3
import csv
import os
import json
import hashlib
import secrets
import urllib.request
import urllib.parse
import base64
from datetime import datetime
from contextlib import contextmanager

BASE_DIR = os.path.dirname(__file__)
app = Flask(__name__)

# ── config ────────────────────────────────────────────────────────────────────
DATA_DIR = os.environ.get("APP_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_FILE  = os.environ.get("APP_DB_FILE",  os.path.join(DATA_DIR, "sparkwash.db"))
CSV_FILE = os.environ.get("APP_CSV_FILE", os.path.join(DATA_DIR, "bookings.csv"))

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# 46elks credentials — set via:
#   fly secrets set ELKS_API_USERNAME=u... ELKS_API_PASSWORD=... ELKS_FROM=SparkWash
ELKS_API_USERNAME = os.environ.get("ELKS_API_USERNAME", "udc7b14e696f632e40208132155347b50")
ELKS_API_PASSWORD = os.environ.get("ELKS_API_PASSWORD", "Agg0ac!!4s")
ELKS_FROM         = os.environ.get("ELKS_FROM", "LB")  # max 11 chars, no spaces

# Customers must cancel at least this many minutes before the booking start
CANCEL_WINDOW_MINUTES = 60

os.makedirs(DATA_DIR, exist_ok=True)

# ── database setup ────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
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

            CREATE TABLE IF NOT EXISTS users (
                id         TEXT PRIMARY KEY,
                email      TEXT NOT NULL UNIQUE,
                password   TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bookings (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                phone         TEXT NOT NULL,
                address       TEXT NOT NULL,
                district      TEXT NOT NULL DEFAULT '',
                service       TEXT NOT NULL,
                vehicle_type  TEXT NOT NULL DEFAULT '',
                date          TEXT NOT NULL,
                time          TEXT NOT NULL,
                notes         TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL,
                user_id       TEXT,
                guest_token   TEXT
            );
        """)
        # Safe migrations for existing databases
        for col, definition in [
            ("user_id",      "TEXT"),
            ("guest_token",  "TEXT"),
            ("vehicle_type", "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                db.execute(f"ALTER TABLE bookings ADD COLUMN {col} {definition}")
            except Exception:
                pass

def migrate_from_json():
    """One-time import of old JSON data into SQLite."""
    legacy_data     = os.path.join(DATA_DIR, "app-data.json")
    legacy_slots    = os.path.join(DATA_DIR, "slots.json")
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
        return

    with get_db() as db:
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
                   (id, name, phone, address, district, service, vehicle_type,
                    date, time, notes, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (b["id"], b["name"], b["phone"], b["address"],
                 b.get("district", ""), b["service"], b.get("vehicle_type", ""),
                 b["date"], b["time"], b.get("notes", ""),
                 b.get("created_at", datetime.now().isoformat()))
            )

# ── SMS via 46elks ────────────────────────────────────────────────────────────

def send_sms(to_number: str, message: str):
    """Send an SMS via 46elks. Logs on failure but never crashes the caller."""
    if not ELKS_API_USERNAME or not ELKS_API_PASSWORD:
        app.logger.warning("46elks credentials not configured — SMS skipped.")
        return

    # Normalise Swedish mobile numbers: 07X → +467X
    number = to_number.strip().replace(" ", "").replace("-", "")
    if number.startswith("0"):
        number = "+46" + number[1:]

    payload = urllib.parse.urlencode({
        "from":    ELKS_FROM,
        "to":      number,
        "message": message,
    }).encode()

    req = urllib.request.Request(
        "https://api.46elks.com/a1/sms",
        data=payload,
        method="POST",
    )
    credentials = f"{ELKS_API_USERNAME}:{ELKS_API_PASSWORD}"
    req.add_header("Authorization", "Basic " + base64.b64encode(credentials.encode()).decode())

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            app.logger.info(f"46elks: {resp.status} {resp.read()[:120]}")
    except Exception as exc:
        app.logger.error(f"46elks SMS failed: {exc}")


def build_confirmation_sms(booking: dict) -> str:
    vehicle = f" ({booking['vehicle_type']})" if booking.get("vehicle_type") else ""
    return (
        f"Hej {booking['name']}! Din biltvätt är bokad ✅\n"
        f"Datum: {booking['date']} kl {booking['time']}\n"
        f"Tjänst: {booking['service']}{vehicle}\n"
        f"Adress: {booking['address']}\n"
        f"Boknings-ID: {booking['id']}\n"
        f"Frågor? Ring 070-123 45 67"
    )

# ── helpers ───────────────────────────────────────────────────────────────────

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def export_csv():
    with get_db() as db:
        rows = db.execute("SELECT * FROM bookings ORDER BY date, time").fetchall()
    if not rows:
        return
    keys = ["id", "name", "phone", "address", "district", "service", "vehicle_type",
            "date", "time", "notes", "created_at", "user_id", "guest_token"]
    with open(CSV_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([dict(r) for r in rows])

def minutes_until_booking(date_str: str, time_str: str) -> float:
    """Minutes remaining until the booking starts (negative = already past)."""
    booking_dt = datetime.fromisoformat(f"{date_str}T{time_str}:00")
    return (booking_dt - datetime.now()).total_seconds() / 60

# ── static ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

# ── user auth API ─────────────────────────────────────────────────────────────

@app.route("/api/user/register", methods=["POST"])
def register():
    data     = request.json or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    user_id = f"U{secrets.token_hex(8)}"
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO users (id, email, password, created_at) VALUES (?,?,?,?)",
                (user_id, email, hash_password(password), datetime.now().isoformat())
            )
    except sqlite3.IntegrityError:
        return jsonify({"error": "An account with this email already exists"}), 409

    return jsonify({"success": True, "user_id": user_id, "email": email})

@app.route("/api/user/login", methods=["POST"])
def user_login():
    data     = request.json or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    with get_db() as db:
        user = db.execute(
            "SELECT * FROM users WHERE email=? AND password=?",
            (email, hash_password(password))
        ).fetchone()

    if not user:
        return jsonify({"error": "Invalid email or password"}), 401

    return jsonify({"success": True, "user_id": user["id"], "email": user["email"]})

# ── bookings for logged-in users / guests ─────────────────────────────────────

@app.route("/api/my-bookings", methods=["GET"])
def get_my_bookings():
    user_id     = request.args.get("user_id")
    guest_token = request.args.get("guest_token")

    if not user_id and not guest_token:
        return jsonify({"error": "user_id or guest_token required"}), 400

    with get_db() as db:
        if user_id:
            rows = db.execute(
                "SELECT * FROM bookings WHERE user_id=? ORDER BY date, time", (user_id,)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM bookings WHERE guest_token=? ORDER BY date, time", (guest_token,)
            ).fetchall()

    result = []
    for r in rows:
        b = dict(r)
        b["can_cancel"] = minutes_until_booking(b["date"], b["time"]) >= CANCEL_WINDOW_MINUTES
        result.append(b)

    return jsonify(result)

@app.route("/api/my-bookings/<booking_id>", methods=["DELETE"])
def delete_my_booking(booking_id):
    data        = request.json or {}
    user_id     = data.get("user_id")
    guest_token = data.get("guest_token")

    if not user_id and not guest_token:
        return jsonify({"error": "user_id or guest_token required"}), 400

    with get_db() as db:
        if user_id:
            row = db.execute(
                "SELECT * FROM bookings WHERE id=? AND user_id=?", (booking_id, user_id)
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM bookings WHERE id=? AND guest_token=?", (booking_id, guest_token)
            ).fetchone()

        if not row:
            return jsonify({"error": "Booking not found or not yours"}), 404

        mins_left = minutes_until_booking(row["date"], row["time"])
        if mins_left < 0:
            return jsonify({"error": "Denna bokning har redan passerat."}), 400
        if mins_left < CANCEL_WINDOW_MINUTES:
            return jsonify({
                "error": f"Avbokning måste göras minst {CANCEL_WINDOW_MINUTES} minuter innan bokad tid."
            }), 400

        db.execute("DELETE FROM bookings WHERE id=?", (booking_id,))

    export_csv()
    return jsonify({"success": True})

# ── slots API ─────────────────────────────────────────────────────────────────

@app.route("/api/slots", methods=["GET"])
def get_slots():
    with get_db() as db:
        rows = db.execute(
            "SELECT date, time, capacity, district FROM slots ORDER BY date, time"
        ).fetchall()

    slots = {}
    for r in rows:
        slots.setdefault(r["date"], []).append({
            "time":     r["time"],
            "capacity": r["capacity"],
            "district": r["district"],
        })
    return jsonify(slots)

@app.route("/api/slots", methods=["POST"])
def set_slots():
    data = request.json or {}
    if data.get("admin_password") != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403

    new_slots = data.get("slots", {})

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
    data     = request.json or {}
    required = ["name", "phone", "address", "service", "date", "time"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"Missing field: {field}"}), 400

    date         = data["date"]
    time         = data["time"]
    vehicle_type = (data.get("vehicle_type") or "").strip()
    user_id      = data.get("user_id") or None
    guest_token  = data.get("guest_token") or None

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
               (id, name, phone, address, district, service, vehicle_type,
                date, time, notes, created_at, user_id, guest_token)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (booking_id, data["name"], data["phone"], data["address"],
             slot["district"], data["service"], vehicle_type,
             date, time, data.get("notes", ""),
             datetime.now().isoformat(), user_id, guest_token)
        )

    export_csv()

    # Fire-and-forget SMS confirmation
    send_sms(data["phone"], build_confirmation_sms({
        "id":           booking_id,
        "name":         data["name"],
        "phone":        data["phone"],
        "address":      data["address"],
        "service":      data["service"],
        "vehicle_type": vehicle_type,
        "date":         date,
        "time":         time,
    }))

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
                "time":      s["time"],
                "capacity":  s["capacity"],
                "district":  s["district"],
                "booked":    taken,
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

init_db()
migrate_from_json()
