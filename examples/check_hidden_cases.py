"""Grade the service against locally authored hidden-style cases, judge style.

Mirrors the published evaluation order: interpretation ground truth first, then
downstream directive application replayed against the TRUE directives, then the
normal GridWise consistency checks, and only then cost quality. A case that is
invalid under ground truth earns no optimization credit, exactly as the rubric
states. Prints case ids, verdicts and timings only; never raw response bodies.
"""

import argparse
import json
import math
import time
from pathlib import Path

import httpx

from app.final_validator import final_validator
from app.schemas import OptimizeResponse

TOLERANCE = 0.01
FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/hidden_simulation_cases.json"


def close(actual, expected):
    return (type(actual) in (int, float) and not isinstance(actual, bool)
            and math.isfinite(actual) and abs(actual - expected) <= TOLERANCE)


def compare_adjustment(actual, expected):
    """Compare structured_adjustment against ground truth, tolerating float noise."""
    if expected is None:
        return actual is None, "expected null adjustment"
    if not isinstance(actual, dict) or set(actual) != set(expected):
        return False, "adjustment shape differs from ground truth"
    if actual.get("hours") != expected["hours"]:
        return False, f"hours {actual.get('hours')} != ground truth {expected['hours']}"
    for key, value in expected.items():
        if key == "hours":
            continue
        if not close(actual.get(key), value):
            return False, f"{key}={actual.get(key)} != ground truth {value}"
    return True, ""


def grade_interpretation(returned, truth):
    """Per-note interpretation scoring; returns (matched_notes, total, failures)."""
    failures = []
    if not isinstance(returned, list) or len(returned) != len(truth):
        return 0, len(truth), [f"expected {len(truth)} entries, got "
                               f"{len(returned) if isinstance(returned, list) else 'non-list'}"]
    if [e.get("note_index") for e in returned] != list(range(len(truth))):
        failures.append("directive_interpretation is not in note_index order 0..N-1")
    matched = 0
    for expected in truth:
        index = expected["note_index"]
        actual = returned[index]
        problems = []
        if actual.get("applies") is not expected["applies"]:
            problems.append(f"applies={actual.get('applies')} != {expected['applies']}")
        if actual.get("directive_type") != expected["directive_type"]:
            problems.append(f"type={actual.get('directive_type')} != {expected['directive_type']}")
        else:
            ok, reason = compare_adjustment(actual.get("structured_adjustment"),
                                            expected["structured_adjustment"])
            if not ok:
                problems.append(reason)
        explanation = actual.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            problems.append("explanation missing or blank")
        if problems:
            failures.append(f"note {index}: " + "; ".join(problems))
        else:
            matched += 1
    return matched, len(truth), failures


def grade_case(case, payload, elapsed):
    """Return a verdict dict for one scored case."""
    request, truth = case["input"], case["ground_truth"]["directive_interpretation"]
    optimal = case["ground_truth"]["optimal_cost_bdt"]
    verdict = {"id": case["id"], "probe": case["probe"], "seconds": elapsed,
               "binding": case["ground_truth"]["directive_is_binding"],
               "interpretation_failures": [], "application_failures": [],
               "consistency_failures": [], "quality_ratio": 0.0}

    try:
        result = OptimizeResponse.model_validate(payload).model_dump()
    except Exception:
        verdict["consistency_failures"].append("response does not satisfy the required schema")
        verdict["notes_matched"], verdict["notes_total"] = 0, len(truth)
        verdict["valid"] = False
        return verdict

    if result["scenario_id"] != request["scenario_id"]:
        verdict["consistency_failures"].append("scenario_id not echoed")

    matched, total, failures = grade_interpretation(result["directive_interpretation"], truth)
    verdict["notes_matched"], verdict["notes_total"] = matched, total
    verdict["interpretation_failures"] = failures

    # The judge replays using the TRUE directives, not the team's interpretation.
    plan = result["hourly_plan"]
    verdict["application_failures"] = final_validator(plan, request["hours"], request["battery"], truth)

    prices = {h["hour"]: h["tariff_bdt_per_kwh"] for h in request["hours"]}
    recomputed = {
        "total_grid_kwh": math.fsum(row["grid_kwh"] for row in plan),
        "total_cost_bdt": math.fsum(row["grid_kwh"] * prices[row["hour"]] for row in plan),
        "peak_grid_kwh": max(row["grid_kwh"] for row in plan),
    }
    for field, value in recomputed.items():
        if not close(result[field], value):
            verdict["consistency_failures"].append(
                f"{field} reported {result[field]:.4f} but recomputes to {value:.4f}")

    verdict["cost"] = recomputed["total_cost_bdt"]
    verdict["optimal"] = optimal
    verdict["valid"] = not (verdict["application_failures"] or verdict["consistency_failures"])
    if verdict["valid"] and recomputed["total_cost_bdt"] > 0:
        verdict["quality_ratio"] = min(1.0, optimal / recomputed["total_cost_bdt"])
    elif verdict["valid"]:
        verdict["quality_ratio"] = 1.0
    # Cheaper than the ground-truth optimum means a constraint was ignored.
    if verdict["valid"] and recomputed["total_cost_bdt"] < optimal - TOLERANCE:
        verdict["consistency_failures"].append(
            f"cost {recomputed['total_cost_bdt']:.2f} is below the ground-truth optimum "
            f"{optimal:.2f}; a directive was probably not applied")
        verdict["valid"] = False
        verdict["quality_ratio"] = 0.0
    return verdict


