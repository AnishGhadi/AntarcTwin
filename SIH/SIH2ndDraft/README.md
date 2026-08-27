# Antarctic Stations Digital Twin — MVP

Digital twin for Maitri and Bharati research stations: simulated telemetry
over MQTT, a FastAPI backend that maintains live twin state, room-level
breakdowns, and long-horizon AI forecasts, Postgres for history, and a
multi-page, blueprint-style dashboard.

## What's in this version

The original single-dashboard MVP is now a **3-page clickable frontend**
that mirrors how someone would actually navigate a real station:

1. **Overview** (`#/`) — both stations side by side: overall status, the
   one component that most urgently needs attention, and a countdown to
   the next fuel/water resupply.
2. **Station blueprint** (`#/station/maitri`) — a top-view room layout
   (matching the station blueprints) where every room shows its own
   components at a glance, each individually leveled nominal / warning /
   critical, with live numbers.
3. **Room detail** (`#/station/maitri/room/fuel_system`) — click a room to
   drill in: every numeric reading gets a recent-history sparkline, an
   AI-projected trend for the next 90 days, and predicted values at
   **1 week / 1 month / 3 months** ahead.

Outside conditions (temperature, wind, snow depth, visibility) are shown
as a persistent strip on the station and room pages, and as a summary on
each station's overview card.

## Why this architecture

- **One FastAPI process** hosts ingestion, twin state, forecasting, REST,
  and WebSocket. For a short MVP timeline, separate microservices cost you
  debugging time you don't have. The modules are still separated in code
  (`twin_state.py`, `ai_engine.py`, `main.py`) so it's easy to peel one out
  into its own service later if you want that in your final architecture
  diagram for judges.
- **Twin state, not just telemetry.** `twin_state.py` is the actual digital
  twin: each station is an object with subsystems *and* rooms that have a
  *status* derived from thresholds, not just raw numbers.
- **Rooms are config, not hardcoded HTML.** `ROOM_LAYOUTS` in
  `twin_state.py` describes every room's grid position, icon, and which
  subsystem fields it reads — the blueprint page is rendered entirely from
  what `/api/stations/{id}/rooms` returns. Adding, moving, or renaming a
  room is a config change, not a frontend rewrite.
- **Per-component severity, not just per-subsystem.** Every individual
  reading (temperature, wind, fuel %, water reserve, ...) gets its own
  nominal/warning/critical level via `component_level()`, which is what
  lets a room show "temperature nominal, wind warning" instead of one
  blanket status for the whole room.
- **Fault injection in the simulator** (`--fault maitri:energy`,
  `--fault bharati:fuel`, `--fault maitri:water`) lets you demo resilience
  live instead of just describing it. Baseline consumption (fuel, water)
  is intentionally realistic and slow, like a real station's logistics —
  faults are what make the number move fast enough to demo in a few
  minutes.
- **Forecasting kept simple on purpose** (linear regression + trend,
  `ai_engine.forecast_trend`) so it runs in milliseconds with zero training
  step, yet still projects a believable 90-day curve and 1-week/1-month/
  3-month resource outlook. Swap in Prophet/ARIMA/an LSTM later only if you
  have time left — the shape of the response stays the same.

## Quick start

```bash
# 1. Start MQTT broker + Postgres
docker compose up -d

# 2. Backend
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# 3. Simulator (separate terminal)
cd simulator
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python simulate.py

# 4. Open frontend/index.html directly in a browser
#    (or serve it:  python -m http.server 5500 from the frontend/ dir)
```

To demo a fault live:
```bash
python simulate.py --fault maitri:energy    # generator trips, diesel drops
python simulate.py --fault bharati:fuel     # simulated leak, fast drain
python simulate.py --fault maitri:water     # pump/intake issue, ~8x drain rate
# or combine: --fault maitri:energy,bharati:water
```

Then in the frontend: Overview → click the station → click the affected
room → the AI projection panel will show the resource crossing into
warning/critical within days instead of months.

> **Heads up on URLs:** `frontend/index.html` points at
> `http://localhost:8000` / `ws://localhost:8000/ws`. If you deploy the
> backend somewhere else, update `API_BASE` and `WS_URL` near the top of
> the `<script>` block.

## API endpoints

**Overview / blueprint / rooms (new)**
- `GET /api/overview` — per-station overall status, the single most urgent
  component, and fuel/water resupply countdowns. Powers the home page.
