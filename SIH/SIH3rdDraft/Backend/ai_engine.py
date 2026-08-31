"""
Forecasting engine for energy, fuel/logistics, and generic per-component
depletion/trend prediction used by the room-detail pages.

v3 fix: in v2, each MQTT reading represented a few seconds of real time, so
turning a per-reading slope into a per-day rate required multiplying by
~28,800 (readings/day) - and multiplying a noisy, near-flat regression
slope by 28,800 and then by up to 90 more (for the 90-day horizon) is
exactly how a harmless bit of sensor noise turned into "-10,000C in 90
days". v3's simulator now emits exactly one reading per simulated day, so
a regression slope computed over the reading history *is already* a
per-day rate - no multiplier, no amplification of noise. Combined with the
floor/ceiling clamps from twin_state.NUMERIC_COMPONENT_BOUNDS, forecasts
now stay in the range of what's physically possible.

v3 also stops handing back a perfectly straight forecast line: the
projection curve is built from the trend *plus* the actual day-to-day
noise pattern observed in that component's own recent history, so it
visually varies the way a real sensor would, while the 7d/30d/90d point
predictions stay on the smooth trend (the same one driving the curve).
"""

from typing import List, Dict, Any, Optional, Tuple
import numpy as np


def _linear_forecast(values: List[float], steps_ahead: int) -> List[float]:
    if len(values) < 3:
        last = values[-1] if values else 0
        return [last] * steps_ahead

    x = np.arange(len(values))
    y = np.array(values)
    slope, intercept = np.polyfit(x, y, 1)

    future_x = np.arange(len(values), len(values) + steps_ahead)
    forecast = slope * future_x + intercept
    return [max(0, round(float(v), 2)) for v in forecast]


def forecast_energy_load(history: List[Dict[str, Any]], steps_ahead: int = 12) -> Dict[str, Any]:
    loads = [h.get("load_kw", 0) for h in history]
    diesel = [h.get("diesel_kw", 0) for h in history]
    return {
        "load_kw_forecast": _linear_forecast(loads, steps_ahead),
        "diesel_kw_forecast": _linear_forecast(diesel, steps_ahead),
        "horizon_steps": steps_ahead,
        "basis_points": len(loads),
    }


def forecast_fuel(history: List[Dict[str, Any]], steps_ahead: int = 12) -> Dict[str, Any]:
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
# Generic component trend/depletion forecasting - one reading == one day,
# so every "per day" quantity below is a direct read of the regression,
# no unit conversion required.
# ---------------------------------------------------------------------------

HORIZON_DAYS = (7, 30, 90)


def forecast_trend(
    values: List[float],
    floor: Optional[float] = None,
    ceiling: Optional[float] = None,
    horizon_days: Tuple[int, ...] = HORIZON_DAYS,
    graph_points: int = 31,
) -> Optional[Dict[str, Any]]:
    """Fit a straight-line trend across recent daily readings and project it
    forward. The point predictions (`horizons`) ride the smooth trend line;
    the visual `graph` rides the same trend but with the component's own
    recent day-to-day variability layered back on, so it looks like a real
    forecast instead of a ruler-straight line.

    `floor`/`ceiling` are physical bounds (see twin_state.NUMERIC_COMPONENT_BOUNDS)
    used to clamp both the trend and the noisy curve. Returns None if there's
    no data yet.
    """
    values = [v for v in values if v is not None]
    if not values:
        return None

    n = len(values)
    current = float(values[-1])

    if n < 3:
        slope_per_day = 0.0
        residuals = [0.0]
    else:
        x = np.arange(n)
        y = np.array(values, dtype=float)
        slope, intercept = np.polyfit(x, y, 1)
        slope_per_day = float(slope)
        fitted = slope * x + intercept
        residuals = (y - fitted).tolist()
        # guard against a degenerate all-zero residual set (perfectly flat data)
        if all(abs(r) < 1e-9 for r in residuals):
            residuals = [0.0]

    def clamp(v: float) -> float:
        if floor is not None:
            v = max(floor, v)
        if ceiling is not None:
            v = min(ceiling, v)
        return round(v, 2)

    def trend_at(day: float) -> float:
        return current + slope_per_day * day

    max_day = max(horizon_days)
    step = max_day / (graph_points - 1) if graph_points > 1 else max_day
    graph = []
    for i in range(graph_points):
        day = round(i * step, 1)
        # tile the component's own recent daily variability forward so the
        # curve wiggles the way this exact sensor actually behaves, rather
        # than drawing a synthetic straight line
        noise = residuals[i % len(residuals)]
        graph.append({"day": day, "value": clamp(trend_at(day) + noise)})

    horizons = {f"{d}d": clamp(trend_at(d)) for d in horizon_days}

    depletion_eta_days = None
    if floor is not None and slope_per_day < -1e-9 and current > floor:
        depletion_eta_days = round((current - floor) / (-slope_per_day), 1)

    if abs(slope_per_day) < 1e-4:
        narrative = "Holding steady day over day."
    elif slope_per_day < 0:
        narrative = f"Trending down ~{abs(round(slope_per_day, 3))}/day"
        narrative += (
            f" — projected to hit its floor in about {depletion_eta_days} days."
            if depletion_eta_days is not None else "."
        )
    else:
        narrative = f"Trending up ~{round(slope_per_day, 3)}/day."

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
    """Days until a trending value crosses `threshold` at its current daily
    rate of change. Used for the "would run out before the ship arrives"
    check on the overview page.
    """
    values = [v for v in values if v is not None]
    if len(values) < 3:
        return None
    x = np.arange(len(values))
    y = np.array(values, dtype=float)
    slope, _intercept = np.polyfit(x, y, 1)
    slope_per_day = float(slope)
    current = float(y[-1])

    if from_above:
        if slope_per_day >= -1e-9 or current <= threshold:
            return None
        return round((current - threshold) / (-slope_per_day), 1)
    else:
        if slope_per_day <= 1e-9 or current >= threshold:
            return None
        return round((threshold - current) / slope_per_day, 1)
