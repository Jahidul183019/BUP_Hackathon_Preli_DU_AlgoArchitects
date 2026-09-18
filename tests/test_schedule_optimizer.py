"""Offline LP/replay checks; public references permit equivalent optimal schedules."""

import copy
import json
import unittest
from pathlib import Path

from app.final_validator import final_validator
from app.schedule_optimizer import InfeasibleScheduleError, optimize_schedule


def sample_hours(demand=10.0, solar=0.0, tariff=1.0):
    return [
        {"hour": h, "demand_kwh": demand, "solar_kwh": solar,
         "tariff_bdt_per_kwh": tariff}
        for h in range(24)
    ]


def sample_battery(**overrides):
    battery = {
        "capacity_kwh": 100.0, "initial_energy_kwh": 50.0,
        "minimum_energy_kwh": 0.0, "max_charge_kwh_per_hour": 20.0,
        "max_discharge_kwh_per_hour": 20.0,
    }
    battery.update(overrides)
    return battery


def directive(kind, hours, index=0, **values):
    return {
        "note_index": index, "applies": True, "directive_type": kind,
        "structured_adjustment": {"hours": hours, **values},
        "explanation": "Test directive",
    }


def idle_plan(energy=50.0):
    return [
        {"hour": h, "grid_kwh": 10.0, "solar_used_kwh": 0.0,
         "battery_action": "idle", "battery_kwh": 0.0,
         "battery_energy_after_kwh": energy}
        for h in range(24)
    ]


def cycle_plan():
    plan = idle_plan()
    plan[0].update(grid_kwh=15.0, battery_action="charge", battery_kwh=5.0,
                   battery_energy_after_kwh=55.0)
    plan[1].update(grid_kwh=5.0, battery_action="discharge", battery_kwh=5.0)
    return plan


