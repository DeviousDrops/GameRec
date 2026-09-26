"""What a run chooses to spend its request budget on (D14, D15).

Ordering is the interesting part. The cap on refreshes is easy to get right and easy to notice when
it is wrong; the priority is neither -- a run that quietly spends its whole budget re-fetching games
already in the corpus looks perfectly healthy and adds nothing anyone asked for.
"""

from __future__ import annotations

from gamerec.names import IN_CORPUS, PENDING_INGEST, NameEntry, NameIndex
from ingest.run import plan
from ingest.steam import Popular


def pop(appid: int, reviews: int = 100) -> Popular:
    return Popular(appid, f"Game {appid}", reviews)


def test_a_first_run_takes_everything_in_popularity_order():
    candidates = [pop(1), pop(2), pop(3)]
    assert [p.appid for p in plan(candidates, set(), max_updates=500)] == [1, 2, 3]


def test_already_seen_appids_come_after_new_ones():
    """This is the resumption property: a fill that stopped at 2 spends the next run on 3 and 4."""
    candidates = [pop(1), pop(2), pop(3), pop(4)]
    assert [p.appid for p in plan(candidates, {1, 2}, max_updates=500)] == [3, 4, 1, 2]


def test_refreshes_are_capped_and_new_games_are_not():
    """A big Steam change day must not starve out new games (D15)."""
    candidates = [pop(i) for i in range(1, 11)]
    seen = {1, 2, 3, 4, 5, 6, 7, 8}

    chosen = [p.appid for p in plan(candidates, seen, max_updates=2)]

    assert chosen[:2] == [9, 10], "new appids are never deferred"
    assert len(chosen) == 4 and set(chosen[2:]) <= seen


def test_nothing_new_and_no_update_budget_means_no_requests():
    candidates = [pop(1), pop(2)]
    assert plan(candidates, {1, 2}, max_updates=0) == []


def test_merging_names_does_not_shrink_the_index(tmp_path):
    """Each run sees a slice of the catalogue, so writing only that slice would throw away every
    earlier entry -- including the not-in-corpus ones that let a miss explain itself."""
    path = tmp_path / "names.json"
    NameIndex.dump([NameEntry(1, "One", IN_CORPUS), NameEntry(2, "Two", PENDING_INGEST)], path)

    size = NameIndex.merge(path, [NameEntry(3, "Three", IN_CORPUS)])

    assert size == 3
    assert len(NameIndex.load(path)) == 3


def test_merging_names_lets_a_status_change(tmp_path):
    path = tmp_path / "names.json"
    NameIndex.dump([NameEntry(2, "Two", PENDING_INGEST)], path)

    NameIndex.merge(path, [NameEntry(2, "Two", IN_CORPUS)])

    assert NameIndex.load(path).resolve(2).usable


def test_merging_into_nothing_is_a_first_run(tmp_path):
    path = tmp_path / "names.json"
    assert NameIndex.merge(path, [NameEntry(1, "One", IN_CORPUS)]) == 1
    assert path.exists()
