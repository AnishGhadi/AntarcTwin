import asyncio
import json
import os
from collections import deque, defaultdict
from datetime import datetime
from typing import List

import paho.mqtt.client as mqtt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from database import Base, engine, get_db, SessionLocal
from models import TelemetryRecord, Alert
from twin_state import registry, NUMERIC_COMPONENT_BOUNDS, COMPONENT_META, component_level
import ai_engine

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
HISTORY_WINDOW = 200  # simulated days kept in-memory per station/subsystem for AI + charts

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Antarctic Digital Twin API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

history = defaultdict(lambda: defaultdict(lambda: deque(maxlen=HISTORY_WINDOW)))
connections: List[WebSocket] = []
main_loop: asyncio.AbstractEventLoop | None = None


def evaluate_and_store_alerts(db: Session, station_id: str, subsystem: str):
    twin = registry.get(station_id)
    if not twin or subsystem not in twin.subsystems:
        return
    sub = twin.subsystems[subsystem]

    existing = (
        db.query(Alert)
        .filter(Alert.station_id == station_id, Alert.subsystem == subsystem, Alert.active == True)  # noqa: E712
        .first()
    )

    if sub.status in ("warning", "critical"):
        if not existing:
            db.add(Alert(
                station_id=station_id,
                subsystem=subsystem,
                severity=sub.status,
                message=f"{subsystem} on {station_id} is {sub.status}",
                active=True,
            ))
            db.commit()
        elif existing.severity != sub.status:
            existing.severity = sub.status
            db.commit()
    else:
        if existing:
            existing.active = False
            existing.resolved_at = datetime.utcnow()
            db.commit()


def on_mqtt_message(client, userdata, msg):
    try:
        parts = msg.topic.split("/")
        if len(parts) != 3:
            return
        _, station_id, subsystem = parts
        payload = json.loads(msg.payload.decode())

        registry.update(station_id, subsystem, payload)
        history[station_id][subsystem].append(payload)

        db = SessionLocal()
        try:
            db.add(TelemetryRecord(station_id=station_id, subsystem=subsystem, payload=payload))
            db.commit()
            evaluate_and_store_alerts(db, station_id, subsystem)
        finally:
            db.close()

        if main_loop:
            asyncio.run_coroutine_threadsafe(broadcast_state(), main_loop)
    except Exception as e:
        print(f"[mqtt] error processing message: {e}")


def start_mqtt_client():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="fastapi-backend")
    client.on_message = on_mqtt_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.subscribe("station/+/+")
    client.loop_start()
    return client


async def broadcast_state():
    if not connections:
        return
    payload = json.dumps({"type": "state_update", "stations": registry.snapshot()})
    dead = []
    for ws in connections:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connections.remove(ws)


@app.on_event("startup")
async def startup():
    global main_loop
    main_loop = asyncio.get_running_loop()
    start_mqtt_client()


# ---------------------------------------------------------------------------
# Legacy / raw endpoints (kept for compatibility and for the history charts)
# ---------------------------------------------------------------------------

@app.get("/api/stations")
def get_stations():
    return registry.snapshot()


@app.get("/api/stations/{station_id}")
def get_station(station_id: str):
    twin = registry.get(station_id)
    return twin.to_dict() if twin else {"error": "station not found"}


@app.get("/api/stations/{station_id}/history/{subsystem}")
def get_history(station_id: str, subsystem: str, limit: int = 60):
    records = list(history[station_id][subsystem])[-limit:]
    return {"station_id": station_id, "subsystem": subsystem, "records": records}


@app.get("/api/stations/{station_id}/forecast/energy")
def forecast_energy(station_id: str, steps: int = 12):
    records = list(history[station_id]["energy"])
    return ai_engine.forecast_energy_load(records, steps_ahead=steps)


@app.get("/api/stations/{station_id}/forecast/fuel")
def forecast_fuel(station_id: str, steps: int = 12):
    records = list(history[station_id]["fuel"])
    return ai_engine.forecast_fuel(records, steps_ahead=steps)


@app.get("/api/alerts")
def get_alerts(active_only: bool = True, db: Session = Depends(get_db)):
    q = db.query(Alert)
    if active_only:
        q = q.filter(Alert.active == True)  # noqa: E712
    return [
        {
            "id": a.id, "station_id": a.station_id, "subsystem": a.subsystem,
            "severity": a.severity, "message": a.message, "created_at": a.created_at.isoformat(),
        }
        for a in q.order_by(Alert.created_at.desc()).limit(100).all()
    ]


# ---------------------------------------------------------------------------
# Outside conditions helper (shown on every page of the frontend)
# ---------------------------------------------------------------------------

def _outside_conditions(station_id: str):
    twin = registry.get(station_id)
    env = twin.subsystems["environment"].latest if twin else {}
    return {
        "temperature_c": env.get("temperature_c"),
        "wind_speed_kmh": env.get("wind_speed_kmh"),
        "snow_depth_cm": env.get("snow_depth_cm"),
        "visibility_km": env.get("visibility_km"),
        "humidity_pct": env.get("humidity_pct"),
        "storm_warning": env.get("storm_warning", False),
        "day": env.get("day"),
    }


