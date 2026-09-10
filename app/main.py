"""AGRIVOS API — routes for auth, centres, slots, bookings, queue, admin."""
import os
import random
import json
import urllib.request
from datetime import date, datetime

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from . import db
from .ai import PREDICTOR
from .auth import current_user, make_token, require_admin

load_dotenv()

app = FastAPI(title="AGRIVOS API", version="1.0.0")


@app.middleware("http")
async def enforce_utf8(request, call_next):
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if (content_type.startswith("text/") or content_type.startswith("application/json")) \
            and "charset=" not in content_type.lower():
        response.headers["content-type"] = f"{content_type}; charset=utf-8"
    return response

# serve frontend (single-page app)
STATIC_DIR = __file__.replace("main.py", "") + "static"


@app.on_event("startup")
def startup():
    db.init_db()


# ================================================================ schemas
class RegisterIn(BaseModel):
    name: str
    phone: str
    password: str
    village: str = ""
    language: str = "en"


class LoginIn(BaseModel):
    phone: str
    password: str


class BookIn(BaseModel):
    slot_id: int
    quantity_kg: float = Field(gt=0, le=5000)
    vehicle_no: str = ""


class StageIn(BaseModel):
    status: str  # arrived | weighment | quality_check | payment | completed | cancelled


class SlotAdminIn(BaseModel):
    slot_id: int
    status: str  # open | closed


class CentreIn(BaseModel):
    id: int
    capacity_per_slot: int = Field(ge=1, le=100)
    avg_service_min: float = Field(ge=1, le=60)


class HalAIIn(BaseModel):
    transcript: str = Field(min_length=2, max_length=500)
    context: str = ""


class RecommendationIn(BaseModel):
    quantity_kg: float = Field(gt=0, le=5000)
    slot_date: str = ""


class SimulationIn(BaseModel):
    demand_multiplier: float = Field(ge=0.5, le=2.0, default=1.0)
    counters_delta: int = Field(ge=-5, le=10, default=0)
    service_multiplier: float = Field(ge=0.5, le=2.0, default=1.0)


# ================================================================ auth
@app.post("/api/auth/register")
def register(body: RegisterIn):
    if not body.phone.isdigit() or len(body.phone) < 10:
        raise HTTPException(400, "Enter a valid 10-digit phone number")
    if len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if db.query("SELECT id FROM users WHERE phone=?", (body.phone,)):
        raise HTTPException(409, "Phone number already registered")
    uid = db.execute(
        "INSERT INTO users (name, phone, password_hash, role, village, language) VALUES (?,?,?,?,?,?)",
        (body.name.strip(), body.phone, db.hash_password(body.password),
         "farmer", body.village.strip(), body.language),
    )
    db.execute(
        "INSERT INTO notifications (user_id, title, message, kind) VALUES (?,?,?,?)",
        (uid, "Welcome to AGRIVOS",
         "Book a slot at your nearest procurement centre and track your queue live.", "info"),
    )
    user = db.query("SELECT * FROM users WHERE id=?", (uid,))[0]
    return {"token": make_token(user), "user": _public_user(user)}


@app.post("/api/auth/login")
def login(body: LoginIn):
    users = db.query("SELECT * FROM users WHERE phone=?", (body.phone,))
    if not users or not db.verify_password(body.password, users[0]["password_hash"]):
        raise HTTPException(401, "Invalid phone or password")
    return {"token": make_token(users[0]), "user": _public_user(users[0])}


def _public_user(u):
    return {"id": u["id"], "name": u["name"], "phone": u["phone"],
            "role": u["role"], "village": u["village"], "language": u["language"]}


