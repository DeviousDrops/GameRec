"""Narration against the shapes Groq actually answers with (D62).

This module had no tests, which is how it came to be calling a decommissioned model in production: the
endpoint answered 404, `narrate` caught it as designed, and the only evidence was one `log.warning` in
a pod nobody was reading. Narration failing open is right (D6) -- it also means every bug in here is
silent, so the assertions are about what happens instead of about it not raising.
"""

from __future__ import annotations

import httpx
import pytest

from api import narrate as module


def groq(status: int = 200, content: str = "Game: because it is calm.", finish: str = "stop",
         body: dict | None = None) -> tuple[httpx.Client, list[dict]]:
    """A client standing in for Groq, and the list of payloads it was sent."""
    sent: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        import json
        sent.append(json.loads(request.content))
        if body is not None:
            return httpx.Response(status, json=body)
        return httpx.Response(status, json={"choices": [{"finish_reason": finish,
                                                         "message": {"role": "assistant",
                                                                     "content": content}}]})

    return httpx.Client(transport=httpx.MockTransport(handle)), sent


HITS = [{"name": "Stardew Valley", "short_description": "farming"}]


def test_a_reply_is_returned_stripped():
    client, _ = groq(content="  Stardew Valley: it is calm.  ")
    assert module.narrate("cozy", HITS, "k", "m", client=client) == "Stardew Valley: it is calm."


def test_low_reasoning_effort_is_asked_for():
    """The whole reason narration came back empty: reasoning tokens come out of the same budget as the
    reply, so the request has to say how much of it to spend thinking."""
    client, sent = groq()
    module.narrate("cozy", HITS, "k", "m", client=client)
    assert sent[0]["reasoning_effort"] == "low"


def test_an_empty_reply_is_not_narration():
    """A 200 with no content is what a model that reasoned through its whole budget returns. Passing the
    empty string on would put a narration field in the response that exists and says nothing."""
    client, _ = groq(content="", finish="length")
    assert module.narrate("cozy", HITS, "k", "m", client=client) is None


def test_a_null_content_is_not_narration():
    client, _ = groq(body={"choices": [{"finish_reason": "stop", "message": {"content": None}}]})
    assert module.narrate("cozy", HITS, "k", "m", client=client) is None


def test_a_model_that_rejects_reasoning_effort_is_asked_again_without_it():
    """Rejecting an unknown parameter fails the whole call, and narration failing open would make that
    look like a model with nothing to say."""
    replies = iter([httpx.Response(400, json={"error": {"message": "unknown parameter reasoning_effort"}}),
                    httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                           "message": {"content": "fine"}}]})])
    sent: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        import json
        sent.append(json.loads(request.content))
        return next(replies)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    assert module.narrate("cozy", HITS, "k", "m", client=client) == "fine"
    assert "reasoning_effort" in sent[0] and "reasoning_effort" not in sent[1]


def test_a_400_about_anything_else_is_not_retried():
    """Only the parameter is worth dropping. Retrying a rejected model or a malformed prompt is one more
    request for the same error."""
    client, sent = groq(status=400, body={"error": {"message": "model_not_found"}})
    assert module.narrate("cozy", HITS, "k", "m", client=client) is None
    assert len(sent) == 1


def test_no_key_means_no_request_at_all():
    client, sent = groq()
    assert module.narrate("cozy", HITS, "", "m", client=client) is None
    assert sent == []


@pytest.mark.parametrize("status", [404, 429, 500])
def test_every_failure_is_swallowed_and_logged(status, caplog):
    """The property the rest of the API depends on: retrieval is the product, narration is a garnish."""
    client, _ = groq(status=status, body={"error": {"message": "nope"}})
    with caplog.at_level("WARNING"):
        assert module.narrate("cozy", HITS, "k", "m", client=client) is None
    assert "narration unavailable" in caplog.text