# ---------------------------------------------------------------------------
# Page 1 - Home / overview: both stations, what needs attention, supply ship
# ---------------------------------------------------------------------------

@app.get("/api/overview")
def get_overview():
    out = []
    for station_id, twin in registry.stations.items():
        critical = twin.critical_component()

        fuel_data = twin.subsystems["fuel"].latest
        fuel_days = fuel_data.get("est_days_remaining")

        water_series = [h.get("water_reserve_pct") for h in history[station_id]["infrastructure"]]
        water_days = ai_engine.estimate_days_to_threshold(water_series, threshold=15, from_above=True)

        ship = dict(twin.subsystems["logistics"].latest)  # ship_name, status, days_to_arrival, ...

        # flag if either resource would realistically run critical before the
        # scheduled ship arrives - genuinely useful operational signal
        ship_eta = ship.get("days_to_arrival")
        early_warning = None
        if ship_eta is not None:
            if fuel_days is not None and fuel_days < ship_eta:
                early_warning = f"Fuel may run low ({fuel_days}d) before the next scheduled resupply ({ship_eta}d)."
            elif water_days is not None and water_days < ship_eta:
                early_warning = f"Water may run low ({water_days}d) before the next scheduled resupply ({ship_eta}d)."

        resupply = [
            {"item": "Fuel", "days_remaining": fuel_days,
             "level": "critical" if fuel_days is not None and fuel_days < 10
             else "warning" if fuel_days is not None and fuel_days < 30
             else "nominal" if fuel_days is not None else "unknown"},
            {"item": "Water", "days_remaining": water_days,
             "level": "critical" if water_days is not None and water_days < 10
             else "warning" if water_days is not None and water_days < 30
             else "nominal" if water_days is not None else "unknown"},
        ]

        out.append({
            "station_id": station_id,
            "display_name": twin.display_name,
            "overall_status": twin.overall_status,
            "critical_component": critical,
            "resupply": resupply,
            "supply_ship": ship,
            "early_warning": early_warning,
            "outside": _outside_conditions(station_id),
        })
    return out


# ---------------------------------------------------------------------------
# Page 2 - Station map / room layout
# ---------------------------------------------------------------------------

@app.get("/api/stations/{station_id}/rooms")
def get_rooms(station_id: str):
    twin = registry.get(station_id)
    if not twin:
        raise HTTPException(status_code=404, detail="station not found")
    return {
        "station_id": station_id,
        "display_name": twin.display_name,
        "overall_status": twin.overall_status,
        "rooms": twin.rooms_snapshot(),
        "outside": _outside_conditions(station_id),
    }


@app.get("/api/stations/{station_id}/rooms/{room_id}")
def get_room(station_id: str, room_id: str):
    twin = registry.get(station_id)
    if not twin:
        raise HTTPException(status_code=404, detail="station not found")
    room = twin.room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="room not found")
    return {
        "station_id": station_id,
        "display_name": twin.display_name,
        "room": room,
        "outside": _outside_conditions(station_id),
    }


# ---------------------------------------------------------------------------
# Page 3 - Room detail: predictive AI (1 week / 1 month / 3 months ahead)
# ---------------------------------------------------------------------------

@app.get("/api/stations/{station_id}/rooms/{room_id}/predict")
def predict_room(station_id: str, room_id: str):
    twin = registry.get(station_id)
    if not twin:
        raise HTTPException(status_code=404, detail="station not found")
    room_cfg = twin.room_config(room_id)
    if not room_cfg:
        raise HTTPException(status_code=404, detail="room not found")

    predictions = []
    for subsystem, field in room_cfg["fields"]:
        if field not in NUMERIC_COMPONENT_BOUNDS:
            continue
        floor, ceiling = NUMERIC_COMPONENT_BOUNDS[field]
        series = [h.get(field) for h in history[station_id][subsystem]]
        forecast = ai_engine.forecast_trend(series, floor=floor, ceiling=ceiling)
        if forecast is None:
            continue
        label, unit = COMPONENT_META.get(field, (field, ""))
        horizon_levels = {k: component_level(field, v) for k, v in forecast["horizons"].items()}
        predictions.append({
            "key": field, "subsystem": subsystem, "label": label, "unit": unit,
            "current_level": component_level(field, forecast["current"]),
            "horizon_levels": horizon_levels,
            **forecast,
        })

    return {
        "station_id": station_id, "room_id": room_id, "room_name": room_cfg["name"],
        "predictions": predictions,
    }


# ---------------------------------------------------------------------------
# Live push
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connections.append(websocket)
    await websocket.send_text(json.dumps({"type": "state_update", "stations": registry.snapshot()}))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in connections:
            connections.remove(websocket)
