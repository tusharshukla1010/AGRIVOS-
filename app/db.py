"""AGRIVOS database layer — SQLite via stdlib sqlite3.

Schema: users, centres, slots, bookings, notifications, procurement_stages.
A demo dataset (3 centres, today's slots, sample bookings + queue) is seeded
on first run so the app is immediately explorable.
"""
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from passlib.hash import pbkdf2_sha256  # type: ignore

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "agrivos.db")
DB_PATH = str(Path(DB_PATH).resolve())

# ---------------------------------------------------------------- helpers
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def query(sql: str, params=()):
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def execute(sql: str, params=()):
    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def hash_password(pw: str) -> str:
    return pbkdf2_sha256.hash(pw)


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return pbkdf2_sha256.verify(pw, hashed)
    except Exception:
        return False


# ---------------------------------------------------------------- schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    phone         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'farmer',   -- farmer | admin
    language      TEXT NOT NULL DEFAULT 'en',
    village       TEXT DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS centres (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    district    TEXT NOT NULL,
    crop        TEXT NOT NULL,
    open_time   TEXT NOT NULL,          -- '09:00'
    close_time  TEXT NOT NULL,          -- '17:00'
    capacity_per_slot INTEGER NOT NULL, -- farmers served per slot
    avg_service_min    REAL   NOT NULL  -- historical avg minutes per farmer
);

CREATE TABLE IF NOT EXISTS slots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    centre_id  INTEGER NOT NULL REFERENCES centres(id),
    slot_date  TEXT NOT NULL,           -- 'YYYY-MM-DD'
    start_time TEXT NOT NULL,           -- '10:00'
    end_time   TEXT NOT NULL,           -- '11:00'
    status     TEXT NOT NULL DEFAULT 'open'   -- open | closed
);

CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token        TEXT NOT NULL UNIQUE,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    slot_id      INTEGER NOT NULL REFERENCES slots(id),
    centre_id    INTEGER NOT NULL REFERENCES centres(id),
    crop         TEXT NOT NULL,
    quantity_kg  REAL NOT NULL,
    vehicle_no   TEXT DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'booked',
    -- booked | arrived | weighment | quality_check | payment | completed | cancelled
    queue_no     INTEGER NOT NULL,
    booked_at    TEXT NOT NULL DEFAULT (datetime('now')),
    arrived_at   TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    title      TEXT NOT NULL,
    message    TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'info',   -- info | alert | success
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    read       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES users(id),
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   INTEGER,
    previous_value TEXT,
    new_value   TEXT,
    reason      TEXT DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    centre_id   INTEGER REFERENCES centres(id),
    severity    TEXT NOT NULL,
    title       TEXT NOT NULL,
    message     TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Demo accounts (created only if DB is empty)
DEMO_FARMER = {"phone": "9000000001", "password": "farmer123", "name": "Ramesh Patil"}
DEMO_ADMIN  = {"phone": "9000000002", "password": "admin123",  "name": "Centre Officer"}

CROPS = ["Paddy", "Wheat", "Cotton", "Soybean", "Turmeric", "Maize"]

CENTRES = [
    ("Krushi Bazaar Procurement Centre", "Nashik", "Paddy",   "09:00", "17:00", 12, 7.5),
    ("APMC Mandi Godown No. 3",          "Pune",   "Wheat",   "08:30", "16:30", 15, 6.5),
    ("Taluka Co-op Society Centre",      "Nagpur", "Cotton",  "09:30", "15:30", 10, 9.0),
]


