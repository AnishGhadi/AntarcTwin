"""
Hybrid predictive engine (v5) - replaces the single linear-regression model
with two purpose-built models, chosen per field (see twin_state.forecast_model_for):

1. KALMAN (damped local-linear-trend state-space filter) for continuous
   physical measurements: weather, power output, structural health, cold-room
   conditions, etc. The true value and its rate of change are hidden states;
   every reading corrects them. Noise parameters are estimated from each
   signal's own history so the same code works for a temperature in degrees
   and a load in kW. Trend is *damped* so it can't extrapolate forever, the
   forecast carries a growing uncertainty band (95% interval), and the
   filter's innovations drive an anomaly check.

2. RESOURCE-BALANCE (conservation) model for depletable stocks: fuel, water,
   food, spares, warehouse fill... These obey physics a regression ignores -
   between resupply events a stock can only fall. The model estimates daily
   *consumption* from day-over-day drops (resupply jumps excluded, recent
   days weighted more), subtracts it day by day (so the projection can never
   rise on its own), and - when the supply ship's ETA is known - applies the
   resupply jump on the arrival day. Depletion ETAs and "will it run out
   before the ship arrives?" fall out directly.

Both return the same output shape the frontend already consumes (current,
graph, horizons, ...), plus uncertainty, anomaly flag, model name, and a
plain-language multi-paragraph `analysis` for the metric detail page.

One reading == one simulated day, so every "per day" quantity is direct.
"""

from typing import List, Dict, Any, Optional, Tuple
import numpy as np

HORIZON_DAYS = (7, 30, 90)
PHI = 0.96          # trend damping factor for the Kalman model
Z95 = 1.96          # 95% interval multiplier
ANOMALY_Z = 3.0     # |innovation z-score| above this flags an anomaly
CONSUMPTION_WINDOW = 60   # days of history used to estimate consumption
CONSUMPTION_HALF_LIFE = 10.0   # recency weighting half-life, in days


# ---------------------------------------------------------------------------
# Kalman filter (damped local linear trend)
# ---------------------------------------------------------------------------

def _kalman_fit(values: List[float]) -> Dict[str, Any]:
    y = np.asarray(values, dtype=float)
    n = len(y)
    var_total = float(np.var(y)) if n > 1 else 0.0
    diffs = np.diff(y)
    var_diff = float(np.var(diffs)) if len(diffs) > 1 else var_total

    # measurement noise from first differences (random-walk-plus-noise
    # heuristic), floored relative to the signal so it can't collapse to 0
    R = max(var_diff / 2.0, 1e-6 * (1.0 + var_total))
    Q = np.array([[0.05 * R, 0.0], [0.0, 0.005 * R]])
    F = np.array([[1.0, PHI], [0.0, PHI]])

    x = np.array([y[0], 0.0])
    P = np.array([[R, 0.0], [0.0, max(var_diff, R)]])
    filtered, innov, svar = [y[0]], [], []
    for t in range(1, n):
        x = F @ x
        P = F @ P @ F.T + Q
        S = P[0, 0] + R
        K = P[:, 0] / S
        r = y[t] - x[0]
        x = x + K * r
        P = P - np.outer(K, P[0, :])
        filtered.append(x[0])
        innov.append(r)
        svar.append(S)

    return {"x": x, "P": P, "R": R, "Q": Q, "F": F, "y": y,
            "filtered": np.asarray(filtered), "innov": np.asarray(innov), "svar": np.asarray(svar)}


def _kalman_path(fit: Dict[str, Any], steps: int) -> Tuple[List[float], List[float]]:
    x, P, F, Q, R = fit["x"].copy(), fit["P"].copy(), fit["F"], fit["Q"], fit["R"]
    levels, sds = [], []
    for _ in range(steps):
        x = F @ x
        P = F @ P @ F.T + Q
        levels.append(float(x[0]))
        sds.append(float(np.sqrt(max(P[0, 0] + R, 0.0))))
    return levels, sds


def _clamp_factory(floor: Optional[float], ceiling: Optional[float]):
    def clamp(v: float) -> float:
        if floor is not None:
            v = max(floor, v)
        if ceiling is not None:
            v = min(ceiling, v)
        return round(float(v), 2)
    return clamp


def _graph_days(max_day: int, graph_points: int) -> List[float]:
    step = max_day / (graph_points - 1) if graph_points > 1 else max_day
    return [round(i * step, 1) for i in range(graph_points)]


