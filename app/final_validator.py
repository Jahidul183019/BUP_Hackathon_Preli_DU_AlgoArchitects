"""Independently replay a schedule; an empty error list means it is valid."""

from __future__ import annotations

import math
from typing import Any

from .schedule_inputs import validate_inputs


def _finite_number(value: Any) -> bool:
    """Accept JSON numbers only, with booleans explicitly excluded."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def final_validator(
    hourly_plan: list[dict],
    hours: list[dict],
    battery: dict,
    directives: list[dict],
    *,
    tolerance: float = 0.01,
) -> list[str]:
    """Check schedule structure, physical constraints and final neutrality.

    Battery energy is replayed from the initial value using the returned actions;
    reported state-of-charge values never drive the replay. The optimizer's
    derived constraints and solver variables are deliberately not imported.

    Overlapping solar reductions use the smallest remaining fraction, applied
    once to the original forecast. Reserve floors use their maximum and grid
    caps their minimum. Arithmetic comparisons allow ``tolerance`` kWh; raw
    negative or non-finite schedule values are always invalid. Returns ``[]``
    for a valid schedule, or descriptive errors for invalid schedules/context.
    """
    if not _finite_number(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be a finite, non-negative number")

    try:
        input_hours, battery, directives = validate_inputs(hours, battery, directives)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return [f"Invalid schedule context: {exc}"]

    if not isinstance(hourly_plan, list):
        return ["hourly_plan must be a list containing exactly 24 hourly entries"]

    errors: list[str] = []
    if len(hourly_plan) != 24:
        errors.append(f"hourly_plan must contain exactly 24 entries; got {len(hourly_plan)}")

    plan_by_hour: dict[int, dict] = {}
    numeric_fields = (
        "grid_kwh",
        "solar_used_kwh",
        "battery_kwh",
        "battery_energy_after_kwh",
    )
    for position, entry in enumerate(hourly_plan):
        label = f"hourly_plan[{position}]"
        if not isinstance(entry, dict):
            errors.append(f"{label} must be an object")
            continue

        hour = entry.get("hour")
        if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
            errors.append(f"{label}.hour must be an integer from 0 through 23")
        elif hour in plan_by_hour:
            errors.append(f"hourly_plan contains duplicate hour {hour}")
        else:
            plan_by_hour[hour] = entry

        action = entry.get("battery_action")
        if action not in ("charge", "discharge", "idle"):
            errors.append(f"{label}.battery_action must be charge, discharge, or idle")
        for field in numeric_fields:
            value = entry.get(field)
            if not _finite_number(value) or value < 0:
                errors.append(f"{label}.{field} must be a finite, non-negative number")

    missing_hours = sorted(set(range(24)) - set(plan_by_hour))
    if missing_hours:
        errors.append(f"hourly_plan is missing hours {missing_hours}")
    if errors:
        return errors

    # Independently derive the physical limits directly from the directives.
    solar_factors = [1.0] * 24
    reserve_floors = [battery["minimum_energy_kwh"]] * 24
    grid_caps = [math.inf] * 24
    charge_banned = [False] * 24
    discharge_banned = [False] * 24
    for directive in directives:
        kind = directive["directive_type"]
        if kind == "no_op":
            continue
        adjustment = directive["structured_adjustment"]
        for hour in adjustment["hours"]:
            if kind == "solar_reduction":
                solar_factors[hour] = min(solar_factors[hour], adjustment["factor"])
            elif kind == "minimum_battery_reserve":
                reserve_floors[hour] = max(
                    reserve_floors[hour], adjustment["minimum_energy_kwh"]
                )
            elif kind == "max_grid_window":
                grid_caps[hour] = min(grid_caps[hour], adjustment["max_grid_kwh"])
            elif kind == "no_charge_window":
                charge_banned[hour] = True
            elif kind == "no_discharge_window":
                discharge_banned[hour] = True

    replayed_energy = float(battery["initial_energy_kwh"])
    for source in input_hours:
        hour = source["hour"]
        entry = plan_by_hour[hour]
        label = f"Hour {hour}"
        grid = float(entry["grid_kwh"])
        solar = float(entry["solar_used_kwh"])
        magnitude = float(entry["battery_kwh"])
        reported_energy = float(entry["battery_energy_after_kwh"])
        action = entry["battery_action"]
        charge = magnitude if action == "charge" else 0.0
        discharge = magnitude if action == "discharge" else 0.0

        if action == "idle" and magnitude != 0:
            errors.append(f"{label}: idle battery action must have battery_kwh equal to zero")
        if charge > battery["max_charge_kwh_per_hour"] + tolerance:
            errors.append(f"{label}: battery charging exceeds the hourly charge limit")
        if discharge > battery["max_discharge_kwh_per_hour"] + tolerance:
            errors.append(f"{label}: battery discharging exceeds the hourly discharge limit")
        if charge_banned[hour] and charge > tolerance:
            errors.append(f"{label}: battery charges during a no_charge_window")
        if discharge_banned[hour] and discharge > tolerance:
            errors.append(f"{label}: battery discharges during a no_discharge_window")

        solar_cap = source["solar_kwh"] * solar_factors[hour]
        if solar > solar_cap + tolerance:
            errors.append(f"{label}: solar_used_kwh exceeds the effective solar forecast")
        if grid > grid_caps[hour] + tolerance:
            errors.append(f"{label}: grid_kwh exceeds the active grid import cap")

        supplied = grid + solar + discharge
        required = float(source["demand_kwh"]) + charge
        if not math.isfinite(supplied) or not math.isfinite(required):
            errors.append(f"{label}: energy balance arithmetic is non-finite")
        elif abs(supplied - required) > tolerance:
            errors.append(
                f"{label}: energy balance fails (supply {supplied:g}, required {required:g} kWh)"
            )

        replayed_energy += charge - discharge
        if not math.isfinite(replayed_energy):
            errors.append(f"{label}: replayed battery energy is non-finite")
        elif abs(reported_energy - replayed_energy) > tolerance:
            errors.append(
                f"{label}: reported battery energy {reported_energy:g} differs from "
                f"replayed energy {replayed_energy:g} kWh"
            )

        for name, value in (("replayed", replayed_energy), ("reported", reported_energy)):
            if value < reserve_floors[hour] - tolerance:
                errors.append(f"{label}: {name} battery energy is below the active reserve")
            if value > battery["capacity_kwh"] + tolerance:
                errors.append(f"{label}: {name} battery energy exceeds battery capacity")

    initial_energy = battery["initial_energy_kwh"]
    if not math.isfinite(replayed_energy) or abs(replayed_energy - initial_energy) > tolerance:
        errors.append("End of day: replayed battery energy must equal initial_energy_kwh")
    if abs(plan_by_hour[23]["battery_energy_after_kwh"] - initial_energy) > tolerance:
        errors.append("End of day: reported battery energy must equal initial_energy_kwh")
    return errors
