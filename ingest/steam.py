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

log = logging.getLogger(__name__)

STEAMSPY_ALL = "https://steamspy.com/api.php"
APPDETAILS = "https://store.steampowered.com/api/appdetails"

# Steam tolerates roughly 200 requests per 5 minutes. Phase 2 makes this configurable and adds
# proper backoff; this is the floor that keeps the sample run from getting the dev box rate limited.
DEFAULT_DELAY = 1.6


@dataclass
class Popular:
    appid: int
    name: str
    review_count: int


def fetch_popular(limit: int, client: httpx.Client | None = None) -> list[Popular]:
    """Top games by owner count, in popularity order."""
    owns = client or httpx.Client(timeout=30)
    try:
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


def fetch_details(appid: int, client: httpx.Client, attempts: int = 4) -> dict | None:
    """Returns the appdetails `data` block, or None if Steam has nothing to say about this appid."""
    for attempt in range(attempts):
        try:
            response = client.get(
                APPDETAILS, params={"appids": appid, "l": "english", "cc": "us"}, timeout=30
            )
            if response.status_code == 429:
                raise httpx.HTTPError("rate limited")
            response.raise_for_status()
            body = response.json().get(str(appid)) or {}
            return body.get("data") if body.get("success") else None
        except (httpx.HTTPError, ValueError) as exc:
            if attempt == attempts - 1:
                log.warning("appid %s failed after %d attempts: %s", appid, attempts, exc)
                return None
            # Exponential backoff with jitter, so a burst of failures does not resynchronise.
            delay = (2**attempt) + random.uniform(0, 1)
            log.info("appid %s: %s; retrying in %.1fs", appid, exc, delay)
            time.sleep(delay)
    return None
