"""Run locally in your terminal; key input is hidden and never printed."""

import getpass
import os
import sys

from app.settings import DEFAULTS, ENV_FILE


def main():
    if not sys.stdin.isatty():
        raise SystemExit("Run this command in an interactive terminal so key input stays hidden.")
    if ENV_FILE.exists():
        raise SystemExit(".env already exists; edit it locally to change keys. Nothing was overwritten.")
    values = dict(DEFAULTS)
    for name, label in [("GROQ_API_KEY", "Groq"), ("GEMINI_API_KEY", "Gemini")]:
        value = getpass.getpass(f"{label} API key (hidden): ").strip()
        if not value or any(c.isspace() or c in "\"'" for c in value):
            raise SystemExit("A non-empty key without whitespace or quotes is required; nothing was saved.")
        values[name] = value
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(ENV_FILE, flags, 0o600), "w") as stream:
        for name, value in values.items():
            stream.write(f"{name}={value}\n")
    print("Saved both keys to private, git-ignored .env. Keys were not printed.")
    print("Provider order: Groq -> Gemini. You can now run the API and live checks.")


if __name__ == "__main__":
    main()
