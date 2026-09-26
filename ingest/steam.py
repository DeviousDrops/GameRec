"""Steam and SteamSpy access.

Note ISteamApps/GetAppList/v2 is gone -- it 404s with "Method 'GetAppList' not found in interface
'ISteamApps'". The replacement, IStoreService/GetAppList, needs a publisher key. SteamSpy is used
instead, and only for *ordering*: it supplies popularity so the corpus fills most-wanted-first, and
review counts so the review floor can be applied before spending a request on appdetails (D13).
Every fact that ends up in a Game Document comes from Steam itself.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

import httpx

from ingest.throttle import RATE_LIMIT_BACKOFF, RateLimiter

log = logging.getLogger(__name__)

STEAMSPY_ALL = "https://steamspy.com/api.php"
APPDETAILS = "https://store.steampowered.com/api/appdetails"

# Steam tolerates roughly 200 requests per 5 minutes. Pacing against that budget is the RateLimiter's
# job (D17); this delay survives only as the fallback for callers that pass no limiter.
DEFAULT_DELAY = 1.6


class FetchFailed(RuntimeError):
    """Steam never answered. Distinct from "answered, and has nothing for this appid" -- one is worth
    retrying on the next run, the other is a verdict, and recording the wrong one is how a real game
    ends up permanently labelled not_a_game."""


@dataclass
class Popular:
    appid: int
    name: str
    review_count: int


def fetch_popular(
    limit: int, client: httpx.Client | None = None, limiter: RateLimiter | None = None
) -> list[Popular]:
    """Top games by owner count, in popularity order."""
    owns = client or httpx.Client(timeout=30)
    try:
        if limiter is not None:
            limiter.acquire()
        page = owns.get(STEAMSPY_ALL, params={"request": "all", "page": 0}).json()
    finally:
        if client is None:
            owns.close()
    rows = [
        Popular(int(appid), row["name"], int(row.get("positive", 0)) + int(row.get("negative", 0)))
        for appid, row in page.items()
        if row.get("name")
    ]
    rows.sort(key=lambda r: r.review_count, reverse=True)
    return rows[:limit]


def fetch_details(
    appid: int,
    client: httpx.Client,
    attempts: int = 4,
    limiter: RateLimiter | None = None,
) -> dict | None:
    """Returns the appdetails `data` block, or None if Steam has nothing to say about this appid.

    Failures and rate limits get separate budgets. A 429 is not a failure -- it is Steam saying the
    request was fine but the budget is spent -- so spending the retry allowance on it would drop games
    from the corpus for no reason other than being throttled.
    """
    failures = throttles = 0
    while failures < attempts and throttles < attempts:
        if limiter is not None:
            limiter.acquire()
        try:
            response = client.get(
                APPDETAILS, params={"appids": appid, "l": "english", "cc": "us"}, timeout=30
            )
            if response.status_code == 429:
                throttles += 1
                if limiter is not None:
                    limiter.penalise(f"appid {appid}")
                else:
                    time.sleep(random.uniform(*RATE_LIMIT_BACKOFF))
                continue
            response.raise_for_status()
            return _unwrap(appid, response.json() or {})
        except (httpx.HTTPError, ValueError) as exc:
            failures += 1
            if failures >= attempts:
                raise FetchFailed(f"appid {appid} failed after {attempts} attempts: {exc}") from exc
            # Exponential backoff with jitter, so a burst of failures does not resynchronise.
            delay = (2 ** (failures - 1)) + random.uniform(0, 1)
            log.info("appid %s: %s; retrying in %.1fs", appid, exc, delay)
            time.sleep(delay)
    raise FetchFailed(f"appid {appid}: gave up after {throttles} rate limits")


def _unwrap(appid: int, body: dict) -> dict | None:
    """Pull the `data` block out of an appdetails response, or None if Steam has nothing for it.

    The envelope is not keyed by the appid that was asked for. Requesting 105600 (Terraria) comes back
    under "1323320"; the id is stable per app but unrelated to the request, and indexing by the
    requested appid therefore missed the payload entirely and read as "not a game" -- which is how
    Terraria, ELDEN RING and Counter-Strike all got filtered out of a corpus they belong in.

    `data.steam_appid` is the authoritative one, so it is what gets checked. A genuine miss keys by
    the requested appid with `success: false`.
    """
    entry = body.get(str(appid))
    if entry is None and len(body) == 1:
        entry = next(iter(body.values()))
    if not isinstance(entry, dict) or not entry.get("success"):
        return None

    data = entry.get("data") or {}
    returned = data.get("steam_appid")
    if returned is not None and int(returned) != appid:
        # Storing one game's document under another's appid would be wrong in a way nothing
        # downstream could detect, so this is dropped rather than guessed at.
        log.warning("appid %s: appdetails answered with steam_appid %s; ignoring", appid, returned)
        return None
    return data