def _recommend_centres(quantity_kg: float, slot_date: str = "") -> list[dict]:
    """Rank real open slots using queue, capacity, congestion and predicted wait."""
    target_date = slot_date or date.today().isoformat()
    recommendations = []
    for centre in db.query("SELECT * FROM centres ORDER BY name"):
        slots = db.query(
            "SELECT * FROM slots WHERE centre_id=? AND slot_date>=? AND status='open' "
            "ORDER BY slot_date, start_time",
            (centre["id"], target_date),
        )
        if not slots:
            continue
        active = db.query(
            "SELECT COUNT(*) n FROM bookings WHERE centre_id=? AND status NOT IN ('completed','cancelled')",
            (centre["id"],),
        )[0]["n"]
        slot = slots[0]
        booked = db.query(
            "SELECT COUNT(*) n FROM bookings WHERE slot_id=? AND status != 'cancelled'",
            (slot["id"],),
        )[0]["n"]
        available = max(0, centre["capacity_per_slot"] - booked)
        if available <= 0:
            continue
        when = datetime.fromisoformat(f"{slot['slot_date']}T{slot['start_time']}:00")
        wait = PREDICTOR.predict_wait_minutes(
            active, when, centre["capacity_per_slot"], centre["avg_service_min"], active
        )
        congestion = PREDICTOR.predict_congestion(
            when, centre["capacity_per_slot"], active
        )
        recommendations.append({
            "centre_id": centre["id"], "centre_name": centre["name"],
            "district": centre["district"], "crop": centre["crop"],
            "slot_id": slot["id"], "slot_date": slot["slot_date"],
            "start_time": slot["start_time"], "end_time": slot["end_time"],
            "available": available, "estimated_wait_minutes": wait,
            "congestion": congestion, "congestion_label": PREDICTOR.congestion_label(congestion),
            "score": round(wait + (congestion * 15) + (active * 2), 1),
        })
    return sorted(recommendations, key=lambda item: item["score"])


@app.post("/api/ai/recommendations")
def recommendations(body: RecommendationIn, user=Depends(current_user)):
    return {"recommendations": _recommend_centres(body.quantity_kg, body.slot_date)}


def _audit(user_id: int, action: str, entity_type: str, entity_id: int | None,
           previous: str = "", new: str = "", reason: str = ""):
    db.execute(
        """INSERT INTO audit_logs
           (user_id, action, entity_type, entity_id, previous_value, new_value, reason)
           VALUES (?,?,?,?,?,?,?)""",
        (user_id, action, entity_type, entity_id, previous, new, reason),
    )


@app.get("/api/admin/alerts")
def admin_alerts(user=Depends(require_admin)):
    alerts = []
    today = date.today().isoformat()
    for centre in db.query("SELECT * FROM centres ORDER BY name"):
        live = _queue_snapshot(centre["id"], today)
        ratio = live["in_queue"] / max(centre["capacity_per_slot"], 1)
        if ratio >= 1.5:
            severity = "critical"
            title = "Capacity risk"
            message = f"{centre['name']} is carrying {live['in_queue']} active farmers."
            recommendation = "Redirect new bookings to the lowest-load centre."
        elif ratio >= 0.8:
            severity = "warning"
            title = "Queue building"
            message = f"{centre['name']} is approaching its operating capacity."
            recommendation = "Review the next slots and prepare an additional counter."
        else:
            continue
        alerts.append({"centre_id": centre["id"], "centre_name": centre["name"],
                       "severity": severity, "title": title, "message": message,
                       "recommendation": recommendation, "timestamp": datetime.now().isoformat(timespec="minutes")})
    return alerts


@app.post("/api/admin/simulation")
def admin_simulation(body: SimulationIn, user=Depends(require_admin)):
    results = []
    for centre in db.query("SELECT * FROM centres ORDER BY name"):
        live = _queue_snapshot(centre["id"], date.today().isoformat())
        capacity = max(1, centre["capacity_per_slot"] + body.counters_delta * 2)
        projected_queue = round(live["in_queue"] * body.demand_multiplier)
        projected_wait = round(
            projected_queue * centre["avg_service_min"] * body.service_multiplier / capacity, 1
        )
        results.append({"centre_id": centre["id"], "centre_name": centre["name"],
                        "current_queue": live["in_queue"], "projected_queue": projected_queue,
                        "projected_wait_minutes": projected_wait,
                        "status": "critical" if projected_queue > capacity * 1.5 else
                                  "busy" if projected_queue > capacity else "normal"})
    _audit(user["id"], "simulation_run", "simulation", None,
           reason=f"demand={body.demand_multiplier}, counters={body.counters_delta}, service={body.service_multiplier}")
    return {"inputs": body.model_dump(), "results": results}


@app.get("/api/admin/audit")
def admin_audit(user=Depends(require_admin)):
    return db.query(
        """SELECT a.*, COALESCE(u.name, 'System') user_name
           FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id
           ORDER BY a.id DESC LIMIT 100"""
    )