def _fmt(v: float, unit: str = "") -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{s}{unit}" if unit in ("%", "°C") else (f"{s} {unit}" if unit else s)


def _flat_result(current: float, model_used: str, label: str, unit: str, n: int, clamp) -> Dict[str, Any]:
    days = _graph_days(max(HORIZON_DAYS), 31)
    msg = "Not enough history yet to fit the model — showing a flat projection until a few more days of readings arrive."
    return {
        "current": round(current, 2), "slope_per_day": 0.0,
        "graph": [{"day": d, "value": clamp(current)} for d in days],
        "horizons": {f"{d}d": clamp(current) for d in HORIZON_DAYS},
        "uncertainty": {f"{d}d": 0.0 for d in HORIZON_DAYS},
        "depletion_eta_days": None, "days_to_critical": None, "daily_rate": None, "resupply_day": None,
        "anomaly_detected": False, "anomaly_z": 0.0,
        "model_used": model_used, "narrative": msg, "analysis": msg, "basis_points": n,
    }


def _forecast_kalman(values, floor, ceiling, label, unit, graph_points) -> Dict[str, Any]:
    clamp = _clamp_factory(floor, ceiling)
    n = len(values)
    current = float(values[-1])
    model_name = "Damped Kalman Filter (local linear trend)"
    if n < 5:
        return _flat_result(current, model_name, label, unit, n, clamp)

    fit = _kalman_fit(values)
    max_day = max(HORIZON_DAYS)
    levels, sds = _kalman_path(fit, max_day)      # index 0 == day 1
    trend = float(fit["x"][1])
    filt_level = float(fit["x"][0])

    horizons = {f"{d}d": clamp(levels[d - 1]) for d in HORIZON_DAYS}
    # a mean-reverting signal can never be more uncertain than its own
    # historical spread, so cap the accumulated state uncertainty there
    spread_cap = Z95 * float(np.std(fit["y"])) if n > 1 else float("inf")
    uncertainty = {f"{d}d": round(min(Z95 * sds[d - 1], spread_cap), 2) for d in HORIZON_DAYS}

    residuals = (fit["y"] - fit["filtered"]).tolist() or [0.0]
    graph = []
    for i, day in enumerate(_graph_days(max_day, graph_points)):
        base = filt_level if day == 0 else levels[int(round(day)) - 1]
        graph.append({"day": day, "value": clamp(base + residuals[i % len(residuals)])})

    # anomaly check on the latest innovations
    z_recent = (fit["innov"][-3:] / np.sqrt(fit["svar"][-3:])) if len(fit["innov"]) else np.array([0.0])
    z_last = float(z_recent[-1])
    anomaly = bool(np.max(np.abs(z_recent)) > ANOMALY_Z)

    # narrative
    if abs(trend) < 1e-3 * max(1.0, abs(filt_level)):
        trend_txt = "holding steady"
    else:
        trend_txt = f"{'rising' if trend > 0 else 'falling'} ~{abs(trend):.3f}{(' ' + unit) if unit and unit != '%' else unit}/day"
    narrative = (f"Kalman-filtered level {_fmt(filt_level, unit)}, {trend_txt}."
                 + (" ⚠ An unusual reading was detected in the last 3 days." if anomaly else " No anomalies in recent readings."))

    seasonal_note = (" This model does not represent seasonal cycles explicitly, so long-horizon values for strongly "
                     "seasonal quantities (like outdoor temperature) are best read as trend-continuation, which is "
                     "why their intervals are deliberately wide at 90 days.")
    p1 = (f"{label} is forecast with a damped Kalman filter — a state-space model that treats the true value and its "
          f"rate of change as hidden states and corrects both with every new reading. Its noise settings are "
          f"estimated from this signal's own {n}-day history rather than hand-tuned (measurement noise σ ≈ "
          f"{_fmt(float(np.sqrt(fit['R'])), unit)}). The filtered estimate is {_fmt(filt_level, unit)} against a raw "
          f"last reading of {_fmt(current, unit)}; the gap is the sensor jitter the filter is removing.")
    p2 = (f"The filter currently sees the metric {trend_txt} (the trend is damped, so it cannot be extrapolated "
          f"forever). Projected values with 95% intervals: 1 week {_fmt(horizons['7d'], unit)} ± "
          f"{_fmt(uncertainty['7d'], unit)}, 1 month {_fmt(horizons['30d'], unit)} ± {_fmt(uncertainty['30d'], unit)}, "
          f"3 months {_fmt(horizons['90d'], unit)} ± {_fmt(uncertainty['90d'], unit)}. The band widens with horizon "
          f"because state uncertainty accumulates for every extra day the model looks ahead.")
    p3 = ((f"Anomaly check: the newest innovation is {abs(z_last):.1f}σ from what the model expected"
           + (" — beyond the {:.0f}σ alarm line, so this reading is flagged for attention before it is trusted as a new trend."
              .format(ANOMALY_Z) if anomaly else ", well inside normal variation, so no anomaly is flagged."))
          + seasonal_note)

    return {
        "current": round(current, 2), "slope_per_day": round(trend, 4), "graph": graph,
        "horizons": horizons, "uncertainty": uncertainty,
        "depletion_eta_days": None, "days_to_critical": None, "daily_rate": None, "resupply_day": None,
        "anomaly_detected": anomaly, "anomaly_z": round(z_last, 2),
        "model_used": model_name, "narrative": narrative,
        "analysis": "\n\n".join([p1, p2, p3]), "basis_points": n,
    }


