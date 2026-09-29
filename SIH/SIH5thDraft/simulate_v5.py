"""
Digital Twin telemetry simulator for Maitri and Bharati Antarctic stations.

v3 model: each publish tick advances the station's simulated clock by
ONE DAY (not one continuous real-time slice like v1/v2). TICK_REAL_SECONDS
controls how fast real time maps to simulated days for demo pacing - it has
no bearing on the physics. Every field is a bounded, mean-reverting daily
process, so:
  - nothing can wander off to unrealistic extremes (temperature stays in a
    real Antarctic range, wind stays in a real range, etc.)
  - normal day-to-day change is small and believable (fuel/water move by
    fractions of a percent per day, not tens of percent)
  - fault injection still gives you a dramatic, demoable delta, just scaled
    to "notably worse than normal for one day" rather than "instantly zero"

Publishes JSON messages to MQTT topics of the form:
    station/<station_id>/<subsystem>

Subsystems: energy, fuel, environment, infrastructure, logistics, logistics_inventory

Run:
    python simulate.py
    python simulate.py --tick-seconds 4          # advance faster/slower
    python simulate.py --fault maitri:energy      # inject a fault on start
    python simulate.py --fault bharati:water      # pump/intake issue
    python simulate.py --fault maitri:logistics   # supplies draining ~5x faster, RFID flags spike
"""

import json
import math
import random
import time
import argparse
from datetime import datetime, timezone, timedelta

import paho.mqtt.client as mqtt

MQTT_HOST = "localhost"
MQTT_PORT = 1883
TICK_REAL_SECONDS = 6  # real seconds between ticks; each tick = 1 simulated day

SHIP_NAMES = ["MV Golovnin"]  # Cape Town -> Antarctica leg vessel per the operational route

STATIONS = {
    "maitri": {
        "name": "Maitri",
        "base_temp_c": -22,          # annual mean
        "temp_seasonal_amp": 14,     # +/- swing between summer/winter
        "solar_capacity_kw": 15,
        "wind_capacity_kw": 20,
        "diesel_capacity_kw": 60,
        "fuel_tank_liters": 50000,
        "base_load_kw": 32,
        "water_body": "Lake",
    },
    "bharati": {
        "name": "Bharati",
        "base_temp_c": -12,
        "temp_seasonal_amp": 11,
        "solar_capacity_kw": 20,
        "wind_capacity_kw": 25,
        "diesel_capacity_kw": 80,
        "base_load_kw": 40,
        "fuel_tank_liters": 70000,
        "water_body": "Sea",
    },
}

TEMP_ABS_MIN, TEMP_ABS_MAX = -55, 8
WIND_ABS_MIN, WIND_ABS_MAX = 5, 130
HUMIDITY_MIN, HUMIDITY_MAX = 25, 95
VISIBILITY_MIN, VISIBILITY_MAX = 0.3, 25
SNOW_MIN, SNOW_MAX = 0, 300
STRUCTURAL_MIN, STRUCTURAL_MAX = 50, 100

# active fault flags, e.g. {"maitri": {"energy": True}}
active_faults = {sid: {} for sid in STATIONS}

# ---------------------------------------------------------------------------
# Logistics inventory (v5): stock levels for the station's supply room.
# Each stock falls by a small daily amount (a station of ~30 people) and is
# topped back up when the supply ship docks - the same event that refuels
# the fuel farm and refills water. Ranges are (min, max) %-points per day.
# ---------------------------------------------------------------------------
INVENTORY_DAILY_USE = {
    "food_pct": (0.35, 0.55),
    "medical_pct": (0.04, 0.12),
    "scientific_supplies_pct": (0.10, 0.28),
    "ppe_pct": (0.05, 0.12),
    "cleaning_consumables_pct": (0.15, 0.32),
    "emergency_supplies_pct": (0.002, 0.01),
    "generator_spares_pct": (0.03, 0.09),
    "pump_components_pct": (0.03, 0.09),
    "electrical_parts_pct": (0.05, 0.14),
    "vehicle_parts_pct": (0.05, 0.18),
    "warehouse_general_pct": (0.10, 0.25),
    "warehouse_cold_pct": (0.20, 0.35),
    "warehouse_spares_pct": (0.05, 0.14),
    "warehouse_emergency_pct": (0.002, 0.01),
}
INVENTORY_START = {
    "food_pct": (68, 90), "medical_pct": (80, 95), "scientific_supplies_pct": (58, 82),
    "ppe_pct": (75, 94), "cleaning_consumables_pct": (60, 85), "emergency_supplies_pct": (92, 99),
    "generator_spares_pct": (62, 90), "pump_components_pct": (60, 88), "electrical_parts_pct": (66, 92),
    "vehicle_parts_pct": (42, 76), "warehouse_general_pct": (58, 82), "warehouse_cold_pct": (55, 80),
    "warehouse_spares_pct": (60, 84), "warehouse_emergency_pct": (90, 99),
}


