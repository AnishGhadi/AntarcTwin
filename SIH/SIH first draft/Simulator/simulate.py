"""
Digital Twin telemetry simulator for Maitri and Bharati Antarctic stations.

Publishes JSON messages to MQTT topics of the form:
    station/<station_id>/<subsystem>

Subsystems: energy, fuel, environment, infrastructure

Run:
    python simulate.py
    python simulate.py --fault maitri:energy    # inject a fault on start
"""

import json
import math
import random
import time
import argparse
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

MQTT_HOST = "localhost"
MQTT_PORT = 1883
PUBLISH_INTERVAL_SEC = 3

STATIONS = {
    "maitri": {
        "name": "Maitri",
        "base_temp_c": -25,
        "solar_capacity_kw": 15,
        "wind_capacity_kw": 20,
        "diesel_capacity_kw": 60,
        "fuel_tank_liters": 50000,
        "base_load_kw": 35,
    },
    "bharati": {
        "name": "Bharati",
        "base_temp_c": -15,
        "solar_capacity_kw": 20,
        "wind_capacity_kw": 25,
        "diesel_capacity_kw": 80,
        "fuel_tank_liters": 70000,
        "base_load_kw": 45,
    },
}

# in-memory fuel level tracking so consumption trends downward realistically
fuel_state = {sid: cfg["fuel_tank_liters"] for sid, cfg in STATIONS.items()}

# active fault flags, e.g. {"maitri": {"energy": True}}
active_faults = {sid: {} for sid in STATIONS}


def season_factor(hour):
    """Rough diurnal factor for solar/wind availability (0 to 1)."""
    return max(0, math.sin((hour / 24) * math.pi))


def gen_energy(station_id, cfg, t):
    hour = (t / 3600) % 24
    solar_kw = cfg["solar_capacity_kw"] * season_factor(hour) * random.uniform(0.6, 1.0)
    wind_kw = cfg["wind_capacity_kw"] * random.uniform(0.2, 0.9)
    load_kw = cfg["base_load_kw"] + random.uniform(-3, 6)

    fault = active_faults[station_id].get("energy", False)
    diesel_kw = max(0, load_kw - solar_kw - wind_kw)
    generator_status = "running" if diesel_kw > 0.5 else "standby"

    if fault:
        generator_status = "fault"
        diesel_kw = 0

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "solar_kw": round(solar_kw, 2),
        "wind_kw": round(wind_kw, 2),
        "diesel_kw": round(diesel_kw, 2),
        "load_kw": round(load_kw, 2),
        "generator_status": generator_status,
        "grid_frequency_hz": round(random.uniform(49.8, 50.2), 2) if not fault else 0,
    }


def gen_fuel(station_id, cfg, t, energy_payload):
    fault = active_faults[station_id].get("fuel", False)
    # diesel consumption ~ 0.3 L per kWh, rough genset heuristic
    consumption = energy_payload["diesel_kw"] * 0.3 * (PUBLISH_INTERVAL_SEC / 3600)
    if fault:
        consumption += 5  # simulate a leak
    fuel_state[station_id] = max(0, fuel_state[station_id] - consumption)

    days_remaining = None
    avg_daily_use = energy_payload["diesel_kw"] * 0.3 * 24
    if avg_daily_use > 0:
        days_remaining = round(fuel_state[station_id] / avg_daily_use, 1)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fuel_level_liters": round(fuel_state[station_id], 1),
        "fuel_capacity_liters": cfg["fuel_tank_liters"],
        "fuel_pct": round(100 * fuel_state[station_id] / cfg["fuel_tank_liters"], 1),
        "leak_detected": fault,
        "est_days_remaining": days_remaining,
        "resupply_pending": fuel_state[station_id] < 0.15 * cfg["fuel_tank_liters"],
    }


def gen_environment(station_id, cfg, t):
    hour = (t / 3600) % 24
    temp = cfg["base_temp_c"] + 5 * math.sin((hour / 24) * 2 * math.pi) + random.uniform(-2, 2)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "temperature_c": round(temp, 1),
        "wind_speed_kmh": round(random.uniform(10, 70), 1),
        "humidity_pct": round(random.uniform(40, 85), 1),
        "snow_depth_cm": round(random.uniform(20, 150), 1),
        "visibility_km": round(random.uniform(0.5, 20), 1),
        "storm_warning": random.random() < 0.03,
    }


def gen_infrastructure(station_id, cfg, t):
    fault = active_faults[station_id].get("infrastructure", False)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "structural_health_index": round(random.uniform(85, 100) if not fault else random.uniform(40, 60), 1),
        "hvac_status": "nominal" if not fault else "degraded",
        "comms_link_status": "online" if random.random() > 0.02 else "degraded",
        "water_reserve_pct": round(random.uniform(50, 95), 1),
        "power_grid_status": "nominal" if not active_faults[station_id].get("energy") else "fault",
    }


def parse_fault_arg(fault_str):
    """Parse '--fault maitri:energy' into active_faults."""
    if not fault_str:
        return
    for spec in fault_str.split(","):
        station_id, subsystem = spec.split(":")
        active_faults.setdefault(station_id, {})[subsystem] = True
        print(f"[fault injected] {station_id}/{subsystem}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fault", help="e.g. maitri:energy or bharati:fuel", default=None)
    parser.add_argument("--host", default=MQTT_HOST)
    parser.add_argument("--port", type=int, default=MQTT_PORT)
    args = parser.parse_args()
    parse_fault_arg(args.fault)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="antarctic-simulator")
    client.connect(args.host, args.port, keepalive=60)
    client.loop_start()

    print(f"Simulator started. Publishing every {PUBLISH_INTERVAL_SEC}s to {args.host}:{args.port}")
    t = 0
    try:
        while True:
            for station_id, cfg in STATIONS.items():
                energy = gen_energy(station_id, cfg, t)
                fuel = gen_fuel(station_id, cfg, t, energy)
                env = gen_environment(station_id, cfg, t)
                infra = gen_infrastructure(station_id, cfg, t)

                client.publish(f"station/{station_id}/energy", json.dumps(energy), qos=1)
                client.publish(f"station/{station_id}/fuel", json.dumps(fuel), qos=1)
                client.publish(f"station/{station_id}/environment", json.dumps(env), qos=1)
                client.publish(f"station/{station_id}/infrastructure", json.dumps(infra), qos=1)

            t += PUBLISH_INTERVAL_SEC
            time.sleep(PUBLISH_INTERVAL_SEC)
    except KeyboardInterrupt:
        print("Simulator stopped.")
        client.loop_stop()


if __name__ == "__main__":
    main()
