# Antarctic Stations Digital Twin — v4

Digital twin for Maitri and Bharati research stations. Backend, simulator, data
model, and AI forecasting are **completely unchanged from v3** — this version
only replaces the Page 2 "station site map" with a real 3D building viewer.

## What changed in v4

**Page 2 (station view) is now a 3D model you can orbit, not a 2D map.**

- Loads the actual GLB models you provided (`maitri.glb`, `bharati.glb`) using
  Three.js, with full 360-degree orbit (drag) and zoom (scroll).
- A small button box in the top-right corner jumps the camera to **Top,
  Front, Back, Left, Right, or Iso** view with a smooth animated transition —
  you can still freely orbit from wherever it lands.
- **Hover** any building and a panel on the left shows that room's live
  readings, exactly like the old room boxes did (icon, name, each component
  with its status dot and value).
- **Click** a building to open its full room-detail page (page 3, unchanged)
  — same as clicking a room used to do on the old 2D map.
- A room in **critical** status pulses red on the model itself, plus a small
  floating alert popup appears above it naming the specific reading that's
  critical and its value (e.g. "⚠ Water Reserve: 8.2%"). A room in
  **warning** gets a steady amber tint (no blinking — that's reserved for
  critical, so the blink stays meaningful as an urgency signal).
- Nothing on the backend changed to support this — the existing
  `/api/stations/{id}/rooms` endpoint already returned everything the viewer
  needs (per-room status + component list). This is a frontend-only change.

**Pages 1 (Overview) and 3 (Room Detail) are untouched**, byte-for-byte the
same logic as v3.

## Files in this delivery

- **`frontend/index.html`** — modified. Only the station-view (page 2)
  section changed (JS + CSS); everything else (Overview page, Room Detail
  page, routing, the AI charts, WebSocket handling) is identical to what you
  already had. Your `API_BASE`/`WS_URL` LAN IP customization was preserved.
- **`frontend/models/maitri.glb`**, **`frontend/models/bharati.glb`** — new.
  Your uploaded models, placed where `index.html` expects them (relative
  path `models/<station>.glb`).
- **`backend/twin_state.py`, `backend/ai_engine.py`, `backend/main.py`,
  `backend/models.py`, `backend/database.py`, `backend/requirements.txt`,
  `simulator/simulate.py`, `docker-compose.yml`, `mosquitto.conf`** —
  **unchanged**, included only so this is a complete, drop-in project.
  Diffed byte-for-byte against what you uploaded to confirm.

## How the 3D viewer works (for your own understanding / if judges ask)

**Room mapping.** The GLBs are a flat list of named meshes (`Main_Building`,
`Fuel_Tank_1`, `Solar_Panel_2`, `Sensor_Fuel`, ...) with no existing
room/group structure. On load, every mesh name is matched against a set of
keyword patterns (`CLASSIFY_RULES` in `index.html`) that sort it into one of
your existing 7 rooms — e.g. anything matching `fuel_tank|fuel_platform` →
`fuel_system`, anything matching `communication|radome|mast_crossbar` →
`communication`. Scenery meshes (terrain, rocks, the helipad, loose service
pipes) match nothing and are just rendered as background — not clickable,
not hoverable, never tinted. I verified this classification against the
actual node list from both your files: every one of the 7 rooms gets at
least one mesh in both models, and everything left unclassified is
genuinely decorative.

**The tilt fix.** Rather than guess, I inspected the raw vertex bounding box
in both GLBs: the Z axis has by far the smallest extent (~11–12.6 units vs
~30–48 on X/Y) in both files, which is exactly what you'd expect if Z is
"height" on a station-shaped model — meaning both were exported Z-up
(a common Blender default) instead of glTF's standard Y-up. Both models get
rotated **-90° around X** on load to correct this
(`MODEL_CONFIGS.<station>.tiltFixX`). If a model still looks wrong after you
test it (upside-down or facing an unexpected direction), flip that one
constant to `+Math.PI / 2` — the fix is isolated to a single line per model.

**Status visualization.** Every render frame, each mesh's material color is
reset to its original color and then blended toward red (critical, with a
sine-wave pulse for the blink) or amber (warning, steady) — materials are
cloned per-mesh on load specifically so tinting one room's meshes can never
leak into another mesh that happened to share the same material. The
floating critical-alert popups are plain HTML elements, not 3D text —
each room's world-space center is projected to 2D screen coordinates every
frame (`Vector3.project(camera)`) and the popup's `left`/`top` are updated
to match, so it stays pinned above the building as you orbit.

**Library loading.** Three.js, GLTFLoader, and OrbitControls are loaded via
dynamic `import()` from jsdelivr only when you actually open a station page
— the Overview page never pays that cost. This also means the whole 3D
stack lives inside `mountStation()`/`initViewer()`; navigating away calls
`disposeViewer()`, which cancels the render loop, disposes every geometry
and material, and destroys the WebGL context, so repeatedly clicking
between stations doesn't leak GPU contexts.

## A verification note

I inspected both GLBs directly (parsed the glTF JSON to confirm mesh names,
and computed the raw vertex bounding box to derive the tilt-fix direction
empirically rather than guessing) and unit-tested the room-classification
logic against every actual node name in both files. I could **not** do a
live pixel-render test end-to-end, because the sandboxed environment I run
in blocks the CDN this needs (jsdelivr) even though your own browser/network
can reach it fine — the same CDN your project already uses successfully for
Chart.js. Please test this first thing after pulling these files: open a
station page, confirm the model appears upright (not tilted or lying on its
side), confirm hover/click/view-presets work, and let me know immediately
if anything looks off so I can fix it fast rather than you having to debug
three.js internals yourself.

## Quick start (unchanged)

```bash
# 1. Start MQTT broker + Postgres
docker compose up -d

# 2. Backend
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --port 8000

# 3. Simulator (separate terminal)
cd simulator
python -m venv venv && source venv/bin/activate
pip install paho-mqtt==2.1.0
python simulate.py

# 4. Serve the frontend (must be served over http/https, not opened as a
#    file:// URL, or the browser will refuse to load models/*.glb)
cd frontend
python -m http.server 5500
# then open http://localhost:5500 (or http://<your-LAN-IP>:5500 on another device)
```

Everything else — fault injection flags, the AI forecasting behavior, the
Overview page's supply-ship tracker, the Room Detail predictive charts — is
exactly as documented in the previous README.