- `GET /api/stations/{id}/rooms` — full room layout + live component
  levels/values for that station, plus outside conditions. Powers the
  blueprint page.
- `GET /api/stations/{id}/rooms/{room_id}` — one room's detail.
- `GET /api/stations/{id}/rooms/{room_id}/predict` — AI forecast for every
  numeric component in that room: current value, 90-day projection curve,
  and 7/30/90-day horizon predictions with severity levels. Powers the
  room-detail page's predictive panels.

**Raw / legacy (still used for history charts)**
- `GET /api/stations` — live twin snapshot, all stations
- `GET /api/stations/{id}` — one station's subsystem-level twin state
- `GET /api/stations/{id}/history/{subsystem}?limit=50` — recent raw readings
- `GET /api/stations/{id}/forecast/energy?steps=12` — short-horizon energy forecast
- `GET /api/stations/{id}/forecast/fuel?steps=12` — short-horizon fuel forecast
- `GET /api/alerts` — active alerts
- `WS /ws` — live twin state push (fires on every new reading; the
  frontend uses this only as a "something changed, re-fetch the current
  page" signal, throttled to once every 2s)

## Room layout reference

Both stations share the same room set (matching the blueprints):
`environment_zone`, `auxiliary_facilities` (summer camp + container
modules — no live sensors, shown as informational), `main_building`,
`power_energy`, `communication`, `fuel_system`, `water_pump_house`. Edit
`ROOM_LAYOUTS` in `backend/twin_state.py` to add rooms, move them on the
grid, or change which subsystem fields they surface.

## Design notes for the frontend

Winter/blueprint theme, deliberately static — no animated transitions
anywhere (Chart.js `animation: false` on every chart), status color is the
only motion-free "signal" (nominal = aurora mint, warning = aurora gold,
critical = aurora red). The station map is drawn as an architectural
blueprint (hairline grid background, compass rose, drafted room boxes)
rendered purely from the `/rooms` API response.

## Suggested day-by-day plan (7 days)

**Day 1 — Get this skeleton running end to end.**
Run the quick start above exactly as-is. Confirm: simulator publishes →
backend ingests → Overview page shows both stations → clicking through to
a station blueprint and a room detail page works and updates live.

**Day 2 — Make the simulation more convincing.**
Tune the numbers in `simulator/simulate.py` against real Maitri/Bharati
specs if you can find public figures (solar/wind capacity, fuel tank size,
station population). This is cheap credibility with judges. Try all three
fault scenarios end-to-end and watch how they surface on each page.

**Day 3 — Twin state & alerting depth.**
Extend `twin_state.py`: add more rooms/components if your problem
statement mentions them explicitly (e.g. a dedicated waste-management
room). Tune thresholds in `THRESHOLDS` and `component_level()`. Make sure
alerts actually open/close correctly as conditions change.

**Day 4 — AI engine.**
If time allows, upgrade `ai_engine.forecast_trend` from linear regression
to Holt-Winters or Prophet for a smoother-looking projection, and add a
confidence band to the chart. Add a `/api/stations/{id}/recommendation`
endpoint that turns a forecast into a plain-English suggestion (e.g.
"resupply flight recommended within 9 days at current consumption").

**Day 5 — Dashboard polish.**
Add more chart types on the room-detail page if useful (e.g. overlaying
multiple related components). Double-check mobile layout on the blueprint
grid. Keep the "no animation" constraint if that's still the brief.

**Day 6 — Demo script + resilience testing.**
Write and rehearse the actual demo: start clean on the Overview page, show
nominal state, drill into a station, run `--fault maitri:water`, click
into the water pump house room, and show the AI projection panel flip to
"resupply within days." Test what happens if MQTT broker or backend
restarts — don't let a judge's random click break it.

**Day 7 — Buffer.**
Something will break on day 6. This day exists for that. Also prepare the
architecture diagram and a 2-minute problem/solution framing for the pitch.

## What to say if judges ask "why not microservices / Kafka / Kubernetes"

Be upfront: this is intentionally a monolith for the prototype stage, with
clean internal module boundaries that map directly onto a real deployment
architecture (ingestion service, twin-state service, forecasting service,
API gateway) — and MQTT was chosen specifically because Antarctic stations
have constrained, intermittent satellite bandwidth, which is exactly the
condition MQTT was designed for over heavier alternatives.
