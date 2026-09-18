"""Test a running API over HTTP with real providers. Never print raw failure bodies."""

import argparse
import json
import math
import time
from pathlib import Path

import httpx

from app.final_validator import final_validator
from app.schemas import OptimizeResponse


def equivalent(actual, expected):
    if isinstance(expected, bool) or expected is None or isinstance(expected, str):
        return actual == expected and type(actual) is type(expected)
    if isinstance(expected, (int, float)):
        return type(actual) in (int, float) and math.isfinite(actual) and abs(actual - expected) <= 0.01
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(equivalent(a, b) for a, b in zip(actual, expected))
    if isinstance(expected, dict):
        return isinstance(actual, dict) and set(actual) == set(expected) and all(equivalent(actual[k], expected[k]) for k in expected)
    return False


def check_result(result, case):
    result = OptimizeResponse.model_validate(result).model_dump()
    request, expected = case["input"], case["expected_output"]
    if result["scenario_id"] != request["scenario_id"]:
        return False
    actual = result["directive_interpretation"]
    truth = expected["directive_interpretation"]
    if len(actual) != len(truth):
        return False
    for a, b in zip(actual, truth):
        for key in ["note_index", "applies", "directive_type", "structured_adjustment"]:
            if not equivalent(a[key], b[key]):
                return False
    plan = result["hourly_plan"]
    if final_validator(plan, request["hours"], request["battery"], truth):
        return False
    prices = {h["hour"]: h["tariff_bdt_per_kwh"] for h in request["hours"]}
    computed = {"total_grid_kwh": math.fsum(p["grid_kwh"] for p in plan),
                "total_cost_bdt": math.fsum(p["grid_kwh"] * prices[p["hour"]] for p in plan),
                "peak_grid_kwh": max(p["grid_kwh"] for p in plan)}
    return all(abs(result[k] - v) <= 0.01 for k, v in computed.items()) and abs(result["total_cost_bdt"] - expected["total_cost_bdt"]) <= 0.01


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--rounds", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 10:
        raise SystemExit("rounds must be between 1 and 10")
    source = Path(__file__).resolve().parents[1] / "tests/fixtures/public_optimizer_cases.json"
    cases = json.loads(source.read_text())["cases"]
    timings, passed = [], 0
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=30, follow_redirects=False) as client:
        try:
            health = client.get("/health")
            if health.status_code != 200 or health.json() != {"status": "ok"}:
                raise ValueError()
            if client.post("/optimize-energy", json={}).status_code != 400:
                raise ValueError()
            if client.post("/optimize-energy", content="{", headers={"Content-Type": "application/json"}).status_code != 400:
                raise ValueError()
        except Exception:
            raise SystemExit("Health/input checks failed. Check the running API; no response body was printed.") from None
        for round_index in range(args.rounds):
            for case in cases:
                start = time.monotonic()
                status, valid = None, False
                try:
                    response = client.post("/optimize-energy", json=case["input"])
                    status = response.status_code
                    valid = status == 200 and check_result(response.json(), case)
                except Exception:
                    pass
                elapsed = time.monotonic() - start
                timings.append(elapsed)
                valid = valid and elapsed < 30
                passed += int(valid)
                print(f"round={round_index + 1} case={case['id']} status={status} seconds={elapsed:.3f} result={'PASS' if valid else 'FAIL'}", flush=True)
    p95 = sorted(timings)[math.ceil(0.95 * len(timings)) - 1]
    print(f"Passed {passed}/{len(timings)}; p95={p95:.3f}s; max={max(timings):.3f}s")
    raise SystemExit(0 if passed == len(timings) else 1)


if __name__ == "__main__":
    main()
