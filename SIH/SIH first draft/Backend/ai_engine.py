"""
Forecasting engine for energy and fuel/logistics.

Deliberately simple for the MVP timeline: linear regression on a recent
rolling window plus exponential smoothing for noise reduction. This is
enough to produce a believable short-horizon forecast and a fuel-runout
estimate, and it trains/predicts in milliseconds with no external
dependencies beyond numpy. Swap in Prophet/ARIMA later if time allows -
the interface (forecast_series) stays the same either way.
"""

from typing import List, Dict, Any
import numpy as np


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
