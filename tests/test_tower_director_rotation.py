"""The board's rotation under `omni-board-policy/2`, through `BoardSession` in the synthetic time loop of
`tests.board_policy`: a stale game kept out of the weighted turns, the non-base cap counted on confirmed time with a
live base on hand, and a base turn's accounting split where its eligible games change."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

import pytest

from omni.cards.base import ScoreboardCard
from omni.domain.breaks import BreakKind
from omni.policy.allocation import SetAsideReason
from omni.preferences.favorites import Favorites
from omni.tower.board import BoardHealth
from omni.tower.decisions import BaseReason, BaseTurnNote, CapNote, CardNote, CardRole
from tests.board_policy import (
    T0,
    Run,
    action,
    evidence,
    facts,
    final,
    games,
    live,
    offline,
    pregame,
)
from tests.test_tower_director_rules import _football

NO_FAVORITES = Favorites()


def _key(card: ScoreboardCard[Any]) -> str:
    return card.dedupe_key.raw


# --- stale games -----------------------------------------------------------------------------------------------------


def test_a_stale_nfl_tile_takes_no_turn_from_a_healthy_mlb_game_and_airs_only_with_nothing_fresh_to_show() -> None:
    """The Steelers' feed has gone stale; the Angels' is healthy. Every base turn is the Angels', and the Steelers'
    dim card never airs while they play. Once the Angels' game is gone, the stale tile is the last safe thing to show:
    it airs dimmed, as a fallback with no base turn. Back to fresh, the Steelers start their account at zero."""
    angels = games(2)[1]
    steelers = _football("1", "PIT", "CIN", caption=None)
    stale = dataclasses.replace(steelers, stale_since=T0)
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(angels), stale)
    run.describe(facts(angels, 0.4), facts(steelers.contest, 0.4, fresh=False))  # type: ignore[arg-type]
    run.publish()
    run.run(0, 120)
    assert set(run.base_turns()) == {angels.id.raw}
    assert _key(stale) not in run.keys()

    def angels_gone() -> None:
        del run.facts[angels.id]
        run.queue.remove(_key(live(angels)))

    run.publish_at(121, angels_gone)
    run.run(121, 160)
    assert set(run.base_turns()) == {angels.id.raw}  # the fallback takes no base turn
    fallback = [
        (note.at.monotonic.seconds, note.card.raw) for note in run.of(CardNote) if note.role is CardRole.FALLBACK
    ]
    assert fallback[0] == (121.0, _key(stale))  # at once: the Angels' card came down with their game
    assert all(cue.stale for when, cue in run.cues if 121 <= when < 200)
    assert [note.why for note in run.of(CardNote) if note.role is CardRole.FALLBACK][0] == (
        "Steelers at Bengals: the last safe frame; nothing fresh to show"
    )

    def fresh_again() -> None:
        run.stage(steelers, live(angels), when=200)
        run.describe(facts(angels, 0.4), facts(steelers.contest, 0.4))  # type: ignore[arg-type]

    run.publish_at(200, fresh_again)
    run.run(200, 260)
    [first] = [note for note in run.of(BaseTurnNote) if note.contest == steelers.contest.id][:1]
    assert first.actual is None and first.debt == 0.0  # no credit, wait or debt from its stale spell


def test_a_stale_game_is_set_aside_from_the_weighted_targets() -> None:
    angels, other = games(3)[1:]
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(angels), dataclasses.replace(live(other), stale_since=T0))
    run.describe(facts(angels, 0.4), facts(other, 0.4, fresh=False))
    run.publish()
    run.run(0, 13)
    [note] = run.of(BaseTurnNote)[:1]
    assert note.targets is not None and [game.contest for game in note.targets.games] == [angels.id]
    assert [(aside.contest, aside.reason) for aside in note.targets.set_aside] == [(other.id, SetAsideReason.STALE)]
