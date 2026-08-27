from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, JSON
from datetime import datetime
from database import Base


class TelemetryRecord(Base):
    """Raw time-series telemetry, one row per reading per subsystem."""
    __tablename__ = "telemetry"

    id = Column(Integer, primary_key=True, index=True)
    station_id = Column(String, index=True)
    subsystem = Column(String, index=True)  # energy | fuel | environment | infrastructure
    payload = Column(JSON)
    received_at = Column(DateTime, default=datetime.utcnow, index=True)


class Alert(Base):
    """Alerts raised by the twin state engine (threshold breaches, faults)."""
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)
    station_id = Column(String, index=True)
    subsystem = Column(String)
    severity = Column(String)  # info | warning | critical
    message = Column(String)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    resolved_at = Column(DateTime, nullable=True)
