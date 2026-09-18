"""Small OpenAI Chat Completions adapter; stdlib only, no SDK dependency."""

import json
import os
from urllib.request import Request, urlopen


def complete(system_prompt: str, user_json: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "").strip()
    if not api_key or not model:
        raise RuntimeError("Set OPENAI_API_KEY and OPENAI_MODEL")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_json},
        ],
    }
    request = Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    # Bounded single attempt: no hidden retries extending a hackathon request.
    with urlopen(request, timeout=20) as response:
        body = json.load(response)
    choice = body["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("Incomplete model response")
    content = choice["message"].get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Missing model text")
    return content