def seed_if_empty():
    conn = get_conn()
    try:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
            return
        cur = conn.cursor()
        cur.executescript(SCHEMA)

        # users ------------------------------------------------------
        cur.execute(
            "INSERT INTO users (name, phone, password_hash, role, village) VALUES (?,?,?,?,?)",
            (DEMO_FARMER["name"], DEMO_FARMER["phone"],
             hash_password(DEMO_FARMER["password"]), "farmer", "Sinnar"),
        )
        farmer_id = cur.lastrowid
        cur.execute(
            "INSERT INTO users (name, phone, password_hash, role) VALUES (?,?,?,?)",
            (DEMO_ADMIN["name"], DEMO_ADMIN["phone"],
             hash_password(DEMO_ADMIN["password"]), "admin"),
        )

        # centres + slots (today & tomorrow) --------------------------
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()

        def add_hours(hhmm, hours):
            h, m = map(int, hhmm.split(":"))
            t = h * 60 + m + int(hours * 60)
            return f"{t // 60:02d}:{t % 60:02d}"

        centre_ids = {}
        for (name, district, crop, open_t, close_t, cap, svc) in CENTRES:
            cur.execute(
                """INSERT INTO centres
                   (name, district, crop, open_time, close_time, capacity_per_slot, avg_service_min)
                   VALUES (?,?,?,?,?,?,?)""",
                (name, district, crop, open_t, close_t, cap, svc),
            )
            cid = cur.lastrowid
            centre_ids[cid] = (crop, cap)

            for day in (today, tomorrow):
                t = open_t
                while t < close_t:
                    cur.execute(
                        "INSERT INTO slots (centre_id, slot_date, start_time, end_time) VALUES (?,?,?,?)",
                        (cid, day, t, add_hours(t, 1)),
                    )
                    # advance 1 hour (slot length), skip lunch 13:00–14:00
                    h, m = map(int, t.split(":"))
                    t = f"{h + 1:02d}:{m:02d}"
                    if t == "13:00":
                        t = "14:00"

        # bookings: demo queue for today at each centre ----------------
        demo_farmers = [
            ("Suresh Shinde", "9000010001"), ("Anita Kale", "9000010002"),
            ("Vikas Jadhav", "9000010003"), ("Meera Chavan", "9000010004"),
        ]
        n = 0
        for cid, (crop, _) in centre_ids.items():
            slots_today = conn.execute(
                "SELECT id FROM slots WHERE centre_id=? AND slot_date=? ORDER BY start_time",
                (cid, today),
            ).fetchall()
            if not slots_today:
                continue
            slot_id = slots_today[0]["id"]
            cur.execute("SELECT COUNT(*) FROM bookings WHERE slot_id=?", (slot_id,))
            base = cur.fetchone()[0]
            for i, (nm, ph) in enumerate(demo_farmers):
                cur.execute(
                    "INSERT OR IGNORE INTO users (name, phone, password_hash, role) VALUES (?,?,?,?)",
                    (nm, ph, hash_password("demo123"), "farmer"),
                )
                uid = cur.execute("SELECT id FROM users WHERE phone=?", (ph,)).fetchone()["id"]
                n += 1
                status = "arrived" if i == 0 else "booked"
                cur.execute(
                    """INSERT INTO bookings
                       (token, user_id, slot_id, centre_id, crop, quantity_kg, vehicle_no,
                        status, queue_no, arrived_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (f"AGV-{n:04d}", uid, slot_id, cid, crop, 40 + n * 5,
                     f"MH{15 + n}AB{1000 + n}", status, base + i + 1,
                     datetime.now().isoformat(timespec="minutes") if i == 0 else None),
                )

        # welcome notification for demo farmer -------------------------
        cur.execute(
            "INSERT INTO notifications (user_id, title, message, kind) VALUES (?,?,?,?)",
            (farmer_id, "Welcome to AGRIVOS",
             "Book a slot at your nearest procurement centre and track your queue live.", "info"),
        )
        conn.commit()
    finally:
        conn.close()


def ensure_recent_slots():
    """Keep the demo alive day after day: create today's + tomorrow's slots
    for every centre if they are missing (seed data is date-fixed)."""
    conn = get_conn()
    try:
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        for c in conn.execute("SELECT id, open_time, close_time FROM centres").fetchall():
            for day in (today, tomorrow):
                if conn.execute(
                    "SELECT 1 FROM slots WHERE centre_id=? AND slot_date=? LIMIT 1",
                    (c["id"], day),
                ).fetchone():
                    continue
                def add_hours(hhmm, hours):
                    h, m = map(int, hhmm.split(":"))
                    t = h * 60 + m + int(hours * 60)
                    return f"{t // 60:02d}:{t % 60:02d}"
                t = c["open_time"]
                while t < c["close_time"]:
                    conn.execute(
                        "INSERT INTO slots (centre_id, slot_date, start_time, end_time) VALUES (?,?,?,?)",
                        (c["id"], day, t, add_hours(t, 1)),
                    )
                    h, m = map(int, t.split(":"))
                    t = f"{h + 1:02d}:{m:02d}"
                    if t == "13:00":
                        t = "14:00"
        conn.commit()
    finally:
        conn.close()


def init_db():
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
    seed_if_empty()
    ensure_recent_slots()


if __name__ == "__main__":
    init_db()
    print("DB ready at", DB_PATH)
