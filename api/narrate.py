"""Optional narration through Groq (D16).

Narration never gates a response. If the key is missing, the model is slow, or the call fails, the
recommendations are returned without it -- retrieval is the product, narration is a garnish (D6).
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Groq's catalogue is all reasoning models now, and they spend part of the completion budget thinking
# before any of it reaches the reply. With a 400-token cap and five games to describe, a long enough
# prompt spends the lot on reasoning and answers `content: ""` with `finish_reason: "length"` -- a
# 200, and nothing to show. Asking for low effort keeps it near 20 tokens instead of 140 (D62).
REASONING_EFFORT = "low"

PROMPT = """You recommend Steam games. For each game below, write one sentence explaining why it \
suits the request. Be specific and do not invent facts that are not in the description.

Request: {request}

Games:
{games}

Reply with one line per game, formatted "Name: reason"."""


def narrate(request: str, hits: list[dict], api_key: str, model: str, timeout: float = 8.0,
            client: httpx.Client | None = None) -> str | None:
    if not api_key:
        return None
    games = "\n".join(f"- {h['name']}: {h.get('short_description', '')}" for h in hits)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT.format(request=request, games=games)}],
        "temperature": 0.4,
        "max_tokens": 400,
        "reasoning_effort": REASONING_EFFORT,
    }
    post = client.post if client is not None else httpx.post
    try:
        response = post(GROQ_URL, headers={"Authorization": f"Bearer {api_key}"}, json=payload,
                        timeout=timeout)
        if response.status_code == 400 and "reasoning_effort" in response.text:
            # A model that has never heard of the parameter rejects the whole request, which would turn
            # "ask for less thinking" into "narration is off for this model", silently, because
            # narration fails open. Dropping it and asking again is cheaper than keeping a list of
            # which models accept it.
            log.info("%s does not accept reasoning_effort; retrying without it", model)
            del payload["reasoning_effort"]
            response = post(GROQ_URL, headers={"Authorization": f"Bearer {api_key}"}, json=payload,
                            timeout=timeout)
        response.raise_for_status()
        choice = response.json()["choices"][0]
        content = (choice["message"].get("content") or "").strip()
        if not content:
            # Not the same as a failure and not the same as narration: `""` would reach the response as
            # a narration field that exists and says nothing.
            log.warning("narration came back empty from %s (finish_reason %s); the reply budget most "
                        "likely went on reasoning", model, choice.get("finish_reason"))
            return None
        return content
    except Exception as exc:  # narration is best-effort by design
        log.warning("narration unavailable: %s", exc)
        return None