def init_inventory(sid):
    s = state[sid]
    for k, (lo, hi) in INVENTORY_START.items():
        s[k] = random.uniform(lo, hi)
    s["food_rate_today"] = sum(INVENTORY_DAILY_USE["food_pct"]) / 2
    s["cold_temp_c"] = -18.0
    s["cold_humidity_pct"] = 45.0
    s["cold_door_open"] = False
    s["rfid_tracked"] = random.randint(1300, 1900)
    s["rfid_flagged"] = 2.0


def restock_inventory(sid):
    """Called when the supply ship docks: fill every stock back up and add
    the newly delivered (and newly RFID-tagged) boxes."""
    s = state[sid]
    for k in INVENTORY_START:
        s[k] = random.uniform(88, 97)
    s["rfid_tracked"] = min(3500, s["rfid_tracked"] + random.randint(800, 1200))


# per-station persistent daily state
state = {}
for sid, cfg in STATIONS.items():
    state[sid] = {
        "day": 0,
        "temp_c": cfg["base_temp_c"],
        "wind_kmh": 30.0,
        "humidity_pct": 60.0,
        "visibility_km": 14.0,
        "snow_cm": random.uniform(40, 90),
        "storm_days_left": 0,
        "fuel_liters": cfg["fuel_tank_liters"] * random.uniform(0.55, 0.8),
        "water_pct": random.uniform(70, 92),
        "structural_health": random.uniform(88, 97),
        "hvac_degraded": False,
        "comms_degraded": False,
        "ship_name": random.choice(SHIP_NAMES),
        "ship_status": "In Transit",
        "ship_days_to_arrival": random.randint(40, 110),
        "ship_total_voyage_days": None,
        "ship_delay_days_left": 0,
        "ship_docked_days_left": 0,
        "ship_departed_days_left": 0,
    }
    state[sid]["ship_total_voyage_days"] = state[sid]["ship_days_to_arrival"]
    init_inventory(sid)


def _ar1(current, mean, reversion, daily_vol, lo, hi):
    """One day-step of a bounded, mean-reverting random process."""
    delta = reversion * (mean - current) + random.gauss(0, daily_vol)
    return max(lo, min(hi, current + delta))


def season_temp_mean(cfg, day_of_year):
    """Southern-hemisphere seasonal cycle: coldest ~mid-year, warmest ~new year."""
    return cfg["base_temp_c"] + cfg["temp_seasonal_amp"] * math.cos(2 * math.pi * (day_of_year - 200) / 365)


def season_snow_bias(day_of_year):
    """Net accumulation bias: positive near winter, negative (melt) near summer."""
    return 0.6 * math.cos(2 * math.pi * (day_of_year - 200) / 365)


def step_environment(station_id, cfg):
    s = state[station_id]
    day_of_year = s["day"] % 365

    # storm lifecycle
    if s["storm_days_left"] > 0:
        s["storm_days_left"] -= 1
    elif random.random() < 0.025:
        s["storm_days_left"] = random.randint(2, 5)
    storming = s["storm_days_left"] > 0

    temp_mean = season_temp_mean(cfg, day_of_year)
    s["temp_c"] = round(_ar1(s["temp_c"], temp_mean, 0.3, 1.2, TEMP_ABS_MIN, TEMP_ABS_MAX), 1)

    wind_mean = 55 if storming else 30
    s["wind_kmh"] = round(_ar1(s["wind_kmh"], wind_mean, 0.3, 4.0, WIND_ABS_MIN, WIND_ABS_MAX), 1)

    s["humidity_pct"] = round(_ar1(s["humidity_pct"], 60, 0.2, 3.0, HUMIDITY_MIN, HUMIDITY_MAX), 1)

    vis_mean = 2.0 if storming else 14.0
    s["visibility_km"] = round(_ar1(s["visibility_km"], vis_mean, 0.35, 1.5, VISIBILITY_MIN, VISIBILITY_MAX), 1)

    snow_delta = random.uniform(3, 9) if storming else season_snow_bias(day_of_year) * random.uniform(0.3, 1.2)
    s["snow_cm"] = round(max(SNOW_MIN, min(SNOW_MAX, s["snow_cm"] + snow_delta)), 1)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "day": s["day"],
        "temperature_c": s["temp_c"],
        "wind_speed_kmh": s["wind_kmh"],
        "humidity_pct": s["humidity_pct"],
        "snow_depth_cm": s["snow_cm"],
        "visibility_km": s["visibility_km"],
        "storm_warning": storming,
    }


