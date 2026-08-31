# Antarctic Stations Digital Twin — v3

Digital twin for Maitri and Bharati research stations: simulated telemetry
over MQTT (now on a realistic **1-tick-equals-1-day** clock), a FastAPI
backend with room-level twin state and long-horizon AI forecasts, Postgres
for history, and a full-screen, ice/glacier-themed dashboard with an actual
architectural site map instead of a plain grid.

## What changed in v3 (based on direct feedback on v2)

1. **Theme is now actually Antarctic** — white/ice background, navy
   signage-style header and section bars (matching the look of the station
   blueprint images you sent), aurora status colors reserved for
   nominal/warning/critical only.
2. **Full-width layout.** The dashboard now fills the viewport instead of
   sitting in a narrow centered column.
3. **Realistic environment simulation.** Temperature, wind, humidity, snow
   depth and visibility are bounded, mean-reverting daily processes clamped
   to real Antarctic ranges (temperature: -55°C to 8°C, wind: 5-130 km/h,
   etc.) — see "Why the old version broke" below for what was actually
   causing the -10,000°C readings.
4. **AI projection charts are no longer straight lines.** The projection
   curve is the trend line *plus* that exact component's own recent
   day-to-day variability tiled forward, so it visibly wiggles the way a
   real forecast would, while the 7-day/30-day/90-day point predictions
   stay grounded in the same underlying trend.
5. **Recent Readings are realistic** — a direct consequence of #3 and the
   new day-granularity (see below): the sparkline now shows real seasonal
   and day-to-day movement instead of a nearly-flat 2-minute window.
6. **Bigger text everywhere** (16px base instead of 13px, larger headings,
   larger chart labels), partly because of #2 freeing up room to breathe.
7. **Mobile-safe.** Below ~860px width the architectural site map switches
   to a stacked full-width room list instead of trying to shrink a
   1000×700 schematic into a phone screen (see the design note below on
   why this is a deliberate tradeoff, not a bug).
8. **The station map is now an actual site map**, not a grid of rectangles:
   rooms are positioned like the blueprint you sent, connected by drawn
   pathways, color-coded by area type (Main Building / Power / Fuel /
   Water / Living / Communication / Weather) with a legend, a compass
   rose, and a shoreline with a water-intake connector into the Water Pump
   House — rendered from `MAP_LAYOUT` in `frontend/index.html`, driven by
   live data from `/api/stations/{id}/rooms`.
