"""Offline example by default; --live calls the configured model and asserts semantics."""

import argparse
import json

from app.llm_interpreter import interpret_notes, validate_directives

NOTES = [
    "Solar output will drop to about 20% from 1 PM to 3 PM.",
    "Keep the battery at least half full between 6 PM and 9 PM.",
    "The cafeteria menu changes tomorrow.",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.live:
        result = interpret_notes(NOTES, capacity_kwh=200)
    else:
        # A model-response fixture tests plumbing, NOT actual model understanding.
        fixture = [
            {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
             "structured_adjustment": {"hours": [13, 14], "factor": 0.2}, "explanation": "20% remains."},
            {"note_index": 1, "applies": True, "directive_type": "minimum_battery_reserve",
             "structured_adjustment": {"hours": [18, 19, 20], "minimum_energy_kwh": 100},
             "explanation": "Half of 200 kWh is 100 kWh."},
            {"note_index": 2, "applies": False, "directive_type": "no_op",
             "structured_adjustment": None, "explanation": "Irrelevant menu change."},
        ]
        result = interpret_notes(NOTES, 200, completion=lambda system, context: json.dumps(fixture))
    checked = validate_directives(result, note_count=len(NOTES), capacity_kwh=200)
    print("LIVE MODEL" if args.live else "OFFLINE MOCK (no model call)")
    print(json.dumps(checked, indent=2, allow_nan=False))
    assert [x["note_index"] for x in checked] == [0, 1, 2]
    assert [x["directive_type"] for x in checked] == ["solar_reduction", "minimum_battery_reserve", "no_op"]
    assert checked[0]["structured_adjustment"] == {"hours": [13, 14], "factor": 0.2}
    assert checked[1]["structured_adjustment"] == {"hours": [18, 19, 20], "minimum_energy_kwh": 100}
    assert checked[2]["applies"] is False and checked[2]["structured_adjustment"] is None
    print("All three example interpretations passed.")


if __name__ == "__main__":
    main()