def step_energy(station_id, cfg, env_payload):
    s = state[station_id]
    fault = active_faults[station_id].get("energy", False)
    day_of_year = s["day"] % 365

    daylight_factor = max(0.15, math.sin(math.pi * (day_of_year % 365) / 365) ** 0.5) if cfg["base_temp_c"] else 0.5
    solar_kw = round(cfg["solar_capacity_kw"] * daylight_factor * random.uniform(0.5, 0.9), 2)
    wind_kw = round(cfg["wind_capacity_kw"] * min(1.0, env_payload["wind_speed_kmh"] / 70) * random.uniform(0.5, 0.9), 2)

    load_kw = round(_ar1(cfg["base_load_kw"], cfg["base_load_kw"], 0.4, 1.5,
                          cfg["base_load_kw"] * 0.75, cfg["base_load_kw"] * 1.3), 2)

    diesel_kw = max(0.0, round(load_kw - solar_kw - wind_kw, 2))
    generator_status = "running" if diesel_kw > 0.5 else "standby"

    if fault:
        generator_status = "fault"
        diesel_kw = 0.0

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "day": s["day"],
        "solar_kw": solar_kw,
        "wind_kw": wind_kw,
        "diesel_kw": diesel_kw,
        "load_kw": load_kw,
        "generator_status": generator_status,
        "grid_frequency_hz": round(random.uniform(49.85, 50.15), 2) if not fault else 0,
    }


def step_fuel(station_id, cfg, energy_payload):
    s = state[station_id]
    fault = active_faults[station_id].get("fuel", False)

    # realistic daily diesel consumption: L/kWh heuristic over a full day
    daily_consumption = energy_payload["diesel_kw"] * 24 * 0.3
    if fault:
        daily_consumption += random.uniform(120, 220)  # a real leak, but over one day - not instant-empty
    s["fuel_liters"] = max(0.0, s["fuel_liters"] - daily_consumption)

    days_remaining = None
    if daily_consumption > 0.5:
        days_remaining = round(s["fuel_liters"] / daily_consumption, 1)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "day": s["day"],
        "fuel_level_liters": round(s["fuel_liters"], 1),
        "fuel_capacity_liters": cfg["fuel_tank_liters"],
        "fuel_pct": round(100 * s["fuel_liters"] / cfg["fuel_tank_liters"], 1),
        "leak_detected": fault,
        "est_days_remaining": days_remaining,
        "resupply_pending": s["fuel_liters"] < 0.15 * cfg["fuel_tank_liters"],
    }


def step_infrastructure(station_id, cfg):
    s = state[station_id]
    infra_fault = active_faults[station_id].get("infrastructure", False)
    water_fault = active_faults[station_id].get("water", False)

    daily_pct_rate = random.uniform(0.15, 0.4) * (6 if water_fault else 1)
    s["water_pct"] = max(0.0, min(100.0, s["water_pct"] - daily_pct_rate))

    structural_mean = 55 if infra_fault else 92
    s["structural_health"] = round(_ar1(s["structural_health"], structural_mean, 0.15, 0.4,
                                         STRUCTURAL_MIN, STRUCTURAL_MAX), 1)

    if infra_fault:
        s["hvac_degraded"] = True
    elif s["hvac_degraded"] and random.random() < 0.5:
        s["hvac_degraded"] = False

    if random.random() < 0.03:
        s["comms_degraded"] = True
    elif s["comms_degraded"] and random.random() < 0.6:
        s["comms_degraded"] = False

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "day": s["day"],
        "structural_health_index": s["structural_health"],
        "hvac_status": "degraded" if s["hvac_degraded"] else "nominal",
        "comms_link_status": "degraded" if s["comms_degraded"] else "online",
        "water_reserve_pct": round(s["water_pct"], 1),
        "power_grid_status": "fault" if active_faults[station_id].get("energy") else "nominal",
    }


