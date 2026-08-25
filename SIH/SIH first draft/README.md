# Antarctic Stations Digital Twin — MVP

Digital twin for Maitri and Bharati research stations: simulated telemetry
over MQTT, a FastAPI backend that maintains live twin state and forecasts,
Postgres for history, and a WebSocket-driven dashboard.

## Why this architecture

- **One FastAPI process** hosts ingestion, twin state, forecasting, REST,
  and WebSocket. For a 7-day MVP, separate microservices cost you debugging
  time you don't have. The modules are still separated in code
  (`twin_state.py`, `ai_engine.py`, `main.py`) so it's easy to peel one out
  into its own service later if you want that in your final architecture
  diagram for judges.
- **Twin state, not just telemetry.** `twin_state.py` is the actual digital
  twin: each station is an object with subsystems that have a *status*
  derived from thresholds, not just raw numbers. This is what a judge will
  look for and what a plain "IoT dashboard" doesn't have.
- **Fault injection in the simulator** (`--fault maitri:energy`) lets you
  demo resilience live instead of just describing it.
- **Forecasting kept simple on purpose** (linear regression + trend) so it
  runs in milliseconds with zero training step. Swap in Prophet/ARIMA/an
  LSTM later only if you have time left — the interface doesn't change.

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
pip install -r requirements.txt
python simulate.py

# 4. Open frontend/index.html directly in a browser
#    (or serve it: python -m http.server 5500 from the frontend/ dir)
```

To demo a fault live:
```bash
python simulate.py --fault maitri:energy
# or trigger it mid-run by restarting with --fault bharati:fuel
```

## API endpoints

- `GET /api/stations` — live twin snapshot, all stations
- `GET /api/stations/{id}` — one station's twin state
- `GET /api/stations/{id}/history/{subsystem}?limit=50` — recent raw readings
- `GET /api/stations/{id}/forecast/energy?steps=12` — energy forecast
- `GET /api/stations/{id}/forecast/fuel?steps=12` — fuel forecast + resupply flag
- `GET /api/alerts` — active alerts
- `WS /ws` — live twin state push (fires on every new reading)

## Suggested day-by-day plan (7 days)

**Day 1 — Get this skeleton running end to end.**
Run the quick start above exactly as-is. Confirm: simulator publishes →
backend ingests → dashboard shows live-updating cards for both stations.
Don't customize anything yet — just get the pipeline breathing.

**Day 2 — Make the simulation more convincing.**
Tune the numbers in `simulator/simulate.py` against real Maitri/Bharati
specs if you can find public figures (solar/wind capacity, fuel tank size,
station population). This is cheap credibility with judges. Add 1-2 more
fault scenarios (e.g. comms blackout, storm).

**Day 3 — Twin state & alerting depth.**
Extend `twin_state.py`: add more subsystems if your problem statement
mentions them explicitly (e.g. water treatment, waste management). Tune
thresholds. Make sure alerts actually open/close correctly as conditions
change — this is what you'll demo live.

**Day 4 — AI engine.**
If time allows, upgrade `ai_engine.py` from linear regression to
Holt-Winters or Prophet for a smoother-looking forecast curve, and add a
confidence interval to plot. Add a `/api/stations/{id}/recommendation`
endpoint that turns the forecast into a plain-English suggestion (e.g.
"resupply flight recommended within 9 days at current consumption").

**Day 5 — Dashboard polish.**
Add a historical chart (energy load over last N hours) using the
`/history` endpoint. Add a station map/layout view if you want a visual
"twin" feel rather than just cards. Add light/dark styling consistency.

**Day 6 — Demo script + resilience testing.**
Write and rehearse the actual demo: start clean, show nominal state, run
`--fault maitri:energy`, watch the alert appear and the dashboard update
within seconds, show the fuel forecast shifting. Test what happens if
MQTT broker or backend restarts — don't let a judge's random click break it.

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
