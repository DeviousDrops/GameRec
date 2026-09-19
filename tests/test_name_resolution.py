"""Seed resolution has to be pickier than a fuzzy scorer is by default.

The value of the name index is that a miss explains itself. That is worth nothing if a miss silently
becomes a hit on a different game, which is exactly what fuzzy matching does to sequel numbers.
"""

from __future__ import annotations

import pytest

from gamerec.names import (
    FILTERED_LOW_REVIEWS, IN_CORPUS, NOT_A_GAME, PENDING_INGEST, NameEntry, NameIndex,
)

ENTRIES = [
    NameEntry(70, "Half-Life", IN_CORPUS),
    NameEntry(220, "Half-Life 2", IN_CORPUS),
    NameEntry(400, "Portal", IN_CORPUS),
    NameEntry(620, "Portal 2", IN_CORPUS),
    NameEntry(335300, "DARK SOULS II: Scholar of the First Sin", IN_CORPUS),
    NameEntry(1245620, "ELDEN RING", PENDING_INGEST),
    NameEntry(323190, "Frostpunk", FILTERED_LOW_REVIEWS),
    NameEntry(323180, "Frostpunk Soundtrack", NOT_A_GAME),
]


@pytest.fixture
def index():
    return NameIndex(ENTRIES)


def test_exact_name(index):
    assert index.resolve("Half-Life").entry.appid == 70


@pytest.mark.parametrize(
    "typed,appid",
    [
        ("Portal 2 ", 620),        # trailing space
        ("portal 2", 620),         # all lowercase
        ("half life 2", 220),      # missing hyphen
        ("HALF-LIFE 2", 220),      # shouting
    ],
)
def test_casing_and_punctuation_do_not_matter(index, typed, appid):
    """Without rapidfuzz normalisation these score 72-86 and get rejected by the 90 threshold."""
    assert index.resolve(typed).entry.appid == appid


def test_appid_bypasses_fuzzy_matching(index):
    assert index.resolve(620).entry.appid == 620
    assert index.resolve("620").entry.appid == 620


def test_nonexistent_sequel_is_not_silently_downgraded(index):
    """The bug this test exists for: 'Half-Life 3' scores 90+ against 'Half-Life'."""
    resolution = index.resolve("Half-Life 3")
    assert resolution.entry is None, f"resolved to {resolution.entry}"
    assert "not the same game" in resolution.reason


def test_a_base_game_is_not_answered_with_its_sequel(index):
    assert index.resolve("Portal").entry.appid == 400
    assert index.resolve("Portal 3").entry is None


def test_roman_numerals_count_as_sequel_markers(index):
    """II and 2 are the same sequel, so the marker check must not reject the match on that basis."""
    from gamerec.names import _sequel_markers

    assert _sequel_markers("DARK SOULS II: Scholar of the First Sin") == _sequel_markers("Dark Souls 2")
    assert _sequel_markers("DARK SOULS III") != _sequel_markers("Dark Souls 2")


def test_a_long_subtitle_falls_below_the_threshold(index):
    """Documented limitation, not an accident: "DARK SOULS 2" scores 86.1 against the full title.
    Lowering the threshold would start matching different games, so the appid is the way in."""
    assert index.resolve("DARK SOULS 2").entry is None
    assert index.resolve(335300).entry.appid == 335300


@pytest.mark.parametrize(
    "name,fragment",
    [
        ("ELDEN RING", "has not been ingested yet"),
        ("Frostpunk", "too few reviews"),
        ("Frostpunk Soundtrack", "not a game on Steam"),
    ],
)
def test_known_but_unusable_games_explain_themselves(index, name, fragment):
    resolution = index.resolve(name)
    assert not resolution.usable
    assert fragment in resolution.reason


def test_unknown_appid_says_so(index):
    assert "not in the Steam catalogue" in index.resolve(999999).reason