def step_logistics(station_id, cfg, fuel_payload):
    """A supply ship on a slow, realistic voyage cycle. When it docks it
    resupplies fuel and water, which is what actually resets the resupply
    countdown shown on the overview page.
    """
    s = state[station_id]

    if s["ship_status"] == "In Transit":
        if s["ship_delay_days_left"] > 0:
            s["ship_delay_days_left"] -= 1
            s["ship_status"] = "Delayed (Weather)"
        elif random.random() < 0.01:
            s["ship_delay_days_left"] = random.randint(2, 6)
            s["ship_status"] = "Delayed (Weather)"
        else:
            s["ship_days_to_arrival"] = max(0, s["ship_days_to_arrival"] - 1)
            if s["ship_days_to_arrival"] == 0:
                s["ship_status"] = "Docked - Unloading"
                s["ship_docked_days_left"] = random.randint(2, 4)
                # resupply: top up fuel + water
                s["fuel_liters"] = min(cfg["fuel_tank_liters"], cfg["fuel_tank_liters"] * random.uniform(0.9, 0.97))
                s["water_pct"] = random.uniform(90, 98)
                restock_inventory(station_id)
    elif s["ship_status"] == "Delayed (Weather)":
        s["ship_delay_days_left"] -= 1
        if s["ship_delay_days_left"] <= 0:
            s["ship_status"] = "In Transit"
    elif s["ship_status"] == "Docked - Unloading":
        s["ship_docked_days_left"] -= 1
        if s["ship_docked_days_left"] <= 0:
            s["ship_status"] = "Departed"
            s["ship_departed_days_left"] = random.randint(3, 6)
    elif s["ship_status"] == "Departed":
        s["ship_departed_days_left"] -= 1
        if s["ship_departed_days_left"] <= 0:
            s["ship_name"] = random.choice(SHIP_NAMES)
            s["ship_status"] = "In Transit"
            s["ship_total_voyage_days"] = random.randint(70, 140)
            s["ship_days_to_arrival"] = s["ship_total_voyage_days"]

    progress_pct = None
    if s["ship_status"] in ("In Transit", "Delayed (Weather)") and s["ship_total_voyage_days"]:
        progress_pct = round(100 * (1 - s["ship_days_to_arrival"] / s["ship_total_voyage_days"]), 1)
    elif s["ship_status"] in ("Docked - Unloading", "Departed"):
        progress_pct = 100.0

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "day": s["day"],
        "ship_name": s["ship_name"],
        "status": s["ship_status"],
        "days_to_arrival": s["ship_days_to_arrival"] if s["ship_status"] in ("In Transit", "Delayed (Weather)") else 0,
        "total_voyage_days": s["ship_total_voyage_days"],
        "progress_pct": progress_pct,
    }