# ---------------------------------------------------------------------------
# Resource-balance (conservation) model
# ---------------------------------------------------------------------------

def _consumption_stats(values: List[float], floor: float, ceiling: Optional[float]) -> Dict[str, Any]:
    y = np.asarray(values, dtype=float)
    span = (ceiling - floor) if ceiling is not None else max(float(np.max(y)), 1.0)
    jump = 0.05 * span
    deltas = y[:-1] - y[1:]                    # positive => stock fell (consumption)
    resupplied = deltas < -jump                # big rise => resupply event, excluded
    cons = np.where(resupplied, np.nan, np.maximum(deltas, 0.0))
    window = cons[-CONSUMPTION_WINDOW:]
    valid = window[~np.isnan(window)]
    if len(valid) < 3:
        return {"rate": 0.0, "sigma": 0.0, "samples": valid, "n_used": len(valid), "span": span,
                "recent": 0.0, "long": 0.0, "resupply_events": int(resupplied.sum())}
    ages = np.arange(len(valid))[::-1]
    w = 0.5 ** (ages / CONSUMPTION_HALF_LIFE)
    rate = float(np.sum(w * valid) / np.sum(w))
    sigma = float(np.sqrt(np.sum(w * (valid - rate) ** 2) / np.sum(w)))
    return {"rate": rate, "sigma": sigma, "samples": valid, "n_used": len(valid), "span": span,
            "recent": float(np.mean(valid[-7:])), "long": float(np.mean(valid)),
            "resupply_events": int(resupplied.sum())}


def _resource_path(current: float, daily: np.ndarray, floor: float, days: int,
                   resupply: Optional[Dict[str, Any]]) -> List[float]:
    """Day-by-day projection; `daily` is the consumption applied each day
    (index 0 = day 1). Never rises except on the scheduled resupply day."""
    R = resupply["days"] if resupply and resupply.get("days") and resupply["days"] > 0 else None
    v, path = current, [current]
    for d in range(1, days + 1):
        v = max(floor, v - float(daily[(d - 1) % len(daily)]))
        if R is not None and d == R:
            v = max(v, float(resupply["target"]))
        path.append(v)
    return path


