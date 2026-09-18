from .schemas import DirectiveInterpretation, OptimizeRequest


def interpret_notes(request: OptimizeRequest) -> list[DirectiveInterpretation]:
    """Placeholder only: one no_op entry per note; no LLM calls."""
    return [DirectiveInterpretation(note_index=i) for i in range(len(request.operator_notes))]
