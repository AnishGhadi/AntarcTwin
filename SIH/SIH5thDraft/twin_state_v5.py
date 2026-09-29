"""
The digital twin's live state model.

v5 changes:
- New subsystem "logistics_inventory" (distinct from "logistics", which is
  still the supply-ship tracker) modeling the station's actual supply room:
  food/medical/scientific/spares/PPE/cleaning/emergency stock levels, spare
  parts by category, cold storage conditions, warehouse capacity by zone,
  and RFID-tracked box counts.
- A new "logistics_room" in ROOM_LAYOUTS surfacing all of the above.
- `est_days_remaining` and `food_autonomy_days` are display-only now (not in
  NUMERIC_COMPONENT_BOUNDS): the resource-balance forecasting model computes
  its own authoritative depletion ETA, so independently trend-forecasting a
  simulator-computed ETA would just be a redundant second "days remaining".
- RESOURCE_FIELDS / forecast_model_for(): single source of truth for which
  forecasting approach applies to which field - "resource" for depletable
  stocks governed by conservation (can't rise without a resupply event),
  "kalman" for continuous physical measurements.
"""

from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

THRESHOLDS = {
    "fuel_pct_low": 20,
    "fuel_pct_critical": 10,
    "structural_health_low": 75,
    "structural_health_critical": 65,
    "temperature_extreme_low": -40,
    "temperature_extreme_critical": -50,
    "wind_speed_high": 60,
    "wind_speed_critical": 90,
    "water_reserve_low": 30,
    "water_reserve_critical": 15,
    "stock_low": 20,
    "stock_critical": 10,
}

LEVEL_RANK = {"unknown": 0, "nominal": 1, "warning": 2, "critical": 3}

# Generic "how much do we have left" gauges share fuel_pct's low/critical bands.
STOCK_PCT_FIELDS = {
    "food_pct", "medical_pct", "scientific_supplies_pct", "engineering_spares_pct",
    "ppe_pct", "cleaning_consumables_pct", "emergency_supplies_pct",
    "generator_spares_pct", "pump_components_pct", "electrical_parts_pct",
    "vehicle_parts_pct", "warehouse_general_pct", "warehouse_cold_pct",
    "warehouse_spares_pct", "warehouse_emergency_pct",
}

# Depletable stocks under physical conservation -> resource-balance model.
RESOURCE_FIELDS = STOCK_PCT_FIELDS | {"fuel_level_liters", "fuel_pct", "water_reserve_pct"}


def forecast_model_for(field: str) -> str:
    return "resource" if field in RESOURCE_FIELDS else "kalman"


NUMERIC_COMPONENT_BOUNDS = {
    "temperature_c": (-55, 8),
    "wind_speed_kmh": (0, 130),
    "humidity_pct": (0, 100),
    "snow_depth_cm": (0, 300),
    "visibility_km": (0, 30),
    "fuel_level_liters": (0, None),
    "fuel_pct": (0, 100),
    "diesel_kw": (0, 150),
    "solar_kw": (0, 60),
    "wind_kw": (0, 60),
    "load_kw": (0, 150),
    "grid_frequency_hz": (0, 55),
    "structural_health_index": (0, 100),
    "water_reserve_pct": (0, 100),
    # -- logistics inventory (v5) --
    "food_pct": (0, 100),
    "medical_pct": (0, 100),
    "scientific_supplies_pct": (0, 100),
    "engineering_spares_pct": (0, 100),
    "ppe_pct": (0, 100),
    "cleaning_consumables_pct": (0, 100),
    "emergency_supplies_pct": (0, 100),
    "generator_spares_pct": (0, 100),
    "pump_components_pct": (0, 100),
    "electrical_parts_pct": (0, 100),
    "vehicle_parts_pct": (0, 100),
    "cold_storage_temp_c": (-30, 5),
    "cold_storage_humidity_pct": (0, 100),
    "warehouse_general_pct": (0, 100),
    "warehouse_cold_pct": (0, 100),
    "warehouse_spares_pct": (0, 100),
    "warehouse_emergency_pct": (0, 100),
    "rfid_boxes_tracked": (0, 5000),
    "rfid_boxes_flagged": (0, 200),
    "logistics_health_pct": (0, 100),
}