def _forecast_resource(values, floor, ceiling, label, unit, graph_points,
                       resupply, critical_level) -> Dict[str, Any]:
    fl = 0.0 if floor is None else floor
    clamp = _clamp_factory(fl, ceiling)
    n = len(values)
    current = float(values[-1])
    model_name = "Resource-Balance (conservation) Model"
    if n < 5:
        return _flat_result(current, model_name, label, unit, n, clamp)

    st = _consumption_stats(values, fl, ceiling)
    rate, sigma = st["rate"], st["sigma"]
    max_day = max(HORIZON_DAYS)
    R = resupply["days"] if resupply and resupply.get("days") and resupply["days"] > 0 else None

    smooth = _resource_path(current, np.array([rate]), fl, max_day, resupply)
    horizons = {f"{d}d": clamp(smooth[d]) for d in HORIZON_DAYS}
    span = st["span"]
    uncertainty = {}
    for d in HORIZON_DAYS:
        if R is not None and d >= R:
            u = Z95 * sigma * np.sqrt(max(d - R, 0)) + 0.02 * span
        else:
            u = Z95 * sigma * np.sqrt(d)
        uncertainty[f"{d}d"] = round(float(u), 2)

    # chart curve: same conservation path, but using the metric's own real
    # day-to-day consumption pattern (scaled to the estimated rate) so it
    # steps irregularly like reality - still strictly non-increasing
    samples = st["samples"]
    if rate > 1e-12 and len(samples) and float(np.mean(samples)) > 1e-12:
        daily = samples * (rate / float(np.mean(samples)))
    else:
        daily = np.array([rate])
    noisy = _resource_path(current, daily, fl, max_day, resupply)
    graph = [{"day": d, "value": clamp(noisy[int(round(d))])} for d in _graph_days(max_day, graph_points)]

    depletion = round((current - fl) / rate, 1) if rate > 1e-12 and current > fl else None
    crit_days = None
    if critical_level is not None and rate > 1e-12:
        crit_days = 0.0 if current <= critical_level else round((current - critical_level) / rate, 1)

    accelerating = st["long"] > 1e-12 and st["recent"] > 1.5 * st["long"] and st["recent"] > 0
    accel_pct = round(100 * (st["recent"] / st["long"] - 1)) if st["long"] > 1e-12 else 0
    unit_rate = f"{unit}/day" if unit else "/day"

    # narrative
    if rate <= 1e-12:
        narrative = f"No measurable consumption recently — holding at {_fmt(current, unit)}."
    else:
        narrative = f"Consuming ~{rate:.3g} {unit_rate}"
        if crit_days is not None and R is not None:
            narrative += (f". Reaches the critical level in ~{crit_days:g} days — BEFORE the scheduled resupply in {R} days."
                          if crit_days < R else
                          f". The scheduled resupply in {R} days arrives before it reaches the critical level (~{crit_days:g} days).")
        elif crit_days is not None:
            narrative += f". Projected to reach the critical level in ~{crit_days:g} days."
        if accelerating:
            narrative += f" ⚠ Consumption is running {accel_pct}% above its longer-term average."

    # analysis paragraphs
    resup_txt = ""
    if rate > 1e-12 and R is not None and crit_days is not None:
        resup_txt = (f"A resupply is scheduled in {R} days; at the current pace the stock would hit its critical level "
                     f"in ≈{crit_days:g} days, so it {'would run critical BEFORE the ship arrives' if crit_days < R else 'is expected to last until the ship arrives'}. ")
    elif R is not None:
        resup_txt = f"A resupply is scheduled in {R} days. "
    p1 = (f"{label} is forecast with a resource-balance (conservation) model instead of a regression line. A stock like "
          f"this can only fall between resupply events, so the model estimates daily consumption from the day-over-day "
          f"drops across the last {st['n_used']} usable days (resupply jumps are excluded, and recent days weigh more, "
          f"with a {CONSUMPTION_HALF_LIFE:g}-day half-life) and subtracts it from the current level every day. The "
          f"projection therefore can never rise on its own — it only steps up when a resupply is actually scheduled.")
    if rate > 1e-12:
        p2 = (f"Estimated consumption is {rate:.3g} {unit_rate} (day-to-day σ ≈ {sigma:.3g}). From {_fmt(current, unit)} now, "
              f"that puts the stock at {_fmt(horizons['7d'], unit)} ± {_fmt(uncertainty['7d'], unit)} in one week, "
              f"{_fmt(horizons['30d'], unit)} ± {_fmt(uncertainty['30d'], unit)} in one month and "
              f"{_fmt(horizons['90d'], unit)} ± {_fmt(uncertainty['90d'], unit)} in three months (95% intervals). "
              + (f"Empty is ≈{depletion:g} days away with no resupply. " if depletion is not None else "")
              + resup_txt)
    else:
        p2 = (f"No consumption has been measurable over the last {st['n_used']} usable days, so the model projects the level "
              f"holding at {_fmt(current, unit)}. " + resup_txt)
    if accelerating:
        p3 = (f"Risk note: the last 7 days of consumption average {st['recent']:.3g} {unit_rate}, {accel_pct}% above the "
              f"{st['long']:.3g} {unit_rate} longer-term average. That pattern is what a leak, a fault or an unusual load "
              f"looks like, and because recent days are weighted more heavily the forecast already reflects it. "
              f"Recommend inspecting the associated system before relying on the resupply date.")
    else:
        p3 = (f"Risk note: recent consumption ({st['recent']:.3g} {unit_rate}) is in line with the longer-term average "
              f"({st['long']:.3g} {unit_rate}), so no acceleration is detected. The main uncertainty is day-to-day "
              f"variation in usage, which is what the widening ± band shows; the model does not predict step changes "
              f"such as a new fault or a delayed ship, which is why the ship's live ETA is fed in directly.")

    return {
        "current": round(current, 2), "slope_per_day": round(-rate, 4), "graph": graph,
        "horizons": horizons, "uncertainty": uncertainty,
        "depletion_eta_days": depletion, "days_to_critical": crit_days,
        "daily_rate": round(rate, 4), "resupply_day": R,
        "anomaly_detected": bool(accelerating), "anomaly_z": 0.0,
        "model_used": model_name, "narrative": narrative,
        "analysis": "\n\n".join([p1, p2, p3]), "basis_points": n,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def forecast_component(
    values: List[float], model: str, floor: Optional[float] = None, ceiling: Optional[float] = None,
    label: str = "Metric", unit: str = "", resupply: Optional[Dict[str, Any]] = None,
    critical_level: Optional[float] = None, graph_points: int = 31,
) -> Optional[Dict[str, Any]]:
    """Dispatch to the right model. `model` is "kalman" or "resource".
    `resupply` = {"days": int, "target": float} (resource model only).
    Returns None if there is no data at all yet."""
    values = [float(v) for v in values if v is not None]
    if not values:
        return None
    if model == "resource":
        return _forecast_resource(values, floor, ceiling, label, unit, graph_points, resupply, critical_level)
    return _forecast_kalman(values, floor, ceiling, label, unit, graph_points)


def estimate_days_to_threshold(values: List[float], threshold: float, from_above: bool = True) -> Optional[float]:
    """Days until a depleting stock reaches `threshold`, from the resource
    model's consumption-rate estimate. Used for the overview page's
    "will it run low before the ship arrives?" check."""
    values = [float(v) for v in values if v is not None]
    if len(values) < 5 or not from_above:
        return None
    st = _consumption_stats(values, 0.0, 100.0)
    current = values[-1]
    if st["rate"] <= 1e-12 or current <= threshold:
        return None
    return round((current - threshold) / st["rate"], 1)


# ---------------------------------------------------------------------------
# Legacy step-ahead endpoints (unused by the UI) - now on the new models too
# ---------------------------------------------------------------------------

def forecast_energy_load(history: List[Dict[str, Any]], steps_ahead: int = 12) -> Dict[str, Any]:
    def path(vals):
        vals = [float(v) for v in vals if v is not None]
        if len(vals) < 5:
            return [round(vals[-1], 2) if vals else 0.0] * steps_ahead
        levels, _ = _kalman_path(_kalman_fit(vals), steps_ahead)
        return [max(0.0, round(v, 2)) for v in levels]
    loads = [h.get("load_kw", 0) for h in history]
    diesel = [h.get("diesel_kw", 0) for h in history]
    return {"load_kw_forecast": path(loads), "diesel_kw_forecast": path(diesel),
            "horizon_steps": steps_ahead, "basis_points": len(loads), "model": "kalman"}


def forecast_fuel(history: List[Dict[str, Any]], steps_ahead: int = 12) -> Dict[str, Any]:
    levels = [float(h.get("fuel_level_liters", 0)) for h in history]
    if len(levels) < 5:
        fc = [round(levels[-1], 2) if levels else 0.0] * steps_ahead
        rate = 0.0
    else:
        st = _consumption_stats(levels, 0.0, None)
        rate = st["rate"]
        fc = [round(v, 2) for v in _resource_path(levels[-1], np.array([rate]), 0.0, steps_ahead, None)[1:]]
    depletion_step = next((i for i, v in enumerate(fc) if v <= 0), None)
    return {"fuel_level_forecast": fc, "horizon_steps": steps_ahead, "basis_points": len(levels),
            "est_depletion_step": depletion_step, "daily_consumption": round(rate, 2),
            "resupply_recommended": bool(levels and fc and fc[-1] < 0.2 * max(levels)),
            "model": "resource-balance"}
