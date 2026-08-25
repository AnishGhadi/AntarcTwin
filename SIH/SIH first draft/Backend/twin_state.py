"""
The digital twin's live state model.

This is what makes the system a "twin" rather than a telemetry dashboard:
each station is represented as a structured object with subsystem states,
health, and derived status - not just a stream of raw JSON.
"""

from datetime import datetime, timezone
from typing import Dict, Any, List

THRESHOLDS = {
    "fuel_pct_low": 20,
    "fuel_pct_critical": 10,
    "structural_health_low": 65,
    "temperature_extreme_low": -40,
    "wind_speed_high": 90,
}


class SubsystemState:
    def __init__(self, name: str):
        self.name = name
        self.latest: Dict[str, Any] = {}
        self.status: str = "unknown"  # nominal | warning | critical | offline
        self.last_updated: datetime | None = None

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
            if payload.get("storm_warning") or payload.get("wind_speed_kmh", 0) > THRESHOLDS["wind_speed_high"]:
                return "warning"
            if payload.get("temperature_c", 0) < THRESHOLDS["temperature_extreme_low"]:
                return "warning"
            return "nominal"
        if self.name == "infrastructure":
            if payload.get("structural_health_index", 100) < THRESHOLDS["structural_health_low"]:
                return "critical"
            if payload.get("comms_link_status") != "online":
                return "warning"
            return "nominal"
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
        }

    def update(self, subsystem: str, payload: Dict[str, Any]):
        if subsystem in self.subsystems:
            self.subsystems[subsystem].update(payload)

    @property
    def overall_status(self) -> str:
        statuses = [s.status for s in self.subsystems.values()]
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


class DigitalTwinRegistry:
    """Holds the live twin state for all stations. Single source of truth in-process."""

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


# module-level singleton used by the FastAPI app
registry = DigitalTwinRegistry()