COMPONENT_META = {
    "temperature_c": ("Temperature", "°C"),
    "wind_speed_kmh": ("Wind Speed", "km/h"),
    "humidity_pct": ("Humidity", "%"),
    "snow_depth_cm": ("Snow Depth", "cm"),
    "visibility_km": ("Visibility", "km"),
    "storm_warning": ("Storm Warning", ""),
    "fuel_level_liters": ("Fuel Level", "L"),
    "fuel_pct": ("Fuel Reserve", "%"),
    "est_days_remaining": ("Est. Days Remaining", "days"),
    "leak_detected": ("Leak Detected", ""),
    "resupply_pending": ("Resupply Pending", ""),
    "generator_status": ("Generator", ""),
    "diesel_kw": ("Diesel Output", "kW"),
    "solar_kw": ("Solar Output", "kW"),
    "wind_kw": ("Wind Output", "kW"),
    "load_kw": ("Power Load", "kW"),
    "grid_frequency_hz": ("Grid Frequency", "Hz"),
    "structural_health_index": ("Structural Health", "idx"),
    "hvac_status": ("HVAC", ""),
    "comms_link_status": ("Satellite Link", ""),
    "water_reserve_pct": ("Water Reserve", "%"),
    "power_grid_status": ("Power Grid", ""),
    "food_pct": ("Food & Provisions", "%"),
    "medical_pct": ("Medical Supplies", "%"),
    "scientific_supplies_pct": ("Scientific Supplies", "%"),
    "engineering_spares_pct": ("Engineering Spares (Overall)", "%"),
    "ppe_pct": ("PPE / Cold-Weather Clothing", "%"),
    "cleaning_consumables_pct": ("Cleaning & Consumables", "%"),
    "emergency_supplies_pct": ("Emergency Supplies", "%"),
    "food_autonomy_days": ("Food Autonomy", "days"),
    "generator_spares_pct": ("Generator Spares", "%"),
    "pump_components_pct": ("Pump Components", "%"),
    "electrical_parts_pct": ("Electrical Parts", "%"),
    "vehicle_parts_pct": ("Vehicle Parts", "%"),
    "cold_storage_temp_c": ("Cold Storage Temp", "°C"),
    "cold_storage_humidity_pct": ("Cold Storage Humidity", "%"),
    "cold_storage_door_status": ("Cold Storage Door", ""),
    "warehouse_general_pct": ("Warehouse — General", "%"),
    "warehouse_cold_pct": ("Warehouse — Cold Storage", "%"),
    "warehouse_spares_pct": ("Warehouse — Spares", "%"),
    "warehouse_emergency_pct": ("Warehouse — Emergency", "%"),
    "rfid_boxes_tracked": ("RFID-Tracked Boxes", ""),
    "rfid_boxes_flagged": ("RFID Boxes Flagged", ""),
    "logistics_health_pct": ("Logistics Health", "%"),
}


