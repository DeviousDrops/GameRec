"""The /health parser inside deploy/vm/smoke.sh, against every shape it will meet (D59).

This file exists because `smoke.sh` gated a deploy on a `KeyError`. D56 moved `wal_healthy` to the top
level of `/health`, the script was updated to read it there, and the first rollout it ran against was
the one *deploying* D56 -- so the service answering was still the version before it, the key was not
there, and the script failed the deploy it was verifying.

The lesson is about when a smoke test runs, not about a missing `.get`: it runs at the moment the two
versions differ, so it is the one script in the repo that has to understand both of them. Reading the
block out of the shell script rather than reimplementing it is the point -- a copy here would pass
while the thing that runs in production stayed broken.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SMOKE = Path(__file__).resolve().parent.parent / "deploy" / "vm" / "smoke.sh"

BEFORE_D56 = {
    "status": "ok", "corpus_size": 984, "capacity": 200000, "dims": 384,
    "model_stamp": "BAAI/bge-small-en-v1.5/384", "template_version": 1, "name_index_size": 1000,
    "mindb": {"kernel": "avx2", "goarch": "amd64", "fast_int8": True,
              "wal_enabled": True, "wal_healthy": True},
}
AFTER_D56 = {
    "status": "ok", "corpus_size": 984, "dims": 384, "name_index_size": 1000, "wal_healthy": True,
}
WITH_DETAIL = BEFORE_D56 | {"wal_healthy": True}


def parser() -> str:
    """The python between `python3 - "${HEALTH}" <<'PY'` and its terminator, verbatim."""
    lines = SMOKE.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("python3 - ")) + 1
    end = next(i for i, line in enumerate(lines[start:], start=start) if line == "PY")
    return "\n".join(lines[start:end])


def check(health: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", parser(), json.dumps(health)],
                          capture_output=True, text=True)


@pytest.mark.parametrize("health, label", [(BEFORE_D56, "before D56"), (AFTER_D56, "after D56"),
                                           (WITH_DETAIL, "with HEALTH_DETAIL on")])
def test_a_healthy_service_passes_whichever_shape_it_answers_in(health, label):
    result = check(health)
    assert result.returncode == 0, f"{label}: {result.stderr}"
    assert "984" in result.stdout


def test_the_old_shape_is_not_read_as_a_missing_wal():
    """The failure that prompted this file: no top-level wal_healthy is not the same as a broken one,
    and confusing the two fails a deploy on a WAL that was fine."""
    result = check(BEFORE_D56)
    assert "unhealthy WAL" not in result.stderr


def test_an_empty_index_still_fails_the_deploy():
    """The check the whole script exists for: an empty index answers every query with nothing and
    looks healthy doing it."""
    result = check(AFTER_D56 | {"corpus_size": 0})
    assert result.returncode == 1 and "index is empty" in result.stderr


def test_a_dimension_mismatch_still_fails_the_deploy():
    result = check(AFTER_D56 | {"dims": 768})
    assert result.returncode == 1 and "768-dimensional" in result.stderr


@pytest.mark.parametrize("health", [AFTER_D56 | {"wal_healthy": False},
                                    BEFORE_D56 | {"mindb": BEFORE_D56["mindb"] | {"wal_healthy": False}}])
def test_a_broken_wal_fails_the_deploy_in_either_shape(health):
    result = check(health)
    assert result.returncode == 1 and "unhealthy WAL" in result.stderr


def test_no_wal_at_all_is_not_a_broken_wal():
    """MinDB built without a WAL reports wal_enabled false. Failing every deploy of that build would
    be this script inventing a problem."""
    without = BEFORE_D56 | {"mindb": BEFORE_D56["mindb"] | {"wal_enabled": False,
                                                            "wal_healthy": False}}
    assert check(without).returncode == 0
    assert check(AFTER_D56 | {"wal_healthy": None}).returncode == 0
