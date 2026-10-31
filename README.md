# AGRIVOS — Smart Farmer Procurement & Queue Management System

SIH 2026 · Problem Statement 32 (Agriculture) · Team AGRIVOS

An AI-powered farmer procurement platform for **slot booking, digital tokens, live
queue tracking with predicted wait times, and procurement status visibility** —
built exactly per the deck's proposed solution.

## Quick start

```bash
cd agrivos
python -m venv .venv
.venv\Scripts\activate        # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt

uvicorn app.main:app https://agrivos-vkmh.vercel.app
```

Create a `.env` file in the project root:

```env
GEMINI_API_KEY=your_gemini_api_key_here
AGRIVOS_JWT_SECRET=replace_with_a_long_random_secret
```

Open **https://agrivos-vkmh.vercel.app**

Use **https://agrivos-vkmh.vercel.app** after signing in with the admin demo account to open the operations command centre. Predictive alerts, what-if simulation, and the audit trail are available at **https://agrivos-vkmh.vercel.apps**.

## Deploy to Vercel

This project is configured for Vercel's Python runtime (FastAPI). `server.py` is the
entrypoint and `vercel.json` sets the Python function options.

1. Push the repository to GitHub.
2. Import it at [vercel.com/new](https://vercel.com/new). Vercel detects FastAPI
   automatically — leave the build command and output directory empty.
3. Add these Environment Variables in the Vercel project settings:
   - `AGRIVOS_JWT_SECRET` — a long random string
   - `GEMINI_API_KEY` — optional, enables Gemini-backed HAL AI
4. Deploy.

> **Note on the database:** Vercel's filesystem is read-only except `/tmp`, so the
> app automatically stores `agrivos.db` in `/tmp` when running on Vercel and
> re-seeds the demo data on each cold start. This keeps the demo working, but data
> written by users can be lost when a function instance is recycled. For persistent
> production data, point `AGRIVOS_DB_PATH` at a writable volume or replace SQLite
> with a hosted database (Turso, Neon, Supabase, etc.).

## Demo accounts

| Role   | Phone       | Password  |
|--------|-------------|-----------|
| Farmer | 9000000001  | farmer123 |
| Admin  | 9000000002  | admin123  |

(Registration also works — any new user is a farmer.)

## What's implemented

### Farmer flow (PPT "Detailed explanation")
1. **Register / Login** (JWT auth, phone-based — friendly for farmers)
2. **Select procurement centre / crop** — 3 demo centres with timings, capacity, live queue
3. **View timings, capacity, available slots** — slots for today & tomorrow, with **AI congestion badges** (Calm / Moderate / Busy / Severe)
4. **Book a slot → digital token** (e.g. `AGV-4821`) issued instantly
5. **Track queue position + AI-predicted waiting time (ETA)** — live, refreshes every 15s
6. **Alerts** — notification centre for schedule/queue/procurement updates
7. **Procurement stage tracker** — Booked → Arrived → Weighment → Quality Check → Payment → Completed
8. **Cancel & rebook**, **English / हिंदी toggle** (multi-language, per deck)

### Admin / Centre operator (PPT flow)
- Dashboard with per-centre stats: bookings today, quantity procured (kg), live queue, congestion
- **Queue control**: "Serve next" and "Advance stage" per farmer — each transition
  pushes a notification to that farmer automatically
- Slot open/close, capacity & service-rate settings (feeds the AI model)

### AI model (scikit-learn, per deck)
`app/ai.py` — two RandomForest models trained on first startup (~1s, no external data):
- **Wait-time regressor** — features: queue_ahead, hour, day-of-week, centre capacity,
  avg service minutes, recent arrivals → predicted minutes of waiting
- **Congestion classifier** — same features → 0–3 congestion class used for slot badges
  and the "best time to visit" signal

Rush-hour (opening & post-lunch) and market-day (Saturday) effects are baked into the
training data, so predictions mirror real mandi behaviour.

### Tech stack (as proposed in the deck)
- **Backend:** Python, FastAPI
- **Database:** SQLite (stand-in for Firebase Firestore in the prototype)
- **AI/ML:** Python scikit-learn
- **Auth:** JWT (PyJWT)
- **Frontend:** mobile-friendly React + Tailwind single-page app (CDN runtime for this prototype)
- **Voice assistant:** HAL AI uses browser speech recognition and Gemini when `GEMINI_API_KEY` is configured
- **Notifications:** in-app notification centre (FCM hook point in production)

## Structure

```
agrivos/
├── requirements.txt
├── server.py        # Vercel entrypoint (exports the FastAPI app)
├── vercel.json      # Vercel Python function settings
├── app/
│   ├── main.py      # FastAPI routes (auth, centres, slots, bookings, queue, admin)
│   ├── db.py        # SQLite schema + demo seed data
│   ├── ai.py        # RandomForest wait-time & congestion models
│   ├── auth.py      # JWT helpers
│   └── static/
│       ├── index.html   # React + Tailwind farmer dashboard
│       └── logo.png     # AGRIVOS project mark
└── agrivos.db       # auto-created on first run
```

## API overview

```
POST /api/auth/register | /api/auth/login          → JWT
GET  /api/centres                                  → centres + live queue + congestion
GET  /api/centres/{id}/slots                       → slots + availability + congestion
POST /api/bookings                                 → book slot, issue token
GET  /api/bookings                                 → my bookings + queue position + ETA
POST /api/bookings/{id}/cancel
POST /api/hal-ai/parse                             → Gemini voice/text booking intent extraction
POST /api/ai/recommendations                       → ranked centre and slot recommendations
GET  /api/admin/alerts                             → derived capacity warnings
POST /api/admin/simulation                         → deterministic what-if simulation
GET  /api/admin/audit                              → operator audit history
GET  /api/notifications | POST /api/notifications/read
GET  /api/admin/overview                           → admin dashboard
GET  /api/admin/queue/{centre_id}                  → live queue
POST /api/admin/booking/{id}/stage                 → advance procurement stage (+notify)
POST /api/admin/slot | /api/admin/centre           → settings
```

## Notes
- Set `AGRIVOS_JWT_SECRET` env var in production.
- Set `GEMINI_API_KEY` in `.env` to enable Gemini-backed HAL AI parsing. Without it, the local quantity/vehicle fallback keeps demos usable.
- Historical waiting-time data is synthetic; swap `app/ai.py` training data with real
  centre logs (e-NAM / DFPD sources from the references slide) for production.
- Delete `agrivos.db` to reset the demo.