@app.post("/api/hal-ai/parse")
def hal_ai_parse(body: HalAIIn, user=Depends(current_user)):
    """Turn a spoken booking request into structured booking hints."""
    prompt = (
        "You are HAL AI for AGRIVOS. Extract a farmer's slot-booking request. "
        "Return JSON only with keys centre (string or null), date (YYYY-MM-DD or null), "
        "time (HH:MM or null), quantity_kg (number or null), vehicle_no (string or null), "
        "and reply (short confirmation). Do not invent missing values.\n"
        f"Available context: {body.context}\nFarmer said: {body.transcript}"
    )
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        try:
            endpoint = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.0-flash:generateContent?key={api_key}"
            )
            payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
            request = urllib.request.Request(
                endpoint, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=12) as response:
                result = json.loads(response.read())
            text = result["candidates"][0]["content"]["parts"][0]["text"]
            text = text.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(text)
            quantity_kg = float(parsed.get("quantity_kg") or 0)
            ranked = _recommend_centres(quantity_kg) if quantity_kg else []
            return {"source": "gemini", **parsed,
                    "centre": parsed.get("centre") or (ranked[0]["centre_name"] if ranked else None),
                    "recommendations": ranked}
        except (OSError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            pass

    # Keeps voice booking useful in local demos when no Gemini key is configured.
    import re
    quantity = re.search(r"(\d+(?:\.\d+)?)\s*(?:kg|kilo)", body.transcript, re.I)
    vehicle = re.search(r"\b([A-Z]{2}\d{1,2}[A-Z]{1,3}\d{3,4})\b", body.transcript, re.I)
    quantity_kg = float(quantity.group(1)) if quantity else 0
    ranked = _recommend_centres(quantity_kg) if quantity_kg else []
    return {
        "source": "local-fallback",
        "centre": ranked[0]["centre_name"] if ranked else None,
        "date": date.today().isoformat(),
        "time": None,
        "quantity_kg": quantity_kg or None,
        "vehicle_no": vehicle.group(1).upper() if vehicle else None,
        "reply": "I heard your request. Choose a centre and slot to confirm the booking.",
        "recommendations": ranked,
    }


@app.get("/api/me")
def me(user=Depends(current_user)):
    return _public_user(user)


# ================================================================ centres & slots
@app.get("/api/centres")
def list_centres(user=Depends(current_user)):
    today = date.today().isoformat()
    rows = db.query("SELECT * FROM centres ORDER BY name")
    out = []
    for c in rows:
        live = _queue_snapshot(c["id"], today)
        out.append({**c, "live": live})
    return out


@app.get("/api/centres/{centre_id}/slots")
def centre_slots(centre_id: int, user=Depends(current_user)):
    today = date.today().isoformat()
    centre = db.query("SELECT * FROM centres WHERE id=?", (centre_id,))
    if not centre:
        raise HTTPException(404, "Centre not found")
    slots = db.query(
        "SELECT * FROM slots WHERE centre_id=? AND slot_date>=? AND status='open' ORDER BY slot_date, start_time",
        (centre_id, today),
    )
    out = []
    for s in slots:
        booked = db.query(
            "SELECT COUNT(*) n FROM bookings WHERE slot_id=? AND status != 'cancelled'", (s["id"],)
        )[0]["n"]
        left = max(0, centre[0]["capacity_per_slot"] - booked)
        # AI congestion badge for this slot
        hh = int(s["start_time"].split(":")[0])
        when = datetime.fromisoformat(f"{s['slot_date']}T{s['start_time']}:00")
        cong = PREDICTOR.predict_congestion(when, centre[0]["capacity_per_slot"], booked)
        out.append({
            **s,
            "booked": booked, "available": left,
            "congestion": cong,
            "congestion_label": PREDICTOR.congestion_label(cong),
        })
    return out


def _queue_snapshot(centre_id: int, day: str) -> dict:
    """Live queue stats + AI congestion for a centre on the given day."""
    now = datetime.now()
    active = db.query(
        """SELECT b.* FROM bookings b JOIN slots s ON s.id=b.slot_id
           WHERE b.centre_id=? AND s.slot_date=? AND b.status NOT IN ('completed','cancelled')
           ORDER BY b.queue_no""",
        (centre_id, day),
    )
    waiting = [b for b in active if b["status"] != "arrived"]
    centre = db.query("SELECT * FROM centres WHERE id=?", (centre_id,))[0]
    arrivals = db.query(
        "SELECT COUNT(*) n FROM bookings WHERE centre_id=? AND booked_at LIKE ?",
        (centre_id, f"{day}%"),
    )[0]["n"]
    cong = PREDICTOR.predict_congestion(now, centre["capacity_per_slot"], arrivals)
    return {
        "in_queue": len(active),
        "now_serving": next((b["queue_no"] for b in active if b["status"] == "arrived"), None),
        "congestion": cong,
        "congestion_label": PREDICTOR.congestion_label(cong),
    }


# ================================================================ booking & token
@app.post("/api/bookings")
def create_booking(body: BookIn, user=Depends(current_user)):
    if user["role"] != "farmer":
        raise HTTPException(403, "Only farmers can book slots")
    slots = db.query("SELECT * FROM slots WHERE id=?", (body.slot_id,))
    if not slots:
        raise HTTPException(404, "Slot not found")
    slot = slots[0]
    if slot["status"] != "open":
        raise HTTPException(400, "Slot is closed")
    today = date.today().isoformat()
    if slot["slot_date"] < today:
        raise HTTPException(400, "Cannot book a past slot")

    # one active booking per farmer per centre per day (join slots for slot_date)
    dup = db.query(
        """SELECT b.id FROM bookings b JOIN slots s ON s.id=b.slot_id
                     WHERE b.user_id=? AND b.centre_id=? AND s.slot_date=?
                         AND b.status NOT IN ('cancelled', 'completed')""",
        (user["id"], slot["centre_id"], slot["slot_date"]),
    )
    if dup:
        raise HTTPException(409, "You already have a booking at this centre for this day")

    centre = db.query("SELECT * FROM centres WHERE id=?", (slot["centre_id"],))[0]
    booked = db.query(
        "SELECT COUNT(*) n FROM bookings WHERE slot_id=? AND status != 'cancelled'", (slot["id"],)
    )[0]["n"]
    if booked >= centre["capacity_per_slot"]:
        raise HTTPException(409, "Slot is full — pick another slot")

    # queue number: position within the centre's day queue (not per-slot)
    existing = db.query(
        """SELECT COUNT(*) n FROM bookings b JOIN slots s ON s.id=b.slot_id
           WHERE b.centre_id=? AND s.slot_date=? AND b.status != 'cancelled'""",
        (slot["centre_id"], slot["slot_date"]),
    )[0]["n"]
    queue_no = existing + 1
    token = f"AGV-{random.randint(1000, 9999)}"
    while db.query("SELECT id FROM bookings WHERE token=?", (token,)):
        token = f"AGV-{random.randint(1000, 9999)}"

    bid = db.execute(
        """INSERT INTO bookings
           (token, user_id, slot_id, centre_id, crop, quantity_kg, vehicle_no, status, queue_no)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (token, user["id"], slot["id"], slot["centre_id"], centre["crop"],
         body.quantity_kg, body.vehicle_no.strip(), "booked", queue_no),
    )
    db.execute(
        "INSERT INTO notifications (user_id, title, message, kind) VALUES (?,?,?,?)",
        (user["id"], "Slot booked ✓",
         f"Token {token} · {centre['name']} · {slot['slot_date']} {slot['start_time']}–{slot['end_time']}. "
         f"Queue position {queue_no}.", "success"),
    )
    return _booking_detail(bid)


@app.get("/api/bookings")
def my_bookings(user=Depends(current_user)):
    rows = db.query(
        "SELECT id FROM bookings WHERE user_id=? AND status NOT IN ('cancelled', 'completed') ORDER BY id DESC",
        (user["id"],),
    )
    return [_booking_detail(r["id"]) for r in rows]


@app.get("/api/bookings/history")
def booking_history(user=Depends(current_user)):
    rows = db.query(
        "SELECT id FROM bookings WHERE user_id=? AND status IN ('completed', 'cancelled') ORDER BY id DESC",
        (user["id"],),
    )
    return [_booking_detail(r["id"]) for r in rows]


@app.post("/api/bookings/{bid}/cancel")
def cancel_booking(bid: int, user=Depends(current_user)):
    rows = db.query("SELECT * FROM bookings WHERE id=? AND user_id=?", (bid, user["id"]))
    if not rows:
        raise HTTPException(404, "Booking not found")
    if rows[0]["status"] != "booked":
        raise HTTPException(400, "Only bookings not yet arrived can be cancelled")
    db.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
    db.execute(
        "INSERT INTO notifications (user_id, title, message, kind) VALUES (?,?,?,?)",
        (user["id"], "Booking cancelled",
         f"Your token {rows[0]['token']} has been cancelled. You may book another slot.", "alert"),
    )
    return {"ok": True}


def _booking_detail(bid: int) -> dict:
    rows = db.query(
        """SELECT b.*, s.start_time, s.end_time, s.slot_date,
                  c.name centre_name, c.district, c.capacity_per_slot, c.avg_service_min
           FROM bookings b
           JOIN slots s ON s.id = b.slot_id
           JOIN centres c ON c.id = b.centre_id
           WHERE b.id=?""",
        (bid,),
    )
    if not rows:
        raise HTTPException(404, "Booking not found")
    b = rows[0]
    now = datetime.now()

    # queue position = active farmers ahead of me at the centre that day
    ahead = db.query(
        """SELECT COUNT(*) n FROM bookings b JOIN slots s ON s.id=b.slot_id
           WHERE b.centre_id=? AND s.slot_date=? AND b.status NOT IN ('completed','cancelled')
             AND (b.queue_no < ? OR (b.queue_no = ? AND b.id < ?))""",
        (b["centre_id"], b["slot_date"], b["queue_no"], b["queue_no"], b["id"]),
    )[0]["n"]

    eta = None
    if b["status"] in ("booked", "arrived"):
        arrivals = db.query(
            "SELECT COUNT(*) n FROM bookings WHERE centre_id=? AND booked_at LIKE ?",
            (b["centre_id"], f"{b['slot_date']}%"),
        )[0]["n"]
        when = datetime.fromisoformat(f"{b['slot_date']}T{b['start_time']}:00")
        # if slot already started, predict from now
        when = max(when, now)
        eta = PREDICTOR.predict_wait_minutes(
            ahead, when, b["capacity_per_slot"], b["avg_service_min"], arrivals
        )

    STAGES = ["booked", "arrived", "weighment", "quality_check", "payment", "completed"]
    return {
        "id": b["id"], "token": b["token"], "status": b["status"],
        "crop": b["crop"], "quantity_kg": b["quantity_kg"], "vehicle_no": b["vehicle_no"],
        "centre_id": b["centre_id"], "centre_name": b["centre_name"], "district": b["district"],
        "slot_date": b["slot_date"], "start_time": b["start_time"], "end_time": b["end_time"],
        "queue_no": b["queue_no"], "queue_ahead": ahead,
        "eta_minutes": eta,
        "stage_index": STAGES.index(b["status"]) if b["status"] in STAGES else 0,
        "booked_at": b["booked_at"],
    }


# ================================================================ notifications
@app.get("/api/notifications")
def my_notifications(user=Depends(current_user)):
    rows = db.query(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (user["id"],)
    )
    return rows


@app.post("/api/notifications/read")
def mark_read(user=Depends(current_user)):
    db.execute("UPDATE notifications SET read=1 WHERE user_id=?", (user["id"],))
    return {"ok": True}


# ================================================================ admin
@app.get("/api/admin/overview")
def admin_overview(user=Depends(require_admin)):
    today = date.today().isoformat()
    centres = db.query("SELECT * FROM centres ORDER BY name")
    out = []
    for c in centres:
        live = _queue_snapshot(c["id"], today)
        today_b = db.query(
            """SELECT COUNT(*) n, COALESCE(SUM(quantity_kg),0) q FROM bookings b
               JOIN slots s ON s.id=b.slot_id
               WHERE b.centre_id=? AND s.slot_date=? AND b.status != 'cancelled'""",
            (c["id"], today),
        )[0]
        out.append({
            "id": c["id"], "name": c["name"], "district": c["district"], "crop": c["crop"],
            "open_time": c["open_time"], "close_time": c["close_time"],
            "capacity_per_slot": c["capacity_per_slot"], "avg_service_min": c["avg_service_min"],
            "bookings_today": today_b["n"], "qty_today": round(today_b["q"], 1),
            "live": live,
        })
    return out


@app.get("/api/admin/queue/{centre_id}")
def admin_queue(centre_id: int, user=Depends(require_admin)):
    today = date.today().isoformat()
    rows = db.query(
        """SELECT b.*, u.name farmer_name, u.phone, s.start_time, s.end_time, s.slot_date
           FROM bookings b
           JOIN users u ON u.id = b.user_id
           JOIN slots s ON s.id = b.slot_id
           WHERE b.centre_id=? AND s.slot_date=? AND b.status NOT IN ('completed','cancelled')
           ORDER BY b.queue_no""",
        (centre_id, today),
    )
    for r in rows:
        r["eta_minutes"] = None
    out = []
    centre = db.query("SELECT * FROM centres WHERE id=?", (centre_id,))
    service = centre[0]["avg_service_min"] if centre else 7.0
    for i, r in enumerate(rows):
        out.append({
            **r,
            "eta_minutes": round(i * service, 1),  # operator-side rough ETA
        })
    return out


@app.get("/api/admin/centres/{centre_id}/slots")
def admin_centre_slots(centre_id: int, user=Depends(require_admin)):
    """All upcoming slots (open + closed) with booked counts, for the command centre."""
    today = date.today().isoformat()
    rows = db.query(
        "SELECT * FROM slots WHERE centre_id=? AND slot_date>=? ORDER BY slot_date, start_time",
        (centre_id, today),
    )
    for s in rows:
        s["booked"] = db.query(
            "SELECT COUNT(*) n FROM bookings WHERE slot_id=? AND status != 'cancelled'",
            (s["id"],),
        )[0]["n"]
    return rows


@app.post("/api/admin/booking/{bid}/stage")
def set_stage(bid: int, body: StageIn, user=Depends(require_admin)):
    valid = {"arrived", "weighment", "quality_check", "payment", "completed", "cancelled"}
    if body.status not in valid:
        raise HTTPException(400, f"status must be one of {sorted(valid)}")
    rows = db.query(
        """SELECT b.*, u.id uid FROM bookings b JOIN users u ON u.id=b.user_id WHERE b.id=?""", (bid,)
    )
    if not rows:
        raise HTTPException(404, "Booking not found")
    b = rows[0]
    from datetime import datetime as dt
    extra = ""
    params: list = [body.status]
    if body.status == "arrived":
        extra = ", arrived_at=?"
        params.append(dt.now().isoformat(timespec="minutes"))
    if body.status == "completed":
        extra = ", completed_at=?"
        params.append(dt.now().isoformat(timespec="minutes"))
    params.append(bid)
    db.execute(f"UPDATE bookings SET status=?{extra} WHERE id=?", params)
    _audit(user["id"], "booking_stage_changed", "booking", bid,
           previous=b["status"], new=body.status)

    msgs = {
        "arrived": ("Your turn is approaching", f"You are marked ARRIVED. Please be at the gate with token {b['token']}.", "alert"),
        "weighment": ("Weighment in progress", f"Token {b['token']}: your produce is being weighed.", "info"),
        "quality_check": ("Quality check in progress", f"Token {b['token']}: quality check underway.", "info"),
        "payment": ("Payment processing", f"Token {b['token']}: payment is being processed. Check payment stage for details.", "success"),
        "completed": ("Procurement completed ✓", f"Token {b['token']}: procurement completed. Thank you!", "success"),
        "cancelled": ("Booking cancelled", f"Token {b['token']} was cancelled by the centre.", "alert"),
    }
    t, m, k = msgs[body.status]
    db.execute(
        "INSERT INTO notifications (user_id, title, message, kind) VALUES (?,?,?,?)",
        (b["uid"], t, m, k),
    )
    return {"ok": True, "status": body.status}


@app.post("/api/admin/slot")
def set_slot(body: SlotAdminIn, user=Depends(require_admin)):
    if body.status not in ("open", "closed"):
        raise HTTPException(400, "status must be open|closed")
    old = db.query("SELECT status FROM slots WHERE id=?", (body.slot_id,))
    if not old:
        raise HTTPException(404, "Slot not found")
    db.execute("UPDATE slots SET status=? WHERE id=?", (body.status, body.slot_id))
    _audit(user["id"], "slot_status_changed", "slot", body.slot_id,
           previous=old[0]["status"], new=body.status)
    return {"ok": True}


@app.post("/api/admin/centre")
def update_centre(body: CentreIn, user=Depends(require_admin)):
    old = db.query(
        "SELECT capacity_per_slot, avg_service_min FROM centres WHERE id=?", (body.id,)
    )
    if not old:
        raise HTTPException(404, "Centre not found")
    db.execute(
        "UPDATE centres SET capacity_per_slot=?, avg_service_min=? WHERE id=?",
        (body.capacity_per_slot, body.avg_service_min, body.id),
    )
    _audit(user["id"], "centre_settings_changed", "centre", body.id,
           previous=f"capacity={old[0]['capacity_per_slot']}, service={old[0]['avg_service_min']}",
           new=f"capacity={body.capacity_per_slot}, service={body.avg_service_min}")
    return {"ok": True}


# ================================================================ frontend
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/admin")
def admin_frontend():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/admin/insights")
def admin_insights_frontend():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
