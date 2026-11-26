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


# --- the non-base cap ------------------------------------------------------------------------------------------------


def test_neutral_rotation_before_any_live_game_counts_nothing_toward_the_cap() -> None:
    """A pregame rotates alone for 40 s; when a game goes live, its first turn is weighed as usual, not a recovery owed
    to a cap that neutral circulation never ran up."""
    slate = games(3)
    soon = pregame(dataclasses.replace(slate[2], scheduled_start=T0 + timedelta(minutes=80)))
    run = Run(favorites=NO_FAVORITES)
    run.stage(soon)
    run.publish()
    run.publish_at(40, lambda: run.stage(live(slate[1]), when=40), lambda: run.describe(facts(slate[1], 0.4)))
    run.run(0, 70)
    first = run.of(BaseTurnNote)[0]
    assert first.at.monotonic.seconds == 40.0 and first.reason is BaseReason.ONLY_CANDIDATE
    assert run.of(CapNote) == []


def test_an_owed_recovery_cut_short_by_an_outage_is_still_owed_when_the_source_returns() -> None:
    """Four finals run the cap up by 32, and the recovery it reserves is cut short at 36 when the source goes quiet.
    Back at 60, with two finals still due, the board first finishes the full base turn it owes."""
    slate = games(8)
    base = slate[1]
    finals = [final(each, ended=-60 + 10 * index) for index, each in enumerate(slate[2:])]
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(base), *finals)
    run.describe(facts(base, 0.4))
    run.publish()

    def quiet() -> None:
        run.health = offline()

    def back() -> None:
        run.health = BoardHealth()

    run.publish_at(36, quiet)
    run.publish_at(60, back)
    run.run(0, 90)
    after = [(up.at, up.key) for up in run.ups if up.at >= 60]
    assert after[0] == (60.0, _key(live(base)))
    [recovery] = [note for note in run.of(BaseTurnNote) if note.at.monotonic.seconds == 60.0]
    assert recovery.reason is BaseReason.RECOVERY
    assert run.shown(_key(live(base)))[1] == (60.0, 72.0)  # the full turn, through its completion


# --- fairness through a favorite's break -------------------------------------------------------------------------------


def _braves_break(opens: float, ends: float) -> Run:
    """The Braves (a favorite), the Angels and the Orioles live; the Braves' inning break is released at ``opens`` and
    play resumes at ``ends``."""
    braves, angels, other = games(3)
    run = Run()
    run.stage(live(braves), live(angels), live(other))
    run.describe(facts(braves, 0.5), facts(angels, 0.6), facts(other, 0.3))
    in_break = evidence(braves, BreakKind.MLB_INNING_BREAK, received=opens, stated=175)
    run.publish_at(opens, lambda: run.describe(facts(braves, 0.5, breaks=(in_break,))))
    resumed = action(braves, "ab 12 p1", received=ends, closes=(in_break.id,))  # play after the break
    run.publish_at(ends, lambda: run.describe(facts(braves, 0.5, action=resumed)))
    run.publish()
    run.run(0, ends + 20)
    return run


def test_a_favorite_whose_break_opens_in_another_game_s_turn_is_credited_up_to_it_and_nothing_after() -> None:
    """The Braves take the first two turns at their 75% floor, and the Angels' turn at 24 is chosen with the Braves
    acting. The Braves' break opens at 29, and the board sees it at 32, when the turn's protection ends: the turn's first
    8 s are credited to the games eligible when it was chosen, the Braves among them, and its last 4 s to the games
    eligible now, without them. Nothing is credited for the break, nor back once it ends, so at their next turn, at 56,
    the Braves are owed their own two turns' pay (12 s at their share, less the 12 s shown, each) and those 8 s."""
    braves = games(1)[0]
    run = _braves_break(opens=29, ends=52)
    turns = {note.at.monotonic.seconds: note for note in run.of(BaseTurnNote)}
    assert [(at, note.contest.raw) for at, note in sorted(turns.items())][:6] == [
        (0.0, "g00"),
        (12.0, "g00"),
        (24.0, "g01"),
        (36.0, "g02"),
        (48.0, "g01"),
        (56.0, "g00"),
    ]
    first, second, chosen = turns[0.0].targets, turns[12.0].targets, turns[24.0].targets
    assert first is not None and second is not None and chosen is not None
    own, again, latched = first.game(braves.id), second.game(braves.id), chosen.game(braves.id)
    assert own is not None and again is not None and latched is not None
    assert latched.within_league == pytest.approx(0.75)
    expected = -24 + 12 * own.within_league + 12 * again.within_league + 8 * latched.within_league
    assert turns[56.0].debt == pytest.approx(expected, abs=1e-9)  # 0.00 s; crediting by the turn's end gave -6.00 s


def test_a_favorite_s_own_turn_runs_its_12_s_when_its_break_opens_in_it() -> None:
    run = _braves_break(opens=5, ends=30)
    braves = games(1)[0]
    assert run.shown(live(braves).dedupe_key.raw)[0] == (0.0, 12.0)
    assert [note.contest for note in run.of(BaseTurnNote)][1] != braves.id  # then its break is another game's
