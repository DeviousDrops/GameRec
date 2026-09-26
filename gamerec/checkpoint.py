"""The Checkpoint: what has been ingested, and whether it can be trusted (D5/D12, ADR-0002).

Two jobs, and they pull in different directions. Resuming an interrupted fill wants the *latest*
position even if the process died mid-batch; restoring a backup wants a Checkpoint that is known to
match its snapshot. So the file carries a status, and the two readers want different things from it:

    resume   -- use it whatever the status says. Re-ingesting a batch is an upsert, so the cost of
                trusting a PENDING checkpoint is duplicate work, never wrong data.
    restore  -- only ever pair a snapshot with a COMPLETE checkpoint (ADR-0002).

The invariant is `checkpoint <= snapshot`: the pending write happens *before* Snapshot is called, so a
crash in between leaves a checkpoint that claims less than the snapshot holds. The reverse -- a
checkpoint claiming appids the snapshot never got -- would make a restored corpus disagree with the
record of what was ingested, which is the failure ADR-0002 exists to prevent.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PENDING = "pending"
COMPLETE = "complete"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Checkpoint:
    """State carried between ingest runs. `appids` is every appid the run has *seen*, filtered ones
    included -- that is what stops the next run paying for a fetch to reach the same verdict."""

    model_stamp: str
    template_version: int
    status: str = PENDING
    appids: set[int] = field(default_factory=set)
    # Advanced only once a batch is snapshotted (D15), so it can never claim more than is durable.
    watermark: str = ""
    updated_at: str = ""

    @classmethod
    def load(cls, path: Path) -> "Checkpoint | None":
        """None means no checkpoint, which is a first run rather than an error."""
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return cls(
            model_stamp=raw["model_stamp"],
            template_version=int(raw["template_version"]),
            status=raw.get("status", PENDING),
            appids=set(raw.get("appids", [])),
            watermark=raw.get("watermark", ""),
            updated_at=raw.get("updated_at", ""),
        )

    def save(self, path: Path, status: str) -> None:
        """Atomic: a torn checkpoint is indistinguishable from a wrong one, and both are worse than
        an old one. tmp + rename means a reader sees either the previous file or the new one."""
        self.status = status
        self.updated_at = _now()
        path = Path(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "model_stamp": self.model_stamp,
                    "template_version": self.template_version,
                    "status": self.status,
                    # Sorted so a diff between two checkpoints is readable by a human.
                    "appids": sorted(self.appids),
                    "watermark": self.watermark,
                    "updated_at": self.updated_at,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def mark_pending(self, path: Path, appids: set[int]) -> None:
        """Claim a batch *before* it is snapshotted, which is what keeps checkpoint <= snapshot."""
        self.appids |= appids
        self.save(path, PENDING)

    def mark_complete(self, path: Path) -> None:
        """Only after Snapshot returned success. This is the marker a restore looks for."""
        self.watermark = _now()
        self.save(path, COMPLETE)

    @property
    def restorable(self) -> bool:
        return self.status == COMPLETE