class OptimizerTests(unittest.TestCase):
    def assert_valid_result(self, result, hours, battery, directives):
        self.assertEqual(
            set(result),
            {"hourly_plan", "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh"},
        )
        plan = result["hourly_plan"]
        self.assertEqual(len(plan), 24)
        self.assertEqual([entry["hour"] for entry in plan], list(range(24)))
        self.assertEqual(final_validator(plan, hours, battery, directives), [])
        prices = {hour["hour"]: hour["tariff_bdt_per_kwh"] for hour in hours}
        self.assertAlmostEqual(result["total_grid_kwh"], sum(p["grid_kwh"] for p in plan), places=7)
        self.assertAlmostEqual(result["total_cost_bdt"], sum(p["grid_kwh"] * prices[p["hour"]] for p in plan), places=7)
        self.assertAlmostEqual(result["peak_grid_kwh"], max(p["grid_kwh"] for p in plan), places=7)
        self.assertAlmostEqual(plan[-1]["battery_energy_after_kwh"], battery["initial_energy_kwh"], places=7)

    def test_all_ten_public_reference_costs_and_schedule_replay(self):
        fixture = Path(__file__).parent / "fixtures" / "public_optimizer_cases.json"
        cases = json.loads(fixture.read_text())["cases"]
        self.assertEqual(len(cases), 10)
        for case in cases:
            with self.subTest(case=case["id"]):
                request, reference = case["input"], case["expected_output"]
                hours, battery = request["hours"], request["battery"]
                directives = reference["directive_interpretation"]
                originals = copy.deepcopy((hours, battery, directives))
                result = optimize_schedule(hours, battery, directives)
                self.assert_valid_result(result, hours, battery, directives)
                self.assertAlmostEqual(result["total_cost_bdt"], reference["total_cost_bdt"], delta=0.01)
                self.assertEqual((hours, battery, directives), originals)

    def test_evening_grid_cap_requires_charging_four_earlier_hours(self):
        hours = sample_hours(demand=0.0, tariff=3.0)
        for h in range(4):
            hours[h]["tariff_bdt_per_kwh"] = 1.0
        hours[18]["demand_kwh"] = 8.0
        battery = sample_battery(capacity_kwh=8.0, initial_energy_kwh=0.0,
                                 max_charge_kwh_per_hour=2.0, max_discharge_kwh_per_hour=8.0)
        directives = [directive("max_grid_window", [18], max_grid_kwh=0.0)]
        result = optimize_schedule(hours, battery, directives)
        self.assert_valid_result(result, hours, battery, directives)
        self.assertAlmostEqual(result["total_cost_bdt"], 8.0)
        for row in result["hourly_plan"][:4]:
            self.assertEqual(row["battery_action"], "charge")
            self.assertAlmostEqual(row["battery_kwh"], 2.0)
        self.assertAlmostEqual(result["hourly_plan"][18]["grid_kwh"], 0.0)

    def test_terminal_neutrality_prevents_spending_initial_energy(self):
        hours, battery = sample_hours(tariff=10.0), sample_battery()
        result = optimize_schedule(hours, battery, [])
        self.assert_valid_result(result, hours, battery, [])
        self.assertAlmostEqual(result["total_grid_kwh"], 240.0)
        self.assertAlmostEqual(result["total_cost_bdt"], 2400.0)

    def test_zero_capacity_rates_zero_tariffs_and_solar_curtailment(self):
        hours = sample_hours(demand=2.0, solar=5.0, tariff=0.0)
        battery = sample_battery(capacity_kwh=0.0, initial_energy_kwh=0.0,
                                 max_charge_kwh_per_hour=0.0, max_discharge_kwh_per_hour=0.0)
        result = optimize_schedule(hours, battery, [])
        self.assert_valid_result(result, hours, battery, [])
        self.assertEqual(result["total_cost_bdt"], 0.0)
        for row in result["hourly_plan"]:
            self.assertEqual(row["battery_action"], "idle")
            self.assertEqual(row["battery_kwh"], 0.0)
            self.assertLessEqual(row["solar_used_kwh"], 2.0)

    def test_fractional_values(self):
        hours = sample_hours(demand=1.15, solar=0.35, tariff=1.1)
        for h in range(1, 24, 2):
            hours[h]["tariff_bdt_per_kwh"] = 3.2
        battery = sample_battery(capacity_kwh=1.3, initial_energy_kwh=0.7,
                                 minimum_energy_kwh=0.2, max_charge_kwh_per_hour=0.4,
                                 max_discharge_kwh_per_hour=0.3)
        self.assert_valid_result(optimize_schedule(hours, battery, []), hours, battery, [])

    def test_both_battery_bans_force_idle(self):
        hours, battery = sample_hours(), sample_battery()
        directives = [directive("no_charge_window", list(range(24))),
                      directive("no_discharge_window", list(range(24)), index=1)]
        result = optimize_schedule(hours, battery, directives)
        self.assert_valid_result(result, hours, battery, directives)
        self.assertTrue(all(row["battery_action"] == "idle" for row in result["hourly_plan"]))

    def test_overlapping_solar_caps_apply_strictest_factor_once(self):
        hours = sample_hours(demand=10.0, solar=10.0)
        battery = sample_battery(capacity_kwh=0.0, initial_energy_kwh=0.0)
        directives = [directive("solar_reduction", [0], factor=0.8),
                      directive("solar_reduction", [0], index=1, factor=0.5)]
        result = optimize_schedule(hours, battery, directives)
        self.assert_valid_result(result, hours, battery, directives)
        self.assertAlmostEqual(result["total_cost_bdt"], 5.0)

    def test_overlapping_grid_and_reserve_limits_take_strictest(self):
        hours, battery = sample_hours(), sample_battery()
        directives = [directive("minimum_battery_reserve", [0], minimum_energy_kwh=55.0),
                      directive("minimum_battery_reserve", [0], index=1, minimum_energy_kwh=60.0),
                      directive("max_grid_window", [1], index=2, max_grid_kwh=8.0),
                      directive("max_grid_window", [1], index=3, max_grid_kwh=5.0)]
        result = optimize_schedule(hours, battery, directives)
        self.assert_valid_result(result, hours, battery, directives)
        self.assertGreaterEqual(result["hourly_plan"][0]["battery_energy_after_kwh"], 60.0 - 1e-7)
        self.assertLessEqual(result["hourly_plan"][1]["grid_kwh"], 5.0 + 1e-7)

    def test_infeasible_limits_are_explicit_errors(self):
        for directives in (
            [directive("max_grid_window", list(range(24)), max_grid_kwh=0.0)],
            [directive("minimum_battery_reserve", [23], minimum_energy_kwh=51.0)],
        ):
            with self.subTest(directives=directives), self.assertRaises(InfeasibleScheduleError):
                optimize_schedule(sample_hours(), sample_battery(), directives)

    def test_invalid_input_is_rejected(self):
        inputs = []
        inputs.append((sample_hours()[:-1], sample_battery(), []))
        for field, value in (("hour", True), ("hour", 1), ("demand_kwh", float("nan")),
                             ("solar_kwh", -1.0), ("tariff_bdt_per_kwh", float("inf"))):
            hours = sample_hours()
            hours[0][field] = value
            inputs.append((hours, sample_battery(), []))
        for overrides in ({"initial_energy_kwh": 101.0}, {"max_charge_kwh_per_hour": -1.0},
                          {"capacity_kwh": True}):
            inputs.append((sample_hours(), sample_battery(**overrides), []))
        for bad in (directive("unknown", [0]), directive("solar_reduction", [0], factor=1.1),
                    directive("no_charge_window", [24])):
            inputs.append((sample_hours(), sample_battery(), [bad]))
        for i, args in enumerate(inputs):
            with self.subTest(input=i), self.assertRaises(ValueError):
                optimize_schedule(*args)


