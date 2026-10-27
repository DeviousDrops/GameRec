"""The games a user already owns, from library-service (D67).

Like narration, this never gates a response. If the service is slow, down, or answers anything but a
clean list, the recommendations go out unfiltered and the response says so -- recommending a game
someone owns is a lesser failure than recommending nothing.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

# 300 ms per phase: httpx applies a timeout to connecting and, separately, to reading, so the worst
# case is roughly double this. In-cluster the call is a millisecond or two, so 300 ms only ever
# matters when something is wrong, and then it bounds how much it can slow a query down.
TIMEOUT_SECONDS = 0.3

# One client for the life of the process. httpx.get() builds a new one per call, and building one
# loads the CA bundle -- measured at ~190 ms, most of the budget above spent before any request is
# sent. A shared client pays that once at import and keeps connections to the service open.
_http = httpx.Client()


def owned_appids(user: str, base_url: str, timeout: float = TIMEOUT_SECONDS,
                 client: httpx.Client | None = None) -> set[int] | None:
    """The appids `user` owns, or None when they could not be fetched.

    None and an empty set mean different things -- "unknown" against "owns nothing" -- and the
    caller reports which, so a filter that silently stopped working is visible from outside.
    """
    # quote(), with nothing left safe: a user id is one path segment, and a "/" or "?" in it must not
    # be able to turn into a different URL.
    url = f"{base_url.rstrip('/')}/api/v1/users/{quote(user, safe='')}/library/ids"
    client = client or _http
    try:
        response = client.get(url, params={"status": "OWNED"}, timeout=timeout)
        response.raise_for_status()
        return {int(appid) for appid in response.json()["appIds"]}
    except Exception as exc:  # any failure means "unfiltered", by design
        log.warning("library-service unavailable for user %r, recommending unfiltered: %s", user, exc)
        return None
