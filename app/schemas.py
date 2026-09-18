from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


Number = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
Hour = Annotated[int, Field(strict=True, ge=0, le=23)]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HourInput(Schema):
    hour: Hour
    demand_kwh: Number
    solar_kwh: Number
    tariff_bdt_per_kwh: Number


class Battery(Schema):
    capacity_kwh: Number
    initial_energy_kwh: Number
    minimum_energy_kwh: Number
    max_charge_kwh_per_hour: Number
    max_discharge_kwh_per_hour: Number

    @model_validator(mode="after")
    def validate_bounds(self):
        if not self.minimum_energy_kwh <= self.initial_energy_kwh <= self.capacity_kwh:
            raise ValueError("Battery must satisfy minimum <= initial <= capacity")
        return self


class OptimizeRequest(Schema):
    scenario_id: str
    operator_notes: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1, max_length=3)
    hours: list[HourInput] = Field(min_length=24, max_length=24)
    battery: Battery

    @model_validator(mode="after")
    def validate_scenario(self):
        if any(not note.strip() for note in self.operator_notes):
            raise ValueError("Operator notes must not be blank")
        if {item.hour for item in self.hours} != set(range(24)):
            raise ValueError("Hours must contain every hour 0 through 23 exactly once")
        return self


class WindowAdjustment(Schema):
    hours: list[Hour] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def validate_hours(self):
        if self.hours != sorted(set(self.hours)):
            raise ValueError("Directive hours must be unique and ascending")
        return self


class SolarAdjustment(WindowAdjustment):
    factor: Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]


class ReserveAdjustment(WindowAdjustment):
    minimum_energy_kwh: Number


class GridAdjustment(WindowAdjustment):
    max_grid_kwh: Number


class DirectiveInterpretation(Schema):
    note_index: Annotated[int, Field(strict=True, ge=0)]
    applies: StrictBool = False
    directive_type: Literal["solar_reduction", "minimum_battery_reserve", "no_charge_window",
                            "no_discharge_window", "max_grid_window", "no_op"] = "no_op"
    structured_adjustment: SolarAdjustment | ReserveAdjustment | GridAdjustment | WindowAdjustment | None = None
    explanation: str = "placeholder"

    @model_validator(mode="after")
    def validate_directive_shape(self):
        expected = {"solar_reduction": SolarAdjustment, "minimum_battery_reserve": ReserveAdjustment,
                    "max_grid_window": GridAdjustment, "no_charge_window": WindowAdjustment,
                    "no_discharge_window": WindowAdjustment, "no_op": type(None)}[self.directive_type]
        if self.applies != (self.directive_type != "no_op") or type(self.structured_adjustment) is not expected:
            raise ValueError("Directive type, applies and adjustment must agree")
        return self


class HourlyPlan(Schema):
    hour: Hour
    grid_kwh: Number = 0
    solar_used_kwh: Number = 0
    battery_action: Literal["charge", "discharge", "idle"] = "idle"
    battery_kwh: Number = 0
    battery_energy_after_kwh: Number = 0


class OptimizeResponse(Schema):
    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourlyPlan] = Field(min_length=24, max_length=24)
    total_grid_kwh: Number = 0
    total_cost_bdt: Number = 0
    peak_grid_kwh: Number = 0
    plan_summary: str = "placeholder"
