"""
The digital twin's live state model.

v3 changes: added a 5th subsystem, "logistics" (the supply ship tracker),
and tightened every numeric component's physical bounds so that even if a
forecast's trend line is noisy, it can never be clamped-extrapolated into
something absurd (this was the root cause of the "-10000C" style forecasts
in v2 - see ai_engine.py for the actual math fix).
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
}

LEVEL_RANK = {"unknown": 0, "nominal": 1, "warning": 2, "critical": 3}

# Fields that are plottable/predictable numeric series, with (floor, ceiling)
# physical bounds - used both to clamp the raw simulator (defense in depth)
# and to clamp AI forecasts so a noisy trend can never extrapolate past
# what's physically possible for that field.
NUMERIC_COMPONENT_BOUNDS = {
    "temperature_c": (-55, 8),
    "wind_speed_kmh": (0, 130),
    "humidity_pct": (0, 100),
    "snow_depth_cm": (0, 300),
    "visibility_km": (0, 30),
    "fuel_level_liters": (0, None),
    "fuel_pct": (0, 100),
    "est_days_remaining": (0, 2000),
    "diesel_kw": (0, 150),
    "solar_kw": (0, 60),
    "wind_kw": (0, 60),
    "load_kw": (0, 150),
    "grid_frequency_hz": (0, 55),
    "structural_health_index": (0, 100),
    "water_reserve_pct": (0, 100),
}

# Human labels + units for every field we might display, keyed by field name.
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
    if key == "est_days_remaining":
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
    return "nominal"


def worst_level(levels: List[str]) -> str:
    real = [l for l in levels if l != "unknown"]
    if not real:
        return "unknown"
    return max(real, key=lambda l: LEVEL_RANK[l])


# ---------------------------------------------------------------------------
# Room layout (still config-driven; grid row/col retained for API stability
# but the frontend now renders these as a positioned site map, see the
# SITE_LAYOUT constant in frontend/index.html)
# ---------------------------------------------------------------------------

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


ROOM_LAYOUTS = {
    "maitri": [
        _room("environment_zone", "Environment Zone", "🌡️", 1, 1, 2,
              ["environment"], [("environment", k) for k in
              ("temperature_c", "wind_speed_kmh", "humidity_pct", "snow_depth_cm",
               "visibility_km", "storm_warning")]),
        _room("auxiliary_facilities", "Summer Camp & Container Modules", "🏕️", 2, 1, 2,
              [], [], note="Personnel shelter & research/storage containers — no live sensors."),
        _room("main_building", "Maitri Main Building", "🏠", 3, 1, 2,
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
        _room("water_pump_house", "Lake Water Pump House", "💧", 5, 2, 1,
              ["infrastructure"], [("infrastructure", "water_reserve_pct")]),
    ],
    "bharati": [
        _room("environment_zone", "Weather / Environment", "🌡️", 1, 1, 2,
              ["environment"], [("environment", k) for k in
              ("temperature_c", "wind_speed_kmh", "humidity_pct", "snow_depth_cm",
               "visibility_km", "storm_warning")]),
        _room("auxiliary_facilities", "Container Modules & Summer Camp", "🏕️", 2, 1, 2,
              [], [], note="Research/storage containers & emergency shelter — no live sensors."),
        _room("main_building", "Bharati Main Building", "🏠", 3, 1, 2,
              ["infrastructure", "energy"], [
                  ("infrastructure", "hvac_status"),
                  ("infrastructure", "structural_health_index"),
                  ("infrastructure", "water_reserve_pct"),
                  ("energy", "load_kw"),
              ]),
        _room("power_energy", "Power / Generator", "⚡", 4, 1, 1,
              ["energy"], [("energy", k) for k in
              ("generator_status", "diesel_kw", "solar_kw", "wind_kw", "grid_frequency_hz")]),
        _room("communication", "Communication", "📡", 4, 2, 1,
              ["infrastructure"], [("infrastructure", "comms_link_status")]),
        _room("fuel_system", "Fuel Farm & Station", "🛢️", 5, 1, 1,
              ["fuel"], [("fuel", k) for k in
              ("fuel_level_liters", "fuel_pct", "est_days_remaining", "leak_detected", "resupply_pending")]),
        _room("water_pump_house", "Sea Water Pump House", "💧", 5, 2, 1,
              ["infrastructure"], [("infrastructure", "water_reserve_pct")]),
    ],
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
            if payload.get("generator_status") == "fault":
                return "critical"
            return "nominal"
        if self.name == "fuel":
            pct = payload.get("fuel_pct", 100)
            if payload.get("leak_detected"):
                return "critical"
            if pct < THRESHOLDS["fuel_pct_critical"]:
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
        }

    def update(self, subsystem: str, payload: Dict[str, Any]):
        if subsystem in self.subsystems:
            self.subsystems[subsystem].update(payload)

    @property
    def overall_status(self) -> str:
        # logistics delays are informational, not station-health-affecting
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
        rooms = ROOM_LAYOUTS.get(self.station_id, [])
        out = []
        for room in rooms:
            if not room["fields"]:
                out.append({
                    "id": room["id"], "name": room["name"], "icon": room["icon"],
                    "grid": room["grid"], "status": "nominal", "note": room["note"],
                    "components": [],
                })
                continue
            components = [self._component(sub, field) for sub, field in room["fields"]]
            status = worst_level([c["level"] for c in components])
            out.append({
                "id": room["id"], "name": room["name"], "icon": room["icon"],
                "grid": room["grid"], "status": status, "note": room["note"],
                "components": components,
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
