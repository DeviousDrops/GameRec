"""The Ingest Lease: one ingest at a time, enforced rather than assumed (D17, D20).

`concurrencyPolicy: Forbid` stops the CronJob from overlapping itself. It does nothing about the run
somebody starts by hand at 03:20 to backfill something, and pacing is per-process -- two runs each
stay under the rate limit and jointly sail past it, which costs a 429 and a stalled hour.

So a run holds a lease object in R2 for as long as it works:

    acquire  PutObject with If-None-Match: * -- atomic create-if-absent, so exactly one run wins
    renew    rewrite the object with a later expiry, at every batch boundary
    release  delete it, but only if the token inside is still ours
    steal    an expired lease is takeable, because a pod that dies cannot release anything

The token is what makes release safe. Without it, a run that lost its lease to expiry and then
finished would delete the lease its successor is holding, and the next two runs would overlap.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass

from gamerec.objectstore import ObjectStore

log = logging.getLogger("lease")

LEASE_KEY = "locks/ingest.json"


class LeaseHeld(RuntimeError):
    """Someone else is ingesting. Not an error in the run's own logic -- it should exit, not retry."""


@dataclass
class Lease:
    store: ObjectStore
    key: str
    owner: str
    token: str
    ttl: float
    expires_at: float

    def _body(self) -> bytes:
        return json.dumps({"owner": self.owner, "token": self.token,
                           "expires_at": self.expires_at}).encode()

    def renew(self, now: float | None = None) -> bool:
        """Push the expiry out. False if the lease is no longer ours, and the caller should stop.

        Read-then-write rather than a conditional write, because the condition that matters is "the
        token is still mine" and S3 has no compare-and-swap on content. The window is a whole run
        long but harmless: the only writer that could be in it is one that already stole the lease,
        and this returns False in that case.
        """
        holder = _read(self.store, self.key)
        if holder is not None and holder.get("token") != self.token:
            log.warning("lease %s is now held by %s; this run no longer owns it",
                        self.key, holder.get("owner"))
            return False
        self.expires_at = (now or time.time()) + self.ttl
        self.store.put(self.key, self._body())
        return True

    def release(self) -> None:
        """Delete the lease if it is still ours, and say so plainly if it is not."""
        holder = _read(self.store, self.key)
        if holder is None:
            return
        if holder.get("token") != self.token:
            log.warning("not releasing lease %s: it belongs to %s now", self.key,
                        holder.get("owner"))
            return
        self.store.delete(self.key)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _read(store: ObjectStore, key: str) -> dict | None:
    raw = store.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # A lease nobody can parse is a lease nobody can release, so it is treated as expired rather
        # than as a permanent block on all ingest.
        log.warning("lease %s is unreadable; treating it as expired", key)
        return {"owner": "unknown", "token": "", "expires_at": 0}


def default_owner() -> str:
    """Whatever identifies this run to a human reading the lease at 3am."""
    return f"{os.environ.get('HOSTNAME') or socket.gethostname()}/{os.getpid()}"


def acquire(
    store: ObjectStore,
    owner: str | None = None,
    ttl: float = 1800.0,
    key: str = LEASE_KEY,
    now: float | None = None,
) -> Lease:
    """Take the lease or raise LeaseHeld. Never blocks -- a queued ingest is not wanted, a skip is."""
    moment = now or time.time()
    lease = Lease(store=store, key=key, owner=owner or default_owner(), token=uuid.uuid4().hex,
                  ttl=ttl, expires_at=moment + ttl)
    if store.put_if_absent(key, lease._body()):
        return lease

    holder = _read(store, key)
    if holder is None:
        # It existed a moment ago and does not now: the holder released between the two calls.
        if store.put_if_absent(key, lease._body()):
            return lease
        holder = _read(store, key) or {}

    expires_at = float(holder.get("expires_at", 0))
    if expires_at > moment:
        raise LeaseHeld(
            f"ingest lease held by {holder.get('owner')} for another "
            f"{int(expires_at - moment)}s; exiting rather than running two ingests"
        )

    # Expired. A pod that was OOM-killed cannot release its lease, so an expiry has to be takeable or
    # one bad night stops every ingest until someone notices.
    log.warning("stealing expired lease from %s (%ds past expiry)",
                holder.get("owner"), int(moment - expires_at))
    store.delete(key)
    if not store.put_if_absent(key, lease._body()):
        raise LeaseHeld("lost the race to take over an expired ingest lease; exiting")
    return lease