def component_level(key: str, value: Any) -> str:
    if value is None:
        return "unknown"

    if key == "temperature_c":
        if value <= THRESHOLDS["temperature_extreme_critical"]:
            return "critical"
        if value <= THRESHOLDS["temperature_extreme_low"]:
            return "warning"
        return "nominal"
    if key == "wind_speed_kmh":
        if value >= THRESHOLDS["wind_speed_critical"]:
            return "critical"
        if value >= THRESHOLDS["wind_speed_high"]:
            return "warning"
        return "nominal"
    if key == "snow_depth_cm":
        if value >= 180:
            return "critical"
        if value >= 120:
            return "warning"
        return "nominal"
    if key == "visibility_km":
        if value <= 1:
            return "critical"
        if value <= 3:
            return "warning"
        return "nominal"
    if key == "storm_warning":
        return "critical" if value else "nominal"
    if key == "fuel_pct":
        if value < THRESHOLDS["fuel_pct_critical"]:
            return "critical"
        if value < THRESHOLDS["fuel_pct_low"]:
            return "warning"
        return "nominal"
    if key in ("est_days_remaining", "food_autonomy_days"):
        if value < 5:
            return "critical"
        if value < 15:
            return "warning"
        return "nominal"
    if key == "leak_detected":
        return "critical" if value else "nominal"
    if key == "resupply_pending":
        return "warning" if value else "nominal"
    if key == "generator_status":
        return "critical" if value == "fault" else "nominal"
    if key == "grid_frequency_hz":
        if value == 0:
            return "critical"
        if value < 49.5 or value > 50.5:
            return "warning"
        return "nominal"
    if key == "structural_health_index":
        if value < THRESHOLDS["structural_health_critical"]:
            return "critical"
        if value < THRESHOLDS["structural_health_low"]:
            return "warning"
        return "nominal"
    if key == "hvac_status":
        return "warning" if value == "degraded" else "nominal"
    if key == "comms_link_status":
        return "warning" if value != "online" else "nominal"
    if key == "water_reserve_pct":
        if value < THRESHOLDS["water_reserve_critical"]:
            return "critical"
        if value < THRESHOLDS["water_reserve_low"]:
            return "warning"
        return "nominal"
    if key == "power_grid_status":
        return "critical" if value == "fault" else "nominal"
    if key in STOCK_PCT_FIELDS:
        if value < THRESHOLDS["stock_critical"]:
            return "critical"
        if value < THRESHOLDS["stock_low"]:
            return "warning"
        return "nominal"
    if key == "cold_storage_temp_c":
        if value > -2:
            return "critical"
        if value > -10:
            return "warning"
        return "nominal"
    if key == "cold_storage_door_status":
        return "warning" if value == "open" else "nominal"
    if key == "rfid_boxes_flagged":
        if value > 15:
            return "critical"
        if value > 5:
            return "warning"
        return "nominal"
    if key == "logistics_health_pct":
        if value < 40:
            return "critical"
        if value < 65:
            return "warning"
        return "nominal"
    return "nominal"


def worst_level(levels: List[str]) -> str:
    real = [l for l in levels if l != "unknown"]
    if not real:
        return "unknown"
    return max(real, key=lambda l: LEVEL_RANK[l])


def _room(id_, name, icon, row, col, col_span, subsystems, fields, note=None):
    return {
        "id": id_,
        "name": name,
        "icon": icon,
        "grid": {"row": row, "col": col, "colSpan": col_span},
        "subsystems": subsystems,
        "fields": fields,
        "note": note,
    }


_LOGISTICS_FIELDS = [("logistics_inventory", k) for k in (
    "food_pct", "food_autonomy_days", "medical_pct", "scientific_supplies_pct",
    "engineering_spares_pct", "ppe_pct", "cleaning_consumables_pct", "emergency_supplies_pct",
    "generator_spares_pct", "pump_components_pct", "electrical_parts_pct", "vehicle_parts_pct",
    "cold_storage_temp_c", "cold_storage_humidity_pct", "cold_storage_door_status",
    "warehouse_general_pct", "warehouse_cold_pct", "warehouse_spares_pct", "warehouse_emergency_pct",
    "rfid_boxes_tracked", "rfid_boxes_flagged", "logistics_health_pct",
)]


