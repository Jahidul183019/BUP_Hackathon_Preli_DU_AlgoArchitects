"""Joint 24-hour LP scheduling, isolated from the HTTP API and LLM."""

import math

import numpy as np
from scipy.optimize import linprog

from .final_validator import final_validator
from .schedule_inputs import validate_inputs


class OptimizationError(RuntimeError):
    """No verified optimal schedule could be produced."""


class InfeasibleScheduleError(OptimizationError):
    """The supplied energy scenario and directives have no feasible schedule."""


def optimize_schedule(hours: list[dict], battery: dict, directives: list[dict]) -> dict:
    """Minimize grid cost with all 24 hours coupled in one linear program.

    Variable blocks: grid[0:24], solar[24:48], signed delta[48:72],
    end-of-hour energy[72:96]. Positive delta charges; negative discharges.
    No efficiency/loss or export terms are added to the challenge rules.
    """
    hours, battery, directives = validate_inputs(hours, battery, directives)
    initial = battery["initial_energy_kwh"]
    capacity = battery["capacity_kwh"]
    solar_cap = [row["solar_kwh"] for row in hours]
    reserve = [battery["minimum_energy_kwh"]] * 24
    grid_cap = [None] * 24
    lower_delta = [-battery["max_discharge_kwh_per_hour"]] * 24
    upper_delta = [battery["max_charge_kwh_per_hour"]] * 24

    for directive in directives:
        kind = directive["directive_type"]
        if kind == "no_op":
            continue
        adjustment = directive["structured_adjustment"]
        for h in adjustment["hours"]:
            if kind == "solar_reduction":
                # Overlaps are caps on original solar; do not compound factors.
                solar_cap[h] = min(solar_cap[h], hours[h]["solar_kwh"] * adjustment["factor"])
            elif kind == "minimum_battery_reserve":
                reserve[h] = max(reserve[h], adjustment["minimum_energy_kwh"])
            elif kind == "no_charge_window":
                upper_delta[h] = 0
            elif kind == "no_discharge_window":
                lower_delta[h] = 0
            elif kind == "max_grid_window":
                cap = adjustment["max_grid_kwh"]
                grid_cap[h] = cap if grid_cap[h] is None else min(grid_cap[h], cap)

    objective = np.zeros(96)
    objective[:24] = [row["tariff_bdt_per_kwh"] for row in hours]
    equality = np.zeros((49, 96))
    rhs = np.zeros(49)
    for h in range(24):
        # grid + solar - delta = demand
        equality[h, h] = 1
        equality[h, 24 + h] = 1
        equality[h, 48 + h] = -1
        rhs[h] = hours[h]["demand_kwh"]
        # E_after[h] - delta[h] - E_after[h-1] = 0
        equality[24 + h, 72 + h] = 1
        equality[24 + h, 48 + h] = -1
        if h:
            equality[24 + h, 72 + h - 1] = -1
        else:
            rhs[24 + h] = initial
    # Exact model equality, not a penalty or a lower-bound approximation.
    equality[48, 95] = 1
    rhs[48] = initial
    bounds = (
        [(0, cap) for cap in grid_cap]
        + [(0, cap) for cap in solar_cap]
        + list(zip(lower_delta, upper_delta))
        + [(minimum, capacity) for minimum in reserve]
    )
    solved = linprog(
        objective, A_eq=equality, b_eq=rhs, bounds=bounds, method="highs",
        options={"time_limit": 10.0, "primal_feasibility_tolerance": 1e-9,
                 "dual_feasibility_tolerance": 1e-9},
    )
    if solved.status == 2:
        raise InfeasibleScheduleError("Energy scenario and directives are infeasible")
    if not solved.success or solved.x is None or not np.all(np.isfinite(solved.x)):
        raise OptimizationError(f"LP did not produce an optimal finite schedule (status {solved.status})")

    def clean_nonnegative(value):
        value = float(value)
        # Only discard negative solver noise; retain positive fractional energy.
        if -1e-8 <= value < 0:
            return 0.0
        return value + 0.0  # Normalize IEEE -0.0 to 0.0

    hourly_plan = []
    for h in range(24):
        delta = float(solved.x[48 + h])
        action = "charge" if delta > 0 else "discharge" if delta < 0 else "idle"
        hourly_plan.append({
            "hour": h,
            "grid_kwh": clean_nonnegative(solved.x[h]),
            "solar_used_kwh": clean_nonnegative(solved.x[24 + h]),
            "battery_action": action,
            "battery_kwh": abs(delta) + 0.0,
            "battery_energy_after_kwh": clean_nonnegative(solved.x[72 + h]),
        })
    # Preserve exact initial value in the serialized terminal field; verify that
    # actual actions reproduce it, so rounding cannot hide a depleted battery.
    if abs(hourly_plan[-1]["battery_energy_after_kwh"] - initial) > 1e-7:
        raise OptimizationError("Solver violated terminal battery equality")
    hourly_plan[-1]["battery_energy_after_kwh"] = initial
    errors = final_validator(hourly_plan, hours, battery, directives, tolerance=1e-7)
    if errors:
        raise OptimizationError("Final schedule replay failed: " + "; ".join(errors))
    totals = {
        "total_grid_kwh": math.fsum(row["grid_kwh"] for row in hourly_plan),
        "total_cost_bdt": math.fsum(row["grid_kwh"] * hours[row["hour"]]["tariff_bdt_per_kwh"] for row in hourly_plan),
        "peak_grid_kwh": max(row["grid_kwh"] for row in hourly_plan),
    }
    if not all(math.isfinite(value) for value in totals.values()):
        raise OptimizationError("Schedule totals are not finite")
    return {"hourly_plan": hourly_plan, **totals}
