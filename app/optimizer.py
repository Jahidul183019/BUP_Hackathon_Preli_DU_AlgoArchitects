from .schemas import DirectiveInterpretation, HourlyPlan, OptimizeRequest, OptimizeResponse


def optimize_energy(
    request: OptimizeRequest, directives: list[DirectiveInterpretation]
) -> OptimizeResponse:
    """Placeholder only: schema-correct zeros, not an energy-valid schedule."""
    return OptimizeResponse(
        scenario_id=request.scenario_id,
        directive_interpretation=directives,
        hourly_plan=[HourlyPlan(hour=h) for h in range(24)],
    )
