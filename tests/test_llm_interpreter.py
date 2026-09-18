import copy
import json
import unittest
from unittest.mock import Mock, patch

from app.llm_interpreter import interpret_notes, validate_directives


NOTES = [
    "Solar output will drop to about 20% from 1 PM to 3 PM.",
    "Keep the battery at least half full between 6 PM and 9 PM.",
    "The cafeteria menu changes tomorrow.",
]
EXPECTED = [
    {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [13, 14], "factor": 0.2}, "explanation": "20% remains."},
    {"note_index": 1, "applies": True, "directive_type": "minimum_battery_reserve",
     "structured_adjustment": {"hours": [18, 19, 20], "minimum_energy_kwh": 100},
     "explanation": "Half of 200 kWh is 100 kWh."},
    {"note_index": 2, "applies": False, "directive_type": "no_op",
     "structured_adjustment": None, "explanation": "Menu changes do not affect energy."},
]


class InterpreterTests(unittest.TestCase):
    def validate(self, entries):
        return validate_directives(entries, note_count=3, capacity_kwh=200)

    def test_examples_with_mocked_model(self):
        completion = Mock(return_value=json.dumps(EXPECTED))
        result = interpret_notes(NOTES, 200, completion=completion)
        self.assertEqual(result, EXPECTED)
        self.assertEqual(self.validate(result), EXPECTED)
        completion.assert_called_once()
        context = json.loads(completion.call_args.args[1])
        self.assertEqual(context, {"capacity_kwh": 200, "operator_notes": NOTES})

    def test_remaining_three_directives(self):
        for kind, adjustment in [
            ("no_charge_window", {"hours": [0, 1]}),
            ("no_discharge_window", {"hours": [18, 19]}),
            ("max_grid_window", {"hours": [18, 19], "max_grid_kwh": 155}),
        ]:
            with self.subTest(kind=kind):
                entries = copy.deepcopy(EXPECTED)
                entries[0].update(directive_type=kind, structured_adjustment=adjustment)
                self.assertEqual(self.validate(entries), entries)

    def test_malformed_json_or_provider_failure(self):
        for raw in ["not json", "```json\n[]\n```", "[", "null", "{}", None,
                    '[{"note_index":0,"note_index":1}]']:
            with self.subTest(raw=raw):
                result = interpret_notes(NOTES, 200, completion=Mock(return_value=raw))
                self.assertEqual([x["note_index"] for x in result], [0, 1, 2])
                self.assertTrue(all(x["directive_type"] == "no_op" for x in result))
        with self.assertLogs("app.llm_interpreter", level="WARNING") as captured:
            result = interpret_notes(NOTES, 200, completion=Mock(side_effect=TimeoutError("secret-value")))
        self.assertEqual(len(result), 3)
        self.assertNotIn("secret-value", " ".join(captured.output))

    def test_invalid_entries_fallback_only_affected_note(self):
        variants = [
            {"directive_type": "invented"}, {"directive_type": []},
            {"applies": False}, {"applies": 1}, {"explanation": None},
            {"structured_adjustment": {"hours": [14, 13], "factor": 0.2}},
            {"structured_adjustment": {"hours": [13, 13], "factor": 0.2}},
            {"structured_adjustment": {"hours": [True], "factor": 0.2}},
            {"structured_adjustment": {"hours": [13.0], "factor": 0.2}},
            {"structured_adjustment": {"hours": [24], "factor": 0.2}},
            {"structured_adjustment": {"hours": [], "factor": 0.2}},
            {"structured_adjustment": {"hours": [13]}},
            {"structured_adjustment": {"hours": [13], "factor": 0.2, "invented": 1}},
        ]
        variants += [{"structured_adjustment": {"hours": [13], "factor": value}}
                     for value in [-0.1, 1.1, float("nan"), float("inf"), True, "0.2"]]
        for update in variants:
            with self.subTest(update=update):
                entries = copy.deepcopy(EXPECTED)
                entries[0].update(update)
                result = self.validate(entries)
                self.assertEqual(result[0]["directive_type"], "no_op")
                self.assertEqual(result[1:], EXPECTED[1:])

    def test_reserve_and_grid_caps(self):
        for kind, field, values in [
            ("minimum_battery_reserve", "minimum_energy_kwh", [-1, 201, float("nan"), float("inf"), True]),
            ("max_grid_window", "max_grid_kwh", [-1, float("nan"), float("inf"), False]),
        ]:
            for value in values:
                with self.subTest(kind=kind, value=value):
                    entries = copy.deepcopy(EXPECTED)
                    entries[1].update(directive_type=kind, structured_adjustment={"hours": [18], field: value})
                    self.assertEqual(self.validate(entries)[1]["directive_type"], "no_op")

    def test_note_mapping(self):
        self.assertEqual(self.validate(list(reversed(EXPECTED))), EXPECTED)
        result = self.validate([EXPECTED[0], EXPECTED[0], EXPECTED[2]])
        self.assertTrue(all(x["directive_type"] == "no_op" for x in result))
        for index in [-1, 3, True, "0", None]:
            with self.subTest(index=index):
                entries = copy.deepcopy(EXPECTED)
                entries[0]["note_index"] = index
                result = self.validate(entries)
                self.assertEqual(result[0]["directive_type"], "no_op")
                self.assertEqual(result[1:], EXPECTED[1:])

    def test_no_op_semantics(self):
        for update in [{"applies": True}, {"structured_adjustment": {}}, {"applies": 0}]:
            entries = copy.deepcopy(EXPECTED)
            entries[2].update(update)
            result = self.validate(entries)[2]
            self.assertIs(result["applies"], False)
            self.assertIsNone(result["structured_adjustment"])
            self.assertIn("fallback", result["explanation"])

    def test_invalid_caller_context(self):
        for notes, capacity in [([], 200), ([" "], 200), (NOTES, -1), (NOTES, float("inf")), (NOTES, True)]:
            with self.assertRaises(ValueError):
                interpret_notes(notes, capacity, completion=Mock())



if __name__ == "__main__":
    unittest.main()
