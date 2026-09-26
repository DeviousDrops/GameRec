"""The Game Document: what actually gets embedded, and what rides along as payload."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# v1 is metadata only. v2 adds review phrases (D22); a mixed-version corpus is expected, and
# /recommend reports template_version per result so the mix is visible rather than hidden.
TEMPLATE_VERSION = 1


@dataclass
class GameDocument:
    appid: int
    name: str
    short_description: str = ""
    genres: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    developers: list[str] = field(default_factory=list)
    release_year: str = ""
    review_count: int = 0

    @property
    def vector_id(self) -> str:
        # Keyed by appid, which is what makes re-ingest an upsert rather than a duplicate.
        return f"appid:{self.appid}"

    def render(self) -> str:
        """The text that gets embedded. Deliberately plain: BGE does better on prose than on
        key: value lists, so genres and categories are folded into a sentence."""
        parts = [self.name]
        if self.release_year:
            parts.append(f"Released {self.release_year}.")
        if self.genres:
            parts.append(f"A {', '.join(self.genres).lower()} game.")
        if self.short_description:
            parts.append(self.short_description)
        if self.categories:
            parts.append(f"Features: {', '.join(self.categories)}.")
        return " ".join(parts)

    def payload(self, model_stamp: str) -> bytes:
        return json.dumps(
            {
                "appid": self.appid,
                "name": self.name,
                "short_description": self.short_description,
                "genres": self.genres,
                "release_year": self.release_year,
                "review_count": self.review_count,
                "template_version": TEMPLATE_VERSION,
                "model_stamp": model_stamp,
            },
            separators=(",", ":"),
        ).encode()

    def to_jsonl(self) -> str:
        return json.dumps(self.__dict__, separators=(",", ":"))

    @classmethod
    def from_jsonl(cls, line: str) -> "GameDocument":
        return cls(**json.loads(line))
