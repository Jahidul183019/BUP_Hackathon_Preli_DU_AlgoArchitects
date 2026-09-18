"""Groq primary + Gemini fallback, with bounded calls and sanitized diagnostics."""

import asyncio
import json
import logging
import time

import httpx

from .settings import read_settings

logger = logging.getLogger(__name__)
ATTEMPT_TIMEOUT_SECONDS = 8.0
TOTAL_TIMEOUT_SECONDS = 17.0
PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY", "GROQ_MODEL"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "GEMINI_API_KEY", "GEMINI_MODEL"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY", "OPENROUTER_MODEL"),
}


class ProviderUnavailable(RuntimeError):
    """No configured provider returned a usable interpretation."""


async def _request(name: str, settings: dict, system_prompt: str, user_json: str) -> str:
    endpoint, key_name, model_name = PROVIDERS[name]
    payload = {
        "model": settings[model_name],
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_json}],
        "temperature": 0,
        "max_tokens": 1024,
    }
    if name == "groq" and settings[model_name].startswith("openai/gpt-oss-"):
        payload["reasoning_effort"] = "low"
        payload["max_tokens"] = 2048
    if name == "gemini" and settings[model_name].startswith("gemini-3."):
        payload["reasoning_effort"] = "minimal"
    async with httpx.AsyncClient(timeout=TOTAL_TIMEOUT_SECONDS, follow_redirects=False) as client:
        response = await client.post(endpoint, json=payload, headers={"Authorization": f"Bearer {settings[key_name]}"})
        response.raise_for_status()
        choice = response.json()["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("Incomplete provider response")
    content = choice["message"].get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Missing provider content")
    return content


def _check_content(raw: str, user_json: str) -> None:
    # Validate before selecting a provider; valid JSON with invalid directives
    # must trigger the next provider too. Local import avoids a module cycle.
    from .llm_interpreter import DirectiveFallback, _reject_duplicate_keys, validate_directives
    context = json.loads(user_json)
    text = raw.strip() if isinstance(raw, str) else ""
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    parsed = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    checked = validate_directives(parsed, note_count=len(context["operator_notes"]), capacity_kwh=context["capacity_kwh"])
    if any(isinstance(entry, DirectiveFallback) for entry in checked):
        raise ValueError("Invalid provider interpretation")


async def _complete(system_prompt: str, user_json: str, settings: dict) -> str:
    deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
    order = settings["LLM_PROVIDER_ORDER"].split(",")
    for index, name in enumerate(order):
        if not settings[PROVIDERS[name][1]]:
            logger.warning("provider_skipped_missing_key provider=%s", name)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            # Wall-clock deadline cancels the HTTP task, including slow streams.
            raw = await asyncio.wait_for(
                _request(name, settings, system_prompt, user_json),
                timeout=remaining if index == len(order) - 1 else min(ATTEMPT_TIMEOUT_SECONDS, remaining),
            )
            _check_content(raw, user_json)
            logger.info("provider_succeeded provider=%s", name)
            return raw
        except TimeoutError:
            logger.warning("provider_timeout provider=%s", name)
        except httpx.HTTPStatusError as exc:
            logger.warning("provider_http_failure provider=%s status=%s", name, exc.response.status_code)
        except Exception:
            # Never log provider bodies, exception strings, keys or prompts.
            logger.warning("provider_response_failed provider=%s", name)
    raise ProviderUnavailable("No provider returned a valid interpretation") from None


def complete(system_prompt: str, user_json: str) -> str:
    """Try each configured provider once. Called from the API's worker thread."""
    settings = read_settings()
    return asyncio.run(_complete(system_prompt, user_json, settings))
