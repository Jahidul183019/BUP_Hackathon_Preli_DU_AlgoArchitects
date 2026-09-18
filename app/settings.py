"""Small literal .env reader: no shell execution or variable interpolation."""

import os
from pathlib import Path

DEFAULTS = {
    "GROQ_API_KEY": "",
    "GEMINI_API_KEY": "",
    "GROQ_MODEL": "llama-3.3-70b-versatile",
    "GEMINI_MODEL": "gemini-2.5-flash-lite",
    "LLM_PROVIDER_ORDER": "groq,gemini",
}
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def read_settings() -> dict[str, str]:
    values = dict(DEFAULTS)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if not separator or name.strip() not in DEFAULTS:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[name.strip()] = value
    for name in DEFAULTS:
        if name in os.environ:
            values[name] = os.environ[name].strip()
    order = [name.strip().lower() for name in values["LLM_PROVIDER_ORDER"].split(",")]
    if not order or any(name not in ("groq", "gemini") for name in order) or len(order) != len(set(order)):
        raise ValueError("Invalid provider order")
    values["LLM_PROVIDER_ORDER"] = ",".join(order)
    if not values["GROQ_MODEL"] or not values["GEMINI_MODEL"]:
        raise ValueError("Missing model configuration")
    return values
