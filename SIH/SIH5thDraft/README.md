# Antarctic Stations Digital Twin — v5

Four big changes from v4, one per page plus a new page:

1. **Page 1 (Overview):** a live GPS map of the supply ship's actual route
   (NCPOR Goa → Cape Town → Antarctica), alongside the existing text tracker.
2. **Page 2 (3D view):** your new AI-generated station models, auto-rotating,
   bigger, with a new **Logistics & Supply Control** room.
3. **Page 3 (Room Detail):** the forecasting engine is completely rebuilt —
   linear regression is gone, replaced by two purpose-built models (Kalman
   filter / resource-conservation) chosen per metric.
4. **Page 4 (new):** click any metric card to open a full-page deep dive —
   bigger charts, and a multi-paragraph written AI analysis.

Read the **"Please test these first"** section near the bottom before anything
else — a few pieces of this I genuinely could not visually verify.

---

## 1. Page 1 — Live GPS Ship Tracker

Below the station cards and the existing ship-status card, there's now a
live **Leaflet** map (OpenStreetMap tiles, no API key needed) plotting:

- The full route as you specified it: **NCPOR, Goa → Indian port staging →
  Cape Town → Southern Ocean → the station** (Maitri: Schirmacher Oasis,
  70.77°S; Bharati: Larsemann Hills, 69.41°S — both real, publicly documented
  coordinates for these NCPOR stations).
- A solid line for the **distance already sailed**, a dashed line for what's
  left, per station (Maitri in navy, Bharati in green).
- A 🚢 marker at the ship's **current interpolated position**, which moves
  automatically as the simulator's voyage `progress_pct` advances — no new
  polling logic on the frontend, it just re-renders on every existing patch
  cycle like everything else.

The route/waypoints and the ship's position are computed **server-side**
(`main.py: ship_geo()`) from the same `days_to_arrival`/`progress_pct` your
existing ship simulation already produces — nothing changed in how the ship
voyage itself is simulated, this just visualizes it geographically. The
route between waypoints is a smooth interpolation, not a maritime-grade
shipping lane — it's illustrative, the same way your reference flowchart was.

## 2. Page 2 — New Models, Logistics Room, Auto-Rotate

### The new models needed a different interaction approach

Your two new GLBs are structured completely differently from the old
"simplified digital twin" ones: each is **one single fused mesh with one
baked texture** (typical output of an image/prompt-to-3D pipeline), not a
model with named, separate buildings. That meant the v4 approach — click on
the actual `Fuel_Tank_1` mesh, tint the actual `Communication_Mast` mesh red
— literally cannot work here, because there's only one piece of geometry for
the whole station.

**What I built instead: floating hotspot pins.** Each room gets a pin
positioned at an estimated spot above the model (using its bounding box +
a raycast down onto the surface for correct height), shown as an icon with a
colored ring — nominal/warning/critical, with a blinking red ring + a small
alert popup for critical, exactly like you asked. Hover a pin → the left
panel shows that room's data. Click a pin → opens the room's full detail
page. This is a standard, robust pattern for exactly this situation (it's
how most 3D facility/virtual-tour tools handle a single-mesh scan or scene).

**I could not see where your buildings actually are**, so the 7 pins start
at reasonable estimated positions and almost certainly need nudging. There's
an **"📍 Adjust pins" button** in the bottom-right of the viewer — click it,
pick a room from the dropdown, and three sliders let you drag that pin
around (left/right, front/back, height) live while watching it move.
Positions save automatically in your browser as you adjust them, and there's
a **"Copy positions as JSON"** button that copies the exact object to paste
back into `DEFAULT_HOTSPOTS` in `index.html` so the good positions become
the real defaults for everyone.

### Auto-rotate

The camera slowly orbits the model by default (`controls.autoRotate`). The
moment you drag to look around, or tap a Top/Front/Side preset button, it
stops — standard "look, then take control" behavior — with a small
"▶ resume" link if you want it spinning again.

### Bigger

The viewer's height roughly doubled (now `max(520px, 100vh − 230px)` instead
of a fixed cap), and the surrounding chrome (legend, hints) was trimmed to
give the model as much of the screen as possible.

### The new Logistics & Supply Control room

Built from your second reference chat, almost in full. Twenty tracked
metrics: food/medical/scientific/engineering-spares/PPE/cleaning/emergency
stock levels, spare parts broken out by category (generator, pump,
electrical, vehicle), cold-storage temperature + humidity + door status,
warehouse capacity by zone (general/cold/spares/emergency), a computed
**Logistics Health** composite score, and — per your RFID note — a live
**RFID-tracked box count** plus a **flagged-boxes** count (representing
boxes an RFID scan says are missing, misplaced, or need attention). All of
it restocks automatically when the supply ship docks, same as fuel and
water. A new `--fault station:logistics` scenario simulates a
contamination/loss-type event: consumption jumps ~5x and flagged boxes
spike, for demoing that this room reacts too.

