"""Optional narration through Groq (D16).

Narration never gates a response. If the key is missing, the model is slow, or the call fails, the
recommendations are returned without it -- retrieval is the product, narration is a garnish (D6).
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

PROMPT = """You recommend Steam games. For each game below, write one sentence explaining why it \
suits the request. Be specific and do not invent facts that are not in the description.

Request: {request}

Games:
{games}

Reply with one line per game, formatted "Name: reason"."""


def narrate(request: str, hits: list[dict], api_key: str, model: str, timeout: float = 8.0) -> str | None:
    if not api_key:
        return None
    games = "\n".join(f"- {h['name']}: {h.get('short_description', '')}" for h in hits)
    try:
        response = httpx.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": PROMPT.format(request=request, games=games)}],
                "temperature": 0.4,
                "max_tokens": 400,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # narration is best-effort by design
        log.warning("narration unavailable: %s", exc)
        return None