9. **No more full-page flash on refresh.** The frontend now mounts each
   page's DOM structure once and, on every subsequent poll, only patches
   the specific text nodes / status colors / chart data that changed —
   it no longer rebuilds (and doesn't destroy/recreate charts) on every
   update. See "How live updates work now" below.
10. **Changes are realistic and daily**, not extreme jumps — see below.
11. **"Next Resupply" is now a real, deterministic countdown** — the
    scheduled ship's ETA — highlighted at the top of the Overview page,
    and it ticks down by exactly 1 day per simulated tick (see #12).
12. **Supply ship tracker.** Each station now has a simulated cargo ship on
    a realistic ~70-140 day voyage cycle (`station/<id>/logistics` topic).
    When it arrives it actually resupplies fuel and water in the
    simulation. Status includes `In Transit`, `Delayed (Weather)`,
    `Docked - Unloading`, and `Departed`. The overview page also flags it
    if fuel or water is projected to run low *before* the ship is due.

## Why the old version's numbers went to -10,000°C

It wasn't the raw simulator — v2's temperature reading itself was always
bounded per-tick. The bug was in the AI forecasting math: v2 published a
reading every ~3 *seconds* of real time, so projecting "per day" required
multiplying the regression slope by ~28,800 (readings/day), then again by
up to 90 for the 90-day horizon. Multiplying *any* small amount of ordinary
sensor noise by up to 2.6 million is how a flat, boring temperature series
turned into an apocalyptic forecast.

**v3's fix:** the simulator now emits exactly one reading per simulated
day. A regression slope computed over that history *is already* a
per-day rate — no multiplier, no noise amplification — and every numeric
field also has a hard physical floor/ceiling (`twin_state.NUMERIC_COMPONENT_BOUNDS`)
that clamps any forecast regardless. This is also why the recent-readings
charts and day-to-day deltas look realistic now: one entry in history
really does correspond to one day of station time.

## How the "1 day per tick" model works

`simulator/simulate.py` advances every station's simulated calendar by one
day each time it publishes (`--tick-seconds` controls how many *real*
seconds that takes — default 6, purely for demo pacing). Every field is a
bounded, mean-reverting process (`_ar1` helper) so:

- normal day-to-day movement is small and believable (fuel/water typically
  move by a few tenths of a percent per day for a ~30-person station, not
  tens of percent),
- nothing can drift to an impossible value even over a very long run,
- a fault (`--fault station:subsystem`) meaningfully worsens the daily
  rate (e.g. a fuel leak adds ~150-220 L to a single day's consumption)
  without being an instant jump to empty.

## How live updates work now

The backend's WebSocket still pushes on every new reading, but the
frontend no longer treats that as "re-render the page." Each page is
mounted once (full DOM + chart instances built), and every subsequent
WebSocket ping just triggers a "patch" call for whatever page is currently
open: it re-fetches the relevant REST endpoint(s) and updates only the
specific `textContent`, status classes, and `chart.data` that changed —
existing `<canvas>` elements and Chart.js instances are reused
(`chart.update("none")`, no animation), not destroyed and rebuilt. A full
remount only happens when you actually navigate to a different page.

## Design note: the site map on mobile

SVG `viewBox` scaling shrinks *everything* inside it proportionally,
including the room boxes' text — a 1000×700 architectural map that looks
great on a laptop would render its room labels unreadably small on a
360px-wide phone. Rather than ship illegible text on mobile, below ~860px
the site map is swapped for a full-width stacked list of the same rooms
with the same live data, just no map. If you want the schematic map
itself to be pannable/zoomable on mobile instead, that's a reasonable v4
ask — flag it and it can be added with something like an SVG pan-zoom
wrapper.

## Quick start

```bash
# 1. Start MQTT broker + Postgres
docker compose up -d

# 2. Backend
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --port 8000
#   (avoid --reload for a demo/testing session - it restarts the process,
#   which wipes the in-memory history the AI predictor needs to build up)

# 3. Simulator (separate terminal)
cd simulator
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python simulate.py
#   optional: python simulate.py --tick-seconds 3   # advance faster

# 4. Open frontend/index.html directly in a browser
#    (or serve it: python -m http.server 5500 from the frontend/ dir)
```

Give it a minute or two after starting so a few days of history accumulate
— the AI projection needs at least 3 daily readings to fit a trend line at
all, and looks much better with 15-20+.

To demo a fault live:
```bash
python simulate.py --fault maitri:energy    # generator trips, diesel drops
python simulate.py --fault bharati:fuel     # leak, ~150-220L extra/day
python simulate.py --fault maitri:water     # pump/intake issue, ~6x drain rate
# or combine: --fault maitri:energy,bharati:water
```
Then: Overview → click the station → click the affected room → watch the
AI projection panel and the 1-week/1-month/3-month horizon cards shift
toward warning/critical over the next several ticks (days).

> **Heads up on URLs:** `frontend/index.html` points at
> `http://localhost:8000` / `ws://localhost:8000/ws`. Update `API_BASE`
> and `WS_URL` near the top of the `<script>` block if you deploy the
> backend elsewhere.

## API endpoints

**Overview / site map / rooms**
- `GET /api/overview` — per-station overall status, the single most urgent
  component, fuel/water AI depletion estimates, and the supply ship
  tracker (`supply_ship`) including an `early_warning` if a resource is
  projected to run low before the ship is due. Powers the home page.
- `GET /api/stations/{id}/rooms` — full room layout + live component
  levels/values, plus outside conditions (now includes `day`). Powers the
  site map page.
- `GET /api/stations/{id}/rooms/{room_id}` — one room's detail.
- `GET /api/stations/{id}/rooms/{room_id}/predict` — AI forecast for every
  numeric component in that room: current value, 90-day projection curve
  (with realistic noise), and 7/30/90-day horizon predictions with
  severity levels.

**Raw / legacy**
- `GET /api/stations`, `GET /api/stations/{id}` — twin snapshots
- `GET /api/stations/{id}/history/{subsystem}?limit=60` — recent daily readings
- `GET /api/stations/{id}/forecast/energy|fuel?steps=12` — short-horizon forecasts
- `GET /api/alerts` — active alerts (now also fires on ship delays)
- `WS /ws` — live push signal (frontend treats it as "go patch the current
  page," throttled to once every 1.5s)

## Room / area reference

Both stations share the same 7 areas, matching the blueprints you sent:
`environment_zone`, `auxiliary_facilities` (summer camp + container
modules — informational, no live sensors), `main_building`,
`power_energy`, `communication`, `fuel_system`, `water_pump_house`. Edit
`ROOM_LAYOUTS` in `backend/twin_state.py` to change which subsystem fields
a room surfaces, and `MAP_LAYOUT` / `PATHWAYS` near the top of
`frontend/index.html`'s `<script>` to move rooms around on the site map or
change which areas are connected.

## What to say if judges ask "why not microservices / Kafka / Kubernetes"

Be upfront: this is intentionally a monolith for the prototype stage, with
clean internal module boundaries that map directly onto a real deployment
architecture (ingestion service, twin-state service, forecasting service,
API gateway) — and MQTT was chosen specifically because Antarctic stations
have constrained, intermittent satellite bandwidth, which is exactly the
condition MQTT was designed for over heavier alternatives.
