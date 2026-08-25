import asyncio
import json
import os
from collections import deque, defaultdict
from datetime import datetime
from typing import List

import paho.mqtt.client as mqtt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from database import Base, engine, get_db, SessionLocal
from models import TelemetryRecord, Alert
from twin_state import registry
import ai_engine

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
HISTORY_WINDOW = 200  # readings kept in-memory per station/subsystem for AI + charts

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Antarctic Digital Twin API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# rolling in-memory history: history[station_id][subsystem] -> deque of payloads
history = defaultdict(lambda: defaultdict(lambda: deque(maxlen=HISTORY_WINDOW)))

# active websocket connections
connections: List[WebSocket] = []

# the asyncio loop the FastAPI app runs on - needed to schedule broadcasts
# from the MQTT thread, which runs outside the event loop
main_loop: asyncio.AbstractEventLoop | None = None


def evaluate_and_store_alerts(db: Session, station_id: str, subsystem: str):
    """Look at current twin status and open/resolve alerts accordingly."""
    twin = registry.get(station_id)
    if not twin:
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
        parts = msg.topic.split("/")  # station/<station_id>/<subsystem>
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


@app.get("/api/stations")
def get_stations():
    """Current twin state snapshot for all stations."""
    return registry.snapshot()


@app.get("/api/stations/{station_id}")
def get_station(station_id: str):
    twin = registry.get(station_id)
    return twin.to_dict() if twin else {"error": "station not found"}


@app.get("/api/stations/{station_id}/history/{subsystem}")
def get_history(station_id: str, subsystem: str, limit: int = 50):
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
            "id": a.id,
            "station_id": a.station_id,
            "subsystem": a.subsystem,
            "severity": a.severity,
            "message": a.message,
            "created_at": a.created_at.isoformat(),
        }
        for a in q.order_by(Alert.created_at.desc()).limit(100).all()
    ]


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connections.append(websocket)
    # send an initial snapshot immediately on connect
    await websocket.send_text(json.dumps({"type": "state_update", "stations": registry.snapshot()}))
    try:
        while True:
            await websocket.receive_text()  # keep-alive / ignore client messages
    except WebSocketDisconnect:
        if websocket in connections:
            connections.remove(websocket)