def step_logistics_inventory(station_id, cfg):
    """Daily draw-down of every supply category, cold-room conditions, and
    the RFID box tracker. A "logistics" fault multiplies consumption ~5x
    (contamination / loss / theft-style event) and floods the RFID tracker
    with flagged boxes; a power/infrastructure fault warms the cold room."""
    s = state[station_id]
    fault = active_faults[station_id].get("logistics", False)
    power_trouble = active_faults[station_id].get("energy") or active_faults[station_id].get("infrastructure")
    mult = 5.0 if fault else 1.0

    for k, (lo, hi) in INVENTORY_DAILY_USE.items():
        use = random.uniform(lo, hi) * mult
        s[k] = max(0.0, s[k] - use)
        if k == "food_pct":
            s["food_rate_today"] = use

    spares = [s["generator_spares_pct"], s["pump_components_pct"], s["electrical_parts_pct"], s["vehicle_parts_pct"]]
    engineering_spares = sum(spares) / len(spares)

    # cold room: holds near -18C; door left open now and then; warms on power trouble
    s["cold_door_open"] = random.random() < 0.02
    cold_mean = -6.0 if power_trouble else -18.0
    cold_temp = _ar1(s["cold_temp_c"], cold_mean, 0.35, 0.4, -26, 4)
    if s["cold_door_open"]:
        cold_temp = min(4.0, cold_temp + random.uniform(1.0, 3.0))
    s["cold_temp_c"] = cold_temp
    s["cold_humidity_pct"] = _ar1(s["cold_humidity_pct"], 45, 0.2, 2.0, 25, 75)

    # RFID: boxes get used up as stock is consumed; flagged = unread / misplaced / damaged
    s["rfid_tracked"] = max(0, s["rfid_tracked"] - random.randint(1, 5) * (5 if fault else 1))
    flag_mean = 22.0 if fault else 2.0
    s["rfid_flagged"] = _ar1(s["rfid_flagged"], flag_mean, 0.3, 1.2, 0, 60)

    food_autonomy = round(s["food_pct"] / s["food_rate_today"], 1) if s["food_rate_today"] > 1e-6 else None
    health = (s["food_pct"] + s["medical_pct"] + s["scientific_supplies_pct"] + engineering_spares
              + s["ppe_pct"] + s["cleaning_consumables_pct"] + s["emergency_supplies_pct"]) / 7.0

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "day": s["day"],
        "food_pct": round(s["food_pct"], 1),
        "food_autonomy_days": food_autonomy,
        "medical_pct": round(s["medical_pct"], 1),
        "scientific_supplies_pct": round(s["scientific_supplies_pct"], 1),
        "engineering_spares_pct": round(engineering_spares, 1),
        "ppe_pct": round(s["ppe_pct"], 1),
        "cleaning_consumables_pct": round(s["cleaning_consumables_pct"], 1),
        "emergency_supplies_pct": round(s["emergency_supplies_pct"], 1),
        "generator_spares_pct": round(s["generator_spares_pct"], 1),
        "pump_components_pct": round(s["pump_components_pct"], 1),
        "electrical_parts_pct": round(s["electrical_parts_pct"], 1),
        "vehicle_parts_pct": round(s["vehicle_parts_pct"], 1),
        "cold_storage_temp_c": round(s["cold_temp_c"], 1),
        "cold_storage_humidity_pct": round(s["cold_humidity_pct"], 1),
        "cold_storage_door_status": "open" if s["cold_door_open"] else "closed",
        "warehouse_general_pct": round(s["warehouse_general_pct"], 1),
        "warehouse_cold_pct": round(s["warehouse_cold_pct"], 1),
        "warehouse_spares_pct": round(s["warehouse_spares_pct"], 1),
        "warehouse_emergency_pct": round(s["warehouse_emergency_pct"], 1),
        "rfid_boxes_tracked": int(s["rfid_tracked"]),
        "rfid_boxes_flagged": int(round(s["rfid_flagged"])),
        "logistics_health_pct": round(health, 1),
    }


def parse_fault_arg(fault_str):
    if not fault_str:
        return
    for spec in fault_str.split(","):
        station_id, subsystem = spec.split(":")
        active_faults.setdefault(station_id, {})[subsystem] = True
        print(f"[fault injected] {station_id}/{subsystem}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fault", help="e.g. maitri:energy, bharati:fuel, maitri:water, maitri:logistics", default=None)
    parser.add_argument("--host", default=MQTT_HOST)
    parser.add_argument("--port", type=int, default=MQTT_PORT)
    parser.add_argument("--tick-seconds", type=float, default=TICK_REAL_SECONDS,
                         help="real seconds between ticks; each tick advances the station clock by 1 day")
    args = parser.parse_args()
    parse_fault_arg(args.fault)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="antarctic-simulator")
    client.connect(args.host, args.port, keepalive=60)
    client.loop_start()

    print(f"Simulator started. 1 tick = 1 simulated day, every {args.tick_seconds}s of real time, "
          f"publishing to {args.host}:{args.port}")

    try:
        while True:
            for station_id, cfg in STATIONS.items():
                s = state[station_id]
                env = step_environment(station_id, cfg)
                energy = step_energy(station_id, cfg, env)
                fuel = step_fuel(station_id, cfg, energy)
                infra = step_infrastructure(station_id, cfg)
                logistics = step_logistics(station_id, cfg, fuel)
                inventory = step_logistics_inventory(station_id, cfg)

                client.publish(f"station/{station_id}/environment", json.dumps(env), qos=1)
                client.publish(f"station/{station_id}/energy", json.dumps(energy), qos=1)
                client.publish(f"station/{station_id}/fuel", json.dumps(fuel), qos=1)
                client.publish(f"station/{station_id}/infrastructure", json.dumps(infra), qos=1)
                client.publish(f"station/{station_id}/logistics", json.dumps(logistics), qos=1)
                client.publish(f"station/{station_id}/logistics_inventory", json.dumps(inventory), qos=1)

                s["day"] += 1

            time.sleep(args.tick_seconds)
    except KeyboardInterrupt:
        print("Simulator stopped.")
        client.loop_stop()


if __name__ == "__main__":
    main()