def run(client, case):
    start = time.monotonic()
    try:
        response = client.post("/optimize-energy", json=case["input"])
        elapsed = time.monotonic() - start
        return response.status_code, (response.json() if response.status_code == 200 else None), elapsed
    except Exception:
        return None, None, time.monotonic() - start


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--only", help="run a single case id")
    args = parser.parse_args()

    data = json.loads(FIXTURE.read_text())
    cases = [c for c in data["cases"] if not args.only or c["id"] == args.only]
    probes = data["robustness_cases"] if not args.only else []

    verdicts, timings = [], []
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=35) as client:
        health = client.get("/health")
        print(f"health: HTTP {health.status_code} {health.json() if health.status_code == 200 else ''}")
        for round_index in range(args.rounds):
            for case in cases:
                status, payload, elapsed = run(client, case)
                timings.append(elapsed)
                if status != 200:
                    verdict = {"id": case["id"], "probe": case["probe"], "seconds": elapsed,
                               "binding": case["ground_truth"]["directive_is_binding"],
                               "notes_matched": 0, "notes_total": len(case["ground_truth"]["directive_interpretation"]),
                               "interpretation_failures": [f"HTTP {status}"], "application_failures": [],
                               "consistency_failures": [f"expected HTTP 200, got {status}"],
                               "quality_ratio": 0.0, "valid": False, "cost": float("nan"),
                               "optimal": case["ground_truth"]["optimal_cost_bdt"]}
                else:
                    verdict = grade_case(case, payload, elapsed)
                verdicts.append(verdict)
                mark = "PASS" if (verdict["valid"] and not verdict["interpretation_failures"]) else "FAIL"
                print(f"round={round_index + 1} {verdict['id']:<7} {mark:<4} "
                      f"notes={verdict['notes_matched']}/{verdict['notes_total']} "
                      f"valid={str(verdict['valid']):<5} "
                      f"quality={verdict['quality_ratio']:.3f} {elapsed:6.2f}s  {verdict['probe'][:54]}")
                for reason in (verdict["interpretation_failures"] + verdict["application_failures"]
                               + verdict["consistency_failures"]):
                    print(f"         - {reason}")

        print()
        for probe in probes:
            status, payload, elapsed = run(client, probe)
            timings.append(elapsed)
            safe = status in (200, 400, 422, 500, 503, 504)
            detail = ""
            if status == 200 and payload:
                try:
                    checked = OptimizeResponse.model_validate(payload).model_dump()
                    errors = final_validator(checked["hourly_plan"], probe["input"]["hours"],
                                             probe["input"]["battery"], checked["directive_interpretation"])
                    kinds = [e["directive_type"] for e in checked["directive_interpretation"]]
                    detail = f"types={kinds} schedule_valid_under_own_directives={not errors}"
                    safe = safe and not errors
                except Exception:
                    detail, safe = "response failed schema validation", False
            print(f"{probe['id']:<7} {'SAFE' if safe else 'UNSAFE'} HTTP {status} {elapsed:6.2f}s  {probe['probe'][:48]}")
            if detail:
                print(f"         {detail}")
            print(f"         expected: {probe['accept']}")

    scored = [v for v in verdicts]
    if scored:
        notes_matched = sum(v["notes_matched"] for v in scored)
        notes_total = sum(v["notes_total"] for v in scored)
        valid = sum(1 for v in scored if v["valid"])
        clean = sum(1 for v in scored if v["valid"] and not v["interpretation_failures"])
        quality = sum(v["quality_ratio"] for v in scored) / len(scored)
        p95 = sorted(timings)[math.ceil(0.95 * len(timings)) - 1]
        print(f"\n{'=' * 78}")
        print(f"Cases fully correct:      {clean}/{len(scored)}")
        print(f"Notes interpreted right:  {notes_matched}/{notes_total}")
        print(f"Schedules valid vs truth: {valid}/{len(scored)}")
        print(f"Mean quality ratio:       {quality:.4f}  (Optimization Quality = {10 * quality:.2f}/10)")
        print(f"Latency p95:              {p95:.2f}s   max={max(timings):.2f}s")
        print(f"{'=' * 78}")
    raise SystemExit(0 if all(v["valid"] and not v["interpretation_failures"] for v in verdicts) else 1)


if __name__ == "__main__":
    main()
