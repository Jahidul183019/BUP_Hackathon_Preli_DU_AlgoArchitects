"""Compare standalone optimization with organizer reference costs; no LLM calls."""

import argparse
import json
from pathlib import Path

from app.final_validator import final_validator
from app.schedule_optimizer import optimize_schedule


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="Run all 10 public cases")
    parser.add_argument("--show-plan", action="store_true", help="Print the returned schedule JSON")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "tests/fixtures/public_optimizer_cases.json"
    cases = json.loads(source.read_text())["cases"]
    for case in cases:
        if not args.all and case["id"] not in {"SAMPLE-05", "SAMPLE-10"}:
            continue
        request, expected = case["input"], case["expected_output"]
        # Ground-truth directives isolate optimizer testing from LLM accuracy.
        directives = expected["directive_interpretation"]
        result = optimize_schedule(request["hours"], request["battery"], directives)
        errors = final_validator(result["hourly_plan"], request["hours"], request["battery"], directives)
        if errors:
            raise AssertionError(f"{case['id']} replay failed: {errors}")
        difference = abs(result["total_cost_bdt"] - expected["total_cost_bdt"])
        if difference > 0.01:
            raise AssertionError(f"{case['id']} differs from reference cost by {difference}")
        print(f"{case['id']}: cost={result['total_cost_bdt']:.2f} BDT; "
              f"reference={expected['total_cost_bdt']:.2f}; replay=PASS")
        if args.show_plan:
            print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