def _station_rooms(main_name, env_name, aux_name, aux_note, water_name):
    return [
        _room("environment_zone", env_name, "🌡️", 1, 1, 2,
              ["environment"], [("environment", k) for k in
              ("temperature_c", "wind_speed_kmh", "humidity_pct", "snow_depth_cm",
               "visibility_km", "storm_warning")]),
        _room("auxiliary_facilities", aux_name, "🏕️", 2, 1, 2, [], [], note=aux_note),
        _room("main_building", main_name, "🏠", 3, 1, 2,
              ["infrastructure", "energy"], [
                  ("infrastructure", "hvac_status"),
                  ("infrastructure", "structural_health_index"),
                  ("infrastructure", "water_reserve_pct"),
                  ("energy", "load_kw"),
              ]),
        _room("power_energy", "Power / Energy Plant", "⚡", 4, 1, 1,
              ["energy"], [("energy", k) for k in
              ("generator_status", "diesel_kw", "solar_kw", "wind_kw", "grid_frequency_hz")]),
        _room("communication", "Communication", "📡", 4, 2, 1,
              ["infrastructure"], [("infrastructure", "comms_link_status")]),
        _room("fuel_system", "Fuel Farm & Station", "🛢️", 5, 1, 1,
              ["fuel"], [("fuel", k) for k in
              ("fuel_level_liters", "fuel_pct", "est_days_remaining", "leak_detected", "resupply_pending")]),
        _room("water_pump_house", water_name, "💧", 5, 2, 1,
              ["infrastructure"], [("infrastructure", "water_reserve_pct")]),
        _room("logistics_room", "Logistics & Supply Control", "📦", 6, 1, 2,
              ["logistics_inventory"], _LOGISTICS_FIELDS),
    ]


ROOM_LAYOUTS = {
    "maitri": _station_rooms(
        "Maitri Main Building", "Environment Zone", "Summer Camp & Container Modules",
        "Personnel shelter & research/storage containers — no live sensors.",
        "Lake Water Pump House"),
    "bharati": _station_rooms(
        "Bharati Main Building", "Weather / Environment", "Container Modules & Summer Camp",
        "Research/storage containers & emergency shelter — no live sensors.",
        "Sea Water Pump House"),
}


class SubsystemState:
    def __init__(self, name: str):
        self.name = name
        self.latest: Dict[str, Any] = {}
        self.status: str = "unknown"
        self.last_updated: Optional[datetime] = None

    def update(self, payload: Dict[str, Any]):
        self.latest = payload
        self.last_updated = datetime.now(timezone.utc)
        self.status = self._evaluate_status(payload)

    def _evaluate_status(self, payload: Dict[str, Any]) -> str:
        if self.name == "energy":
            return "critical" if payload.get("generator_status") == "fault" else "nominal"
        if self.name == "fuel":
            pct = payload.get("fuel_pct", 100)
            if payload.get("leak_detected") or pct < THRESHOLDS["fuel_pct_critical"]:
                return "critical"
            if pct < THRESHOLDS["fuel_pct_low"]:
                return "warning"
            return "nominal"
        if self.name == "environment":
            if payload.get("storm_warning") or payload.get("wind_speed_kmh", 0) > THRESHOLDS["wind_speed_critical"]:
                return "warning"
            if payload.get("temperature_c", 0) < THRESHOLDS["temperature_extreme_low"]:
                return "warning"
            return "nominal"
        if self.name == "infrastructure":
            if payload.get("structural_health_index", 100) < THRESHOLDS["structural_health_critical"]:
                return "critical"
            if payload.get("water_reserve_pct", 100) < THRESHOLDS["water_reserve_critical"]:
                return "critical"
            if payload.get("comms_link_status") != "online":
                return "warning"
            if payload.get("water_reserve_pct", 100) < THRESHOLDS["water_reserve_low"]:
                return "warning"
            return "nominal"
        if self.name == "logistics":
            return "warning" if str(payload.get("status", "")).startswith("Delayed") else "nominal"
        if self.name == "logistics_inventory":
            levels = [component_level(k, v) for k, v in payload.items()
                      if k in NUMERIC_COMPONENT_BOUNDS or k in ("cold_storage_door_status", "food_autonomy_days")]
            worst = worst_level(levels)
            return worst if worst != "unknown" else "nominal"
        return "nominal"

    def to_dict(self):
        return {
            "status": self.status,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
            "data": self.latest,
        }


