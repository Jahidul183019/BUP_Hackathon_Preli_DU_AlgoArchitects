"""Input shape validation only; optimizer and replay derive constraints separately."""

import copy
import math

from .schemas import Battery, HourInput


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except (OverflowError, TypeError):
        return False


def validate_inputs(hours: list[dict], battery: dict, directives: list[dict]):
    """Reject invalid inputs rather than silently ignoring operational constraints."""
    if not isinstance(hours, list) or len(hours) != 24:
        raise ValueError("hours must contain 24 entries")
    if any(not isinstance(row, dict) for row in hours) or not isinstance(battery, dict):
        raise ValueError("hours entries and battery must be objects")
    rows = sorted((HourInput.model_validate(row).model_dump() for row in hours), key=lambda r: r["hour"])
    if [row["hour"] for row in rows] != list(range(24)):
        raise ValueError("hours must contain each hour 0 through 23 exactly once")
    battery = Battery.model_validate(battery).model_dump()
    if not isinstance(directives, list):
        raise ValueError("directives must be a list")
    shapes = {
        "solar_reduction": {"hours", "factor"},
        "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
        "no_charge_window": {"hours"},
        "no_discharge_window": {"hours"},
        "max_grid_window": {"hours", "max_grid_kwh"},
        "no_op": None,
    }
    for directive in directives:
        if not isinstance(directive, dict):
            raise ValueError("Each directive must be an object")
        kind = directive.get("directive_type")
        if not isinstance(kind, str) or kind not in shapes:
            raise ValueError("Unsupported directive type")
        if "applies" not in directive or "structured_adjustment" not in directive:
            raise ValueError("Directive missing applies or structured_adjustment")
        adjustment = directive["structured_adjustment"]
        if kind == "no_op":
            if directive["applies"] is not False or adjustment is not None:
                raise ValueError("no_op requires applies=false and null adjustment")
            continue
        if directive["applies"] is not True:
            raise ValueError("Operational directives require applies=true")
        if not isinstance(adjustment, dict) or set(adjustment) != shapes[kind]:
            raise ValueError("Incorrect structured_adjustment shape")
        window = adjustment["hours"]
        if not isinstance(window, list) or not window:
            raise ValueError("Directive hours must be a nonempty list")
        if any(type(h) is not int or not 0 <= h <= 23 for h in window):
            raise ValueError("Directive hours must be integers from 0 to 23")
        if window != sorted(set(window)):
            raise ValueError("Directive hours must be unique and ascending")
        for key in shapes[kind] - {"hours"}:
            if not _number(adjustment[key]):
                raise ValueError("Directive values must be finite nonnegative numbers")
        if kind == "solar_reduction" and adjustment["factor"] > 1:
            raise ValueError("Solar factor must not exceed 1")
        if kind == "minimum_battery_reserve" and adjustment["minimum_energy_kwh"] > battery["capacity_kwh"]:
            raise ValueError("Reserve must not exceed battery capacity")
    return rows, battery, copy.deepcopy(directives)
