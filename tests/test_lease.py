"""The Ingest Lease (D17, D20).

What these tests can and cannot prove is worth stating. They prove the lease *protocol*: who wins, who
exits, when an expiry is takeable, and that a release cannot delete somebody else's lease. They cannot
prove R2 implements `If-None-Match: *` correctly, because MemoryStore is single-threaded and agrees
with itself by construction -- D20 requires that half to be checked against real R2.
"""

from __future__ import annotations

import json

import pytest

from gamerec.lease import LeaseHeld, acquire
from gamerec.objectstore import MemoryStore


def test_the_first_run_takes_the_lease():
    store = MemoryStore()
    lease = acquire(store, owner="first", ttl=60, now=1000.0)

    assert json.loads(store.get(lease.key))["owner"] == "first"


def test_a_second_run_exits_rather_than_queueing():
    """A queued ingest is not wanted. Two runs pacing themselves correctly still exceed Steam's
    budget jointly, so the right answer to a busy lease is to leave."""
    store = MemoryStore()
    acquire(store, owner="first", ttl=60, now=1000.0)

    with pytest.raises(LeaseHeld) as raised:
        acquire(store, owner="second", ttl=60, now=1030.0)

    assert "first" in str(raised.value)


def test_an_expired_lease_is_takeable():
    """A pod that is OOM-killed cannot release anything. Without a steal, one bad night stops every
    ingest until a human notices."""
    store = MemoryStore()
    acquire(store, owner="dead", ttl=60, now=1000.0)

    lease = acquire(store, owner="live", ttl=60, now=1100.0)

    assert json.loads(store.get(lease.key))["owner"] == "live"


def test_releasing_after_being_stolen_from_leaves_the_new_lease_alone():
    """The bug the token prevents: the original holder finishes late, deletes what it thinks is its
    own lease, and the next two runs overlap."""
    store = MemoryStore()
    slow = acquire(store, owner="slow", ttl=60, now=1000.0)
    thief = acquire(store, owner="thief", ttl=60, now=1100.0)

    slow.release()

    assert json.loads(store.get(thief.key))["owner"] == "thief"


def test_renewing_extends_the_expiry_so_a_long_run_keeps_its_lease():
    store = MemoryStore()
    lease = acquire(store, owner="filler", ttl=60, now=1000.0)

    assert lease.renew(now=1050.0) is True
    assert json.loads(store.get(lease.key))["expires_at"] == 1110.0
    # ...and the lease is not stealable at a moment that would have worked without the renewal.
    with pytest.raises(LeaseHeld):
        acquire(store, owner="other", ttl=60, now=1070.0)


def test_renewing_a_lost_lease_reports_the_loss():
    """The signal a long fill needs: it has been running without the lease it thinks it holds, so it
    must stop rather than keep spending Steam requests beside another run."""
    store = MemoryStore()
    lease = acquire(store, owner="filler", ttl=60, now=1000.0)
    acquire(store, owner="thief", ttl=60, now=1100.0)

    assert lease.renew(now=1110.0) is False


def test_releasing_frees_it_for_the_next_run():
    store = MemoryStore()
    with acquire(store, owner="first", ttl=60, now=1000.0) as lease:
        assert store.get(lease.key) is not None

    assert store.get(lease.key) is None
    acquire(store, owner="second", ttl=60, now=1001.0)


def test_an_unreadable_lease_does_not_block_ingest_forever():
    """A truncated write should cost one skipped run, not every future one."""
    store = MemoryStore()
    store.put("locks/ingest.json", b"{not json")

    lease = acquire(store, owner="recovering", ttl=60, now=1000.0)

    assert json.loads(store.get(lease.key))["owner"] == "recovering"


def test_a_conflicting_conditional_write_is_not_an_error():
    """R2 answers 412 and AWS answers 409 for the same situation; both mean somebody else won (D20).
    Anything else is a real failure and must not be swallowed."""
    from botocore.exceptions import ClientError

    from gamerec.objectstore import R2Store

    class Client:
        def __init__(self, status: int) -> None:
            self.status = status

        def put_object(self, **kwargs):
            assert kwargs["IfNoneMatch"] == "*"
            raise ClientError({"ResponseMetadata": {"HTTPStatusCode": self.status}}, "PutObject")

    store = R2Store.__new__(R2Store)
    store.bucket = "b"
    for status in (409, 412):
        store._client = Client(status)
        assert store.put_if_absent("k", b"v") is False

    store._client = Client(500)
    with pytest.raises(ClientError):
        store.put_if_absent("k", b"v")