class StationTwin:
    def __init__(self, station_id: str, display_name: str):
        self.station_id = station_id
        self.display_name = display_name
        self.subsystems: Dict[str, SubsystemState] = {
            "energy": SubsystemState("energy"),
            "fuel": SubsystemState("fuel"),
            "environment": SubsystemState("environment"),
            "infrastructure": SubsystemState("infrastructure"),
            "logistics": SubsystemState("logistics"),
            "logistics_inventory": SubsystemState("logistics_inventory"),
        }

    def update(self, subsystem: str, payload: Dict[str, Any]):
        if subsystem in self.subsystems:
            self.subsystems[subsystem].update(payload)

    @property
    def overall_status(self) -> str:
        # the ship tracker's delays are informational, not station-health-affecting
        statuses = [s.status for name, s in self.subsystems.items() if name != "logistics"]
        if "critical" in statuses:
            return "critical"
        if "warning" in statuses:
            return "warning"
        if all(s == "unknown" for s in statuses):
            return "unknown"
        return "nominal"

    def to_dict(self):
        return {
            "station_id": self.station_id,
            "display_name": self.display_name,
            "overall_status": self.overall_status,
            "subsystems": {name: s.to_dict() for name, s in self.subsystems.items()},
        }

    def _component(self, subsystem_name: str, field: str) -> Dict[str, Any]:
        label, unit = COMPONENT_META.get(field, (field, ""))
        sub = self.subsystems.get(subsystem_name)
        value = sub.latest.get(field) if sub else None
        return {
            "key": field,
            "subsystem": subsystem_name,
            "label": label,
            "unit": unit,
            "value": value,
            "level": component_level(field, value),
            "numeric": field in NUMERIC_COMPONENT_BOUNDS,
        }

    def rooms_snapshot(self) -> List[Dict[str, Any]]:
        out = []
        for room in ROOM_LAYOUTS.get(self.station_id, []):
            if not room["fields"]:
                out.append({
                    "id": room["id"], "name": room["name"], "icon": room["icon"],
                    "grid": room["grid"], "status": "nominal", "note": room["note"],
                    "components": [],
                })
                continue
            components = [self._component(sub, field) for sub, field in room["fields"]]
            out.append({
                "id": room["id"], "name": room["name"], "icon": room["icon"],
                "grid": room["grid"], "status": worst_level([c["level"] for c in components]),
                "note": room["note"], "components": components,
            })
        return out

    def room(self, room_id: str) -> Optional[Dict[str, Any]]:
        for room in self.rooms_snapshot():
            if room["id"] == room_id:
                return room
        return None

    def room_config(self, room_id: str) -> Optional[Dict[str, Any]]:
        for room in ROOM_LAYOUTS.get(self.station_id, []):
            if room["id"] == room_id:
                return room
        return None

    def critical_component(self) -> Optional[Dict[str, Any]]:
        candidates = []
        for room in self.rooms_snapshot():
            for c in room["components"]:
                if c["level"] in ("critical", "warning"):
                    candidates.append({**c, "room": room["name"], "room_id": room["id"]})
        if not candidates:
            return None
        candidates.sort(key=lambda c: LEVEL_RANK[c["level"]], reverse=True)
        return candidates[0]


class DigitalTwinRegistry:
    def __init__(self):
        self.stations: Dict[str, StationTwin] = {
            "maitri": StationTwin("maitri", "Maitri"),
            "bharati": StationTwin("bharati", "Bharati"),
        }

    def update(self, station_id: str, subsystem: str, payload: Dict[str, Any]):
        if station_id in self.stations:
            self.stations[station_id].update(subsystem, payload)

    def snapshot(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.stations.values()]

    def get(self, station_id: str):
        return self.stations.get(station_id)


registry = DigitalTwinRegistry()