I deliberately trimmed a few of the most granular items from that chat
(per-item cargo tonnage manifests, a separate scientific-equipment-awaiting-
deployment counter) to keep the room to a coherent, demoable set rather than
an overwhelming one — say the word if you want any of those added back.

## 3. Page 3 — The New AI Models

This is the biggest engineering change in this version, from your first
reference chat.

**Linear regression is gone entirely.** Every metric now uses one of two
purpose-built models, chosen automatically per field:

**Kalman filter** (weather, power output, structural health, cold-room
conditions — anything that's a continuous physical measurement): a
state-space model that tracks a hidden "true level + trend" and corrects
both with every new reading, with the trend *damped* so it can't be
extrapolated forever, and a genuine, growing **95% uncertainty interval**
around every prediction (shown as the "±" under each horizon number). Every
forecast also runs an anomaly check on the filter's innovations — an
unusually large surprise in the last few readings gets flagged.

**Resource-balance (conservation) model** (fuel, water, and every logistics
stock — anything that's a depletable supply): instead of fitting a trend
line that could nonsensically predict a tank *gaining* fuel, this estimates
daily consumption from real day-over-day drops (excluding resupply jumps,
weighting recent days more), and projects forward by literal subtraction —
the forecast **cannot rise on its own**, only on the day a resupply is
actually scheduled. And it now knows about the ship: it pulls the live
`days_to_arrival` from the same ship model powering the page-1 map, so a
forecast can say *"reaches critical in 9 days — before the scheduled
resupply in 34 days"* — a real cross-check between two independent models,
not a coincidence of one chart looking bad.

The charts themselves still look the same (same two-panel layout, same
sparkline-then-projection style) — what changed is what's actually driving
the line, which is exactly what you asked for.

## 4. Page 4 — Metric Deep-Dive (new)

Click any metric card on the room-detail page → full-page view for that one
metric:
- Both charts, much bigger.
- The 7-day/30-day/90-day predictions, enlarged, each with its ± uncertainty.
- A small detail grid (daily rate, depletion ETA, days-to-critical, days to
  next resupply, anomaly status, how many days of history the model used).
- **A written AI analysis, 3 paragraphs**, generated per metric per model —
  what model is being used and why, the actual numbers behind the current
  prediction, and a "risk note" paragraph (e.g. flagging when recent
  consumption is running meaningfully above its longer-term average). This
  is genuinely computed from that metric's own numbers each time, not a
  templated filler paragraph.

---

## Please test these first

I could not do a live pixel-render test in my sandboxed tool environment —
the CDNs this needs (jsdelivr for Three.js, OpenStreetMap tile servers for
the map) are network-blocked for my automated browser, the same limitation
as v4. Everything I *could* verify offline — all backend logic, the JSON
shape of every API response, the resource model's "can never rise without a
resupply" guarantee, the JS syntax of the whole frontend — checks out. But
please check these specifically and tell me right away if anything's off:

1. **Open a station page and see where the 7 pins land.** They will very
   likely need adjusting via the "📍 Adjust pins" tool — that's expected,
   not a bug.
2. **Confirm the models render upright** (not tilted or on their side). I
   checked the raw vertex data and both are already Y-up, so no rotation fix
   was applied — but I couldn't see the actual render.
3. **Confirm the GPS map tiles load** on page 1. If they don't, it's almost
   certainly a network/CORS issue with the OpenStreetMap tile host in your
   environment, not the app logic.

## Quick start (unchanged)

```bash
docker compose up -d
cd backend && python -m venv venv && source venv/bin/activate && pip install -r requirements.txt && uvicorn main:app --port 8000
cd simulator && python -m venv venv && source venv/bin/activate && pip install paho-mqtt==2.1.0 && python simulate.py
cd frontend && python -m http.server 5500   # must be served over http, not opened as a file:// URL
```

New fault scenario: `python simulate.py --fault maitri:logistics`

## Files changed in this delivery

- `backend/twin_state.py` — new `logistics_inventory` subsystem, ~20 fields,
  new `logistics_room`, `FORECAST_MODEL` routing table.
- `backend/ai_engine.py` — full rewrite: Kalman filter + resource-balance
  models, uncertainty, anomaly detection, per-metric written analysis.
- `backend/main.py` — ship geography/GPS interpolation, model-routed
  predictions (passes the live ship ETA into resource forecasts), new
  `/api/stations/{id}/rooms/{room_id}/metric/{field}` endpoint for page 4.
- `backend/models.py`, `backend/database.py`, `backend/requirements.txt` —
  unchanged.
- `simulator/simulate.py` — new `step_logistics_inventory()`, restock hook
  on ship arrival, new `logistics` fault, ship renamed to **MV Golovnin**
  (Cape Town → Antarctica leg, per your route).
- `frontend/index.html` — GPS map (page 1), hotspot-pin 3D viewer with
  auto-rotate + calibration tool (page 2), clickable metric cards (page 3),
  new metric deep-dive page (page 4).
- `frontend/models/maitri.glb`, `frontend/models/bharati.glb` — your new
  models, replacing the old simplified ones.
