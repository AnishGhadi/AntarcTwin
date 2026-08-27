"""
Forecasting engine for energy, fuel/logistics, and (new) generic
per-component depletion/trend prediction used by the room-detail pages.

Deliberately simple for the MVP timeline: linear regression on a recent
rolling window. This is enough to produce a believable short-horizon
forecast and a resource-depletion estimate, and it trains/predicts in
milliseconds with no external dependencies beyond numpy. Swap in
Prophet/ARIMA/Holt-Winters later if time allows - the public functions
here keep the same shape either way.
"""

from typing import List, Dict, Any, Optional, Tuple
import numpy as np

# Must match simulator.PUBLISH_INTERVAL_SEC - see README for why this is a
# hardcoded coupling rather than something negotiated at runtime (MVP).
PUBLISH_INTERVAL_SEC = 3
READINGS_PER_DAY = 86400.0 / PUBLISH_INTERVAL_SEC


def _linear_forecast(values: List[float], steps_ahead: int) -> List[float]:
    if len(values) < 3:
        # not enough history yet - flat forecast
        last = values[-1] if values else 0
        return [last] * steps_ahead

    x = np.arange(len(values))
    y = np.array(values)
    slope, intercept = np.polyfit(x, y, 1)

    future_x = np.arange(len(values), len(values) + steps_ahead)
    forecast = slope * future_x + intercept
    return [max(0, round(float(v), 2)) for v in forecast]


def forecast_energy_load(history: List[Dict[str, Any]], steps_ahead: int = 12) -> Dict[str, Any]:
    """history: list of energy payloads ordered oldest -> newest."""
    loads = [h.get("load_kw", 0) for h in history]
    diesel = [h.get("diesel_kw", 0) for h in history]

    return {
        "load_kw_forecast": _linear_forecast(loads, steps_ahead),
        "diesel_kw_forecast": _linear_forecast(diesel, steps_ahead),
        "horizon_steps": steps_ahead,
        "basis_points": len(loads),
    }


def forecast_fuel(history: List[Dict[str, Any]], steps_ahead: int = 12) -> Dict[str, Any]:
    """history: list of fuel payloads ordered oldest -> newest."""
    levels = [h.get("fuel_level_liters", 0) for h in history]
    forecast_levels = _linear_forecast(levels, steps_ahead)

    depletion_step = None
    for i, lvl in enumerate(forecast_levels):
        if lvl <= 0:
            depletion_step = i
            break

    return {
        "fuel_level_forecast": forecast_levels,
        "horizon_steps": steps_ahead,
        "basis_points": len(levels),
        "est_depletion_step": depletion_step,
        "resupply_recommended": (
            forecast_levels[-1] < 0.2 * levels[0] if levels else False
        ),
    }


# ---------------------------------------------------------------------------
# Generic component trend/depletion forecasting - powers the room-detail
# "predictive AI" panels (1 week / 1 month / 3 months ahead).
# ---------------------------------------------------------------------------

HORIZON_DAYS = (7, 30, 90)


def forecast_trend(
    values: List[float],
    floor: Optional[float] = None,
    ceiling: Optional[float] = None,
    horizon_days: Tuple[int, ...] = HORIZON_DAYS,
    graph_points: int = 14,
) -> Optional[Dict[str, Any]]:
    """Fit a straight-line trend to a recent reading window and project it
    forward in real time (days), not just "next N readings".

    `floor`/`ceiling` describe the physical bounds of the metric (e.g. 0..100
    for a percentage, 0..None for a liters level, None..None for a
    temperature that can go arbitrarily negative). The floor doubles as the
    "depleted" point used for the ETA estimate.

    Returns None if there's no data at all yet.
    """
    values = [v for v in values if v is not None]
    if not values:
        return None

    n = len(values)
    current = float(values[-1])

    if n < 3:
        slope_per_day = 0.0
    else:
        x = np.arange(n)
        y = np.array(values, dtype=float)
        slope, _intercept = np.polyfit(x, y, 1)
        slope_per_day = float(slope) * READINGS_PER_DAY

    def clamp(v: float) -> float:
        if floor is not None:
            v = max(floor, v)
        if ceiling is not None:
            v = min(ceiling, v)
        return round(v, 2)

    max_day = max(horizon_days)
    step = max_day / (graph_points - 1) if graph_points > 1 else max_day
    graph = [
        {"day": round(i * step, 1), "value": clamp(current + slope_per_day * (i * step))}
        for i in range(graph_points)
    ]

    horizons = {f"{d}d": clamp(current + slope_per_day * d) for d in horizon_days}

    depletion_eta_days = None
    if floor is not None and slope_per_day < -1e-9 and current > floor:
        depletion_eta_days = round((current - floor) / (-slope_per_day), 1)

    if abs(slope_per_day) < 1e-6:
        narrative = "Holding steady at current levels."
    elif slope_per_day < 0:
        narrative = f"Declining ~{abs(round(slope_per_day, 2))}/day"
        narrative += (
            f" — projected to hit its floor in {depletion_eta_days} days."
            if depletion_eta_days is not None else "."
        )
    else:
        narrative = f"Rising ~{round(slope_per_day, 2)}/day."

    return {
        "current": round(current, 2),
        "slope_per_day": round(slope_per_day, 4),
        "graph": graph,
        "horizons": horizons,
        "depletion_eta_days": depletion_eta_days,
        "narrative": narrative,
        "basis_points": n,
    }


def estimate_days_to_threshold(
    values: List[float], threshold: float, from_above: bool = True
) -> Optional[float]:
    """Days until a trending value crosses `threshold` (e.g. water reserve
    hitting the 15% critical line). Used for the homepage resupply countdown.
    Returns None if the value isn't trending toward the threshold.
    """
    values = [v for v in values if v is not None]
    if len(values) < 3:
        return None
    x = np.arange(len(values))
    y = np.array(values, dtype=float)
    slope, _intercept = np.polyfit(x, y, 1)
    slope_per_day = float(slope) * READINGS_PER_DAY
    current = float(y[-1])

    if from_above:
        if slope_per_day >= -1e-9 or current <= threshold:
            return None
        return round((current - threshold) / (-slope_per_day), 1)
    else:
        if slope_per_day <= 1e-9 or current >= threshold:
            return None
        return round((threshold - current) / slope_per_day, 1)
