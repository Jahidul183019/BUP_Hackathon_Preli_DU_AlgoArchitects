import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app import llm_provider as provider
from app import settings

CONFIG = {**settings.DEFAULTS, "GROQ_API_KEY": "private-groq-key", "GEMINI_API_KEY": "private-gemini-key"}
CONTEXT = json.dumps({"capacity_kwh": 200, "operator_notes": ["The cafeteria menu changes tomorrow."]})
VALID = json.dumps([{"note_index": 0, "applies": False, "directive_type": "no_op", "structured_adjustment": None, "explanation": "Unrelated menu change."}])


class ProviderTests(unittest.TestCase):
    def call(self):
        with patch.object(provider, "read_settings", return_value=CONFIG):
            return provider.complete("system", CONTEXT)

    def test_primary_success_does_not_call_fallback(self):
        with patch.object(provider, "_request", new=AsyncMock(return_value=VALID)) as request:
            self.assertEqual(self.call(), VALID)
            self.assertEqual([c.args[0] for c in request.call_args_list], ["groq"])

    def test_network_and_invalid_content_trigger_fallback(self):
        for first in [RuntimeError("private-groq-key"), "not json", "[]", VALID.replace('"applies": false', '"applies": true')]:
            with self.subTest(first=type(first).__name__), patch.object(provider, "_request", new=AsyncMock(side_effect=[first, VALID])) as request, self.assertLogs("app", level="WARNING") as captured:
                self.assertEqual(self.call(), VALID)
                self.assertEqual([c.args[0] for c in request.call_args_list], ["groq", "gemini"])
            self.assertNotIn("private-groq-key", " ".join(captured.output))
            self.assertTrue(all(r.exc_info is None for r in captured.records))

    def test_http_429_and_401_trigger_fallback_without_body_logs(self):
        for status in [429, 401, 503]:
            request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
            response = httpx.Response(status, request=request, text="private-groq-key")
            error = httpx.HTTPStatusError("private-groq-key", request=request, response=response)
            with patch.object(provider, "_request", new=AsyncMock(side_effect=[error, VALID])), self.assertLogs("app", level="WARNING") as captured:
                self.assertEqual(self.call(), VALID)
            self.assertNotIn("private-groq-key", " ".join(captured.output))

    def test_primary_timeout_is_cancelled_and_fallback_runs(self):
        cancelled = []
        async def request(name, *args):
            if name == "groq":
                try:
                    await asyncio.sleep(1)
                finally:
                    cancelled.append(name)
            return VALID
        with patch.object(provider, "_request", side_effect=request), patch.object(provider, "ATTEMPT_TIMEOUT_SECONDS", 0.02), self.assertLogs("app", level="WARNING"):
            started = time.monotonic()
            self.assertEqual(self.call(), VALID)
            self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(cancelled, ["groq"])

    def test_shared_budget_limits_both_attempts(self):
        async def slow(*args):
            await asyncio.sleep(1)
        with patch.object(provider, "_request", side_effect=slow), patch.object(provider, "ATTEMPT_TIMEOUT_SECONDS", 0.04), patch.object(provider, "TOTAL_TIMEOUT_SECONDS", 0.05), self.assertLogs("app", level="WARNING"):
            started = time.monotonic()
            with self.assertRaises(provider.ProviderUnavailable):
                self.call()
            self.assertLess(time.monotonic() - started, 0.2)

    def test_no_keys_makes_no_network_calls(self):
        with patch.object(provider, "read_settings", return_value=settings.DEFAULTS), patch.object(provider, "_request", new=AsyncMock()) as request, self.assertLogs("app", level="WARNING"):
            with self.assertRaises(provider.ProviderUnavailable):
                provider.complete("system", CONTEXT)
            request.assert_not_called()

    def test_actual_http_contract_both_providers(self):
        observed = []
        def handler(request):
            observed.append(request)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": VALID}}]})
        real_client = httpx.AsyncClient
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        with patch.object(provider.httpx, "AsyncClient", side_effect=client):
            for name in ["groq", "gemini"]:
                self.assertEqual(asyncio.run(provider._request(name, CONFIG, "system", CONTEXT)), VALID)
        self.assertEqual(observed[0].url.host, "api.groq.com")
        self.assertEqual(observed[1].url.host, "generativelanguage.googleapis.com")
        for request, name in zip(observed, ["groq", "gemini"]):
            self.assertEqual(request.headers["authorization"], "Bearer " + CONFIG[name.upper() + "_API_KEY"])
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], CONFIG[name.upper() + "_MODEL"])
            if name == "groq":
                self.assertEqual(payload["reasoning_effort"], "low")
                self.assertEqual(payload["max_tokens"], 2048)
            else:
                self.assertEqual(payload["reasoning_effort"], "minimal")

    def test_literal_config_and_environment_override(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            path.write_text('GROQ_API_KEY=file-key\nGEMINI_API_KEY="file-gemini"\nLLM_PROVIDER_ORDER=groq,gemini\n')
            with patch.object(settings, "ENV_FILE", path), patch.dict(os.environ, {"GROQ_API_KEY": "environment-key"}, clear=True):
                value = settings.read_settings()
            self.assertEqual(value["GROQ_API_KEY"], "environment-key")
            self.assertEqual(value["GEMINI_API_KEY"], "file-gemini")


if __name__ == "__main__":
    unittest.main()
