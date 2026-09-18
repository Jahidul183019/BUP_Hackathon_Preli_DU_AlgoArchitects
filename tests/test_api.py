import asyncio
import copy
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.final_validator import final_validator
from app.llm_interpreter import interpret_notes, validate_directives
from app.schedule_optimizer import optimize_schedule


CASES = json.loads((Path(__file__).parent / "fixtures/public_optimizer_cases.json").read_text())["cases"]
SECRET = "DO_NOT_LEAK_secret_key_or_private_exception"


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.case = CASES[0]
        self.request = copy.deepcopy(self.case["input"])
        self.raw = json.dumps(self.case["expected_output"]["directive_interpretation"])

    def test_health_and_all_public_cases_through_real_pipeline(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        for case in CASES:
            with self.subTest(case=case["id"]), patch("app.llm_provider.complete", return_value=json.dumps(case["expected_output"]["directive_interpretation"])):
                response = self.client.post("/optimize-energy", json=case["input"])
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual(set(result), {"scenario_id", "directive_interpretation", "hourly_plan", "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh", "plan_summary"})
                self.assertEqual(result["scenario_id"], case["input"]["scenario_id"])
                self.assertEqual(result["directive_interpretation"], case["expected_output"]["directive_interpretation"])
                self.assertAlmostEqual(result["total_cost_bdt"], case["expected_output"]["total_cost_bdt"], delta=0.01)
                self.assertEqual(final_validator(result["hourly_plan"], case["input"]["hours"], case["input"]["battery"], result["directive_interpretation"]), [])

    def test_pipeline_order_and_capacity_context(self):
        calls = []
        def wrap(name, function):
            def called(*args, **kwargs):
                calls.append(name)
                return function(*args, **kwargs)
            return called
        with patch("app.llm_provider.complete", return_value=self.raw), \
             patch.object(main, "interpret_notes", side_effect=wrap("interpret", interpret_notes)) as interpreter, \
             patch.object(main, "validate_directives", side_effect=wrap("guardrails", validate_directives)), \
             patch.object(main, "optimize_schedule", side_effect=wrap("optimize", optimize_schedule)), \
             patch.object(main, "final_validator", side_effect=wrap("replay", final_validator)):
            self.assertEqual(self.client.post("/optimize-energy", json=self.request).status_code, 200)
        self.assertEqual(calls, ["interpret", "guardrails", "optimize", "replay"])
        self.assertEqual(interpreter.call_args.kwargs["capacity_kwh"], self.request["battery"]["capacity_kwh"])

    def test_invalid_requests_are_400_without_echoing_input(self):
        with patch.object(main, "interpret_notes") as interpreter:
            invalid = [{}, {SECRET: SECRET}, {**self.request, "operator_notes": []},
                       {**self.request, "hours": self.request["hours"][:-1]}]
            for request in invalid:
                response = self.client.post("/optimize-energy", json=request)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn(SECRET, response.text)
            response = self.client.post("/optimize-energy", content='{"' + SECRET, headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 400)
            self.assertNotIn(SECRET, response.text)
            interpreter.assert_not_called()

    def test_all_pipeline_exception_paths_are_sanitized(self):
        for stage in ["interpret_notes", "validate_directives", "optimize_schedule", "final_validator"]:
            with self.subTest(stage=stage), patch("app.llm_provider.complete", return_value=self.raw), \
                 patch.object(main, stage, side_effect=RuntimeError(SECRET)), \
                 self.assertLogs("app.main", level="ERROR") as captured:
                response = self.client.post("/optimize-energy", json=self.request)
            self.assertEqual(response.status_code, 500)
            self.assertNotIn(SECRET, response.text + " ".join(captured.output))
            self.assertTrue(all(record.exc_info is None for record in captured.records))

    def test_bad_provider_response_or_failure_falls_back_to_safe_schedule(self):
        for kwargs in [{"return_value": "invalid JSON " + SECRET}, {"side_effect": RuntimeError(SECRET)},
                       {"return_value": "[]"}]:
            with self.subTest(kwargs=kwargs), patch("app.llm_provider.complete", **kwargs), \
                 self.assertLogs("app", level="WARNING") as captured:
                response = self.client.post("/optimize-energy", json=self.request)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(all(item["directive_type"] == "no_op" for item in response.json()["directive_interpretation"]))
            self.assertNotIn(SECRET, response.text + " ".join(captured.output))
            self.assertTrue(all(record.exc_info is None for record in captured.records))

    def test_failed_final_replay_returns_500_with_safe_event(self):
        with patch("app.llm_provider.complete", return_value=self.raw), \
             patch.object(main, "final_validator", return_value=[SECRET]), \
             self.assertLogs("app.main", level="ERROR") as captured:
            response = self.client.post("/optimize-energy", json=self.request)
        self.assertEqual(response.status_code, 500)
        self.assertIn("final_validation_failed", " ".join(captured.output))
        self.assertNotIn(SECRET, response.text + " ".join(captured.output))

    def test_response_schema_failure_is_sanitized(self):
        with patch("app.llm_provider.complete", return_value=self.raw), \
             patch.object(main, "OptimizeResponse", side_effect=ValueError(SECRET)), \
             self.assertLogs("app.main", level="ERROR") as captured:
            response = self.client.post("/optimize-energy", json=self.request)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(SECRET, response.text + " ".join(captured.output))

    def test_timeout_returns_promptly_and_health_stays_responsive(self):
        release = threading.Event()
        finished = threading.Event()
        def slow(*args, **kwargs):
            try:
                release.wait(2)
                raise RuntimeError(SECRET)
            finally:
                finished.set()
        try:
            with patch.object(main, "REQUEST_TIMEOUT_SECONDS", 0.03), patch.object(main, "interpret_notes", side_effect=slow), \
                 self.assertLogs("app.main", level="ERROR") as captured:
                started = time.monotonic()
                response = self.client.post("/optimize-energy", json=self.request)
                elapsed = time.monotonic() - started
                self.assertEqual(response.status_code, 504)
                self.assertLess(elapsed, 0.5)
                self.assertEqual(self.client.get("/health").status_code, 200)
            self.assertNotIn(SECRET, response.text + " ".join(captured.output))
        finally:
            release.set()
            self.assertTrue(finished.wait(2))

    def test_busy_workers_fail_fast(self):
        with patch.object(main, "_slots") as slots:
            slots.acquire.return_value = False
            response = self.client.post("/optimize-energy", json=self.request)
            self.assertEqual(response.status_code, 503)
            slots.release.assert_not_called()

    def test_deadline_includes_request_body_read(self):
        async def run():
            sent = []
            async def downstream(scope, receive, send):
                await receive()
            async def receive():
                await asyncio.sleep(1)
            async def send(message):
                sent.append(message)
            with patch.object(main, "REQUEST_TIMEOUT_SECONDS", 0.01):
                await main.RequestBoundary(downstream)({"type": "http"}, receive, send)
            self.assertEqual(sent[0]["status"], 504)
        with self.assertLogs("app.main", level="ERROR"):
            asyncio.run(run())

    def test_send_failure_does_not_escape_with_traceback(self):
        async def run():
            async def downstream(scope, receive, send):
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"{}"})
            async def receive():
                return {"type": "http.request", "body": b""}
            async def send(message):
                raise RuntimeError(SECRET)
            await main.RequestBoundary(downstream)({"type": "http"}, receive, send)
        with self.assertLogs("app.main", level="ERROR") as captured:
            asyncio.run(run())
        self.assertNotIn(SECRET, " ".join(captured.output))
        self.assertTrue(all(record.exc_info is None for record in captured.records))


if __name__ == "__main__":
    unittest.main()
