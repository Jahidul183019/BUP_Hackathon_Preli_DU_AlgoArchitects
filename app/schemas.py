from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class DirectiveInterpretation(Schema):
    # Expand this placeholder contract when the real interpreter is added.
    note_index: Annotated[int, Field(ge=0)]
    applies: Literal[False] = False
    directive_type: Literal["no_op"] = "no_op"
    structured_adjustment: None = None
    explanation: str = "placeholder"


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