class FinalValidatorTests(unittest.TestCase):
    def assert_invalid(self, plan, hours=None, battery=None, directives=None):
        errors = final_validator(plan, hours or sample_hours(), battery or sample_battery(), directives or [])
        self.assertIsInstance(errors, list)
        self.assertTrue(errors, "Invalid schedule passed independent validation")

    def test_handwritten_valid_plans(self):
        for plan in (idle_plan(), cycle_plan()):
            self.assertEqual(final_validator(plan, sample_hours(), sample_battery(), []), [])

    def test_invalid_numbers_actions_and_energy_balance(self):
        mutations = [("grid_kwh", 9.0), ("grid_kwh", -1.0), ("grid_kwh", float("nan")),
                     ("solar_used_kwh", float("inf")), ("battery_kwh", True),
                     ("battery_energy_after_kwh", float("nan")), ("hour", True),
                     ("battery_action", "unknown"), ("battery_kwh", 1.0)]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                plan = idle_plan()
                plan[0][field] = value
                self.assert_invalid(plan)

    def test_missing_and_duplicate_hours_fail_but_reordering_is_replayed_by_hour(self):
        self.assert_invalid(idle_plan()[:-1])
        plan = idle_plan()
        plan[1]["hour"] = 0
        self.assert_invalid(plan)
        plan = idle_plan()
        plan[0], plan[1] = plan[1], plan[0]
        self.assertEqual(final_validator(plan, sample_hours(), sample_battery(), []), [])

    def test_reported_states_cannot_hide_replay_drift(self):
        plan = cycle_plan()
        plan[0]["battery_energy_after_kwh"] = 50.0
        self.assert_invalid(plan)
        plan = idle_plan()
        plan[12]["battery_energy_after_kwh"] = 49.0
        self.assert_invalid(plan)

    def test_small_per_hour_state_errors_accumulate_in_independent_replay(self):
        # Trusting each reported state would hide 0.12 kWh of day-end depletion.
        plan = idle_plan()
        for row in plan:
            row.update(grid_kwh=9.995, battery_action="discharge", battery_kwh=0.005)
        self.assertEqual(plan[-1]["battery_energy_after_kwh"], 50.0)
        self.assert_invalid(plan)

    def test_rate_limits_capacity_and_minimum_energy(self):
        for overrides in ({"max_charge_kwh_per_hour": 4.0}, {"max_discharge_kwh_per_hour": 4.0},
                          {"capacity_kwh": 54.0}):
            with self.subTest(overrides=overrides):
                self.assert_invalid(cycle_plan(), battery=sample_battery(**overrides))
        plan = idle_plan()
        plan[0].update(grid_kwh=5.0, battery_action="discharge", battery_kwh=5.0,
                       battery_energy_after_kwh=45.0)
        plan[1].update(grid_kwh=15.0, battery_action="charge", battery_kwh=5.0)
        self.assert_invalid(plan, battery=sample_battery(minimum_energy_kwh=46.0))

    def test_each_operational_directive_is_enforced(self):
        cases = [
            (cycle_plan(), sample_hours(), directive("no_charge_window", [0])),
            (cycle_plan(), sample_hours(), directive("no_discharge_window", [1])),
            (idle_plan(), sample_hours(), directive("minimum_battery_reserve", [2], minimum_energy_kwh=51.0)),
            (idle_plan(), sample_hours(), directive("max_grid_window", [2], max_grid_kwh=9.0)),
        ]
        hours, plan = sample_hours(), idle_plan()
        hours[0]["solar_kwh"] = 5.0
        plan[0].update(grid_kwh=5.0, solar_used_kwh=5.0)
        self.assertEqual(final_validator(plan, hours, sample_battery(), []), [])
        cases.append((plan, hours, directive("solar_reduction", [0], factor=0.5)))
        for plan, hours, constraint in cases:
            with self.subTest(type=constraint["directive_type"]):
                self.assert_invalid(plan, hours=hours, directives=[constraint])

    def test_final_energy_must_return_to_initial_even_if_other_constraints_hold(self):
        plan = idle_plan(energy=55.0)
        plan[0].update(grid_kwh=15.0, battery_action="charge", battery_kwh=5.0)
        self.assert_invalid(plan)


if __name__ == "__main__":
    unittest.main()
