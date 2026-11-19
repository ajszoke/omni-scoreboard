"""The unseen major's result under `omni-board-policy/2`, through `BoardSession` in the synthetic time loop of
`tests.board_policy` (`BOARD_POLICY.md` §4, §6).

The case throughout: the Braves (a favorite) act from 3 on. The Angels' walk-off is offered at 0, decisive but on
proxy leverage, so it can't cut into the Braves' action; its card's window and its promotion's lifetime both end at
120 (a 12 s major must start by +108). The Angels' final, naming the walk-off as the play that decided it, is
confirmed at 105. The Braves' first ordinary break comes at 135, at a turn's boundary.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

import pytest

from omni.cards.base import CardKind, ScoreboardCard
from omni.cards.baseball import BigPlayCardPayload, FinalCardPayload
from omni.cards.football import LiveFootballCardPayload
from omni.domain.breaks import BreakKind
from omni.domain.contest import TeamGame
from omni.domain.football import FootballGameState
from omni.events.baseball import BaseballGameEventType
from omni.policy.interrupts import InterruptClass
from omni.policy.promotion import PromotionKey, PromotionRefusal
from omni.tower.decisions import CardNote, CardRole, WaitNote
from omni.tower.director import DirectorPolicy
from omni.tower.summary import SUMMARY_LABEL, ResultSummary, SummaryPolicy, terminal
from tests.board_policy import T0, Run, at, big_play, evidence, facts, final, games, live, major

_BREAK = 135.0


@dataclasses.dataclass
class _Unseen:
    run: Run
    braves: TeamGame
    walk_off: ScoreboardCard[Any]
    result: ScoreboardCard[Any]

    def cue(self, seconds: float) -> ScoreboardCard[Any]:
        """The card drawn by the last cue at ``seconds``."""
        payload = [cue.payload for when, cue in self.run.cues if when == seconds][-1]
        assert isinstance(payload, ScoreboardCard)
        return payload

    def results(self) -> list[tuple[float, str, str]]:
        return [
            (note.at.monotonic.seconds, note.card.raw, note.why)
            for note in self.run.of(CardNote)
            if note.role is CardRole.RESULT
        ]

    def summaries(self) -> list[float]:
        return [when for when, cue in self.run.cues if cue.payload.id.raw.endswith(":result")]  # type: ignore[union-attr]


def _unseen(
    *,
    break_at: float = _BREAK,
    until: float = 160,
    confirmed: bool = True,
    walk_off: ScoreboardCard[Any] | None = None,
    result: Any = None,
    run: Run | None = None,
) -> _Unseen:
    slate = games(3)
    braves, angels = slate[0], slate[1]
    walk_off = walk_off if walk_off is not None else big_play(angels, "wo")
    result = result if result is not None else final(angels, ended=0, available=105, after=(walk_off.dedupe_key,))
    run = run if run is not None else Run()
    run.stage(live(braves), walk_off)
    run.describe(facts(braves, 0.5))
    run.offers.append(major(walk_off, 0.9, model=False))  # decisive, on proxy leverage: never into action
    if confirmed:
        run.publish_at(105, lambda: run.stage(result, when=105))

    def inning_break() -> None:
        run.describe(facts(braves, 0.5, breaks=(evidence(braves, BreakKind.MLB_INNING_BREAK, received=break_at),)))
        run.publish()

    run.on(break_at, inning_break)
    run.publish()
    run.run(3, until)
    return _Unseen(run, braves, walk_off, result)


def test_an_action_blocked_walk_off_airs_as_its_final_s_summary_at_the_first_break_and_spends_no_cooldown() -> None:
    """One ordered result at 135: the Angels' final, drawn first as the walk-off's still with the confirmed score.
    It's a result, not a promotion: a homer offered at 136 is promoted as soon as the summary's 8 s are done."""
    slate = games(3)
    homer = big_play(slate[2], "hr", available=136)
    run = Run()
    run.stage(homer, when=136)
    run.publish_at(136, lambda: run.offers.append(major(homer, 0.9, eligible=136, model=False)))
    case = _unseen(run=run)
    assert case.results() == [
        (_BREAK, case.result.id.raw, "Angels at D-backs: final, with the play that decided it; Braves in inning break")
    ]
    drawn = case.cue(_BREAK)
    walk_off, result = case.walk_off.payload, case.result.payload
    assert isinstance(drawn.payload, BigPlayCardPayload) and isinstance(result, FinalCardPayload)
    assert drawn.id.raw == f"{case.walk_off.id.raw}:result" and drawn.kind is CardKind.BIG_PLAY
    assert drawn.dedupe_key == case.result.dedupe_key  # the final's own key: its place in end order is its own
    assert (drawn.payload.event_type, drawn.payload.description) == (walk_off.event_type, SUMMARY_LABEL)
    assert (drawn.payload.away_score, drawn.payload.home_score) == (result.away_score, result.home_score)
    assert case.run.shown(case.result.dedupe_key.raw) == [(_BREAK, _BREAK + 8)]
    assert case.run.promotions() == [(_BREAK + 8, homer.dedupe_key.raw)]  # no cooldown was charged at 135
    retired = [note for note in case.run.of(WaitNote) if note.reason is PromotionRefusal.RETIRED]
    assert [note.at.monotonic.seconds for note in retired] == [111.0]  # the first decision past +108


def test_a_walk_off_with_no_final_to_lead_never_airs_as_a_result() -> None:
    case = _unseen(confirmed=False)
    assert case.results() == [] and case.summaries() == []


def test_a_final_from_a_stale_source_airs_plain_and_says_its_deciding_play_is_gone() -> None:
    angels = games(3)[1]
    walk_off = big_play(angels, "wo")
    stale = dataclasses.replace(
        final(angels, ended=0, available=105, after=(walk_off.dedupe_key,)), stale_since=T0 + timedelta(seconds=100)
    )
    case = _unseen(walk_off=walk_off, result=stale)
    assert case.summaries() == []
    assert [why for _, _, why in case.results()] == [
        "Angels at D-backs: final; the play that decided it is gone; Braves in inning break"
    ]


def test_a_walk_off_the_board_has_no_still_for_leaves_its_final_plain() -> None:
    """Only an MLB big play has a static still today; another payload's story can't be drawn, so its final airs
    plain."""
    angels = games(3)[1]
    football = LiveFootballCardPayload(state=FootballGameState(period=4, clock_seconds=0, away_score=1, home_score=2))
    walk_off = dataclasses.replace(big_play(angels, "wo"), payload=football)
    case = _unseen(walk_off=walk_off)
    assert case.summaries() == []
    assert [why for _, _, why in case.results()] == [
        "Angels at D-backs: final; the play that decided it is gone; Braves in inning break"
    ]


def test_a_walk_off_that_aired_before_its_final_is_never_summarized() -> None:
    """A break at 110 comes after the promotion ran out but while the walk-off's card is still up to draw: it airs
    as the play that decided the final, and the final follows it plain."""
    case = _unseen(break_at=110)
    assert case.summaries() == []
    assert [(when, card) for when, card, _ in case.results()] == [
        (111.0, case.walk_off.id.raw),
        (119.0, case.result.id.raw),
    ]
    assert case.results()[1][2] == "Angels at D-backs: final; Braves in inning break"


def test_a_revision_of_the_walk_off_after_its_promotion_ran_out_replaces_its_story() -> None:
    angels = games(3)[1]
    walk_off = big_play(angels, "wo")
    payload = walk_off.payload
    assert isinstance(payload, BigPlayCardPayload)
    revised = dataclasses.replace(
        walk_off, payload=dataclasses.replace(payload, event_type=BaseballGameEventType.TRIPLE)
    )
    run = Run()

    def revise() -> None:
        run.stage(revised, when=115)
        run.offers[0] = dataclasses.replace(run.offers[0], revision=1)
        run.publish()

    run.on(115, revise)
    case = _unseen(walk_off=walk_off, run=run)
    drawn = case.cue(_BREAK).payload
    assert isinstance(drawn, BigPlayCardPayload) and drawn.event_type is BaseballGameEventType.TRIPLE
    assert [note.reason for note in run.of(WaitNote) if note.revision == 1] == [PromotionRefusal.RETIRED]


def test_a_summary_isn_t_held_by_the_promotion_cooldown() -> None:
    """A homer offered at 130 is promoted into the break at 135, ahead of the result: the summary follows at 147,
    inside that promotion's cooldown."""
    slate = games(3)
    homer = big_play(slate[2], "hr", available=130)
    run = Run()
    run.stage(homer, when=130)
    run.publish_at(130, lambda: run.offers.append(major(homer, 0.9, eligible=130, model=False)))
    case = _unseen(run=run, until=170)
    assert run.promotions() == [(_BREAK, homer.dedupe_key.raw)]
    assert case.summaries() == [147.0] and [when for when, _, _ in case.results()] == [147.0]


def test_a_summary_past_its_five_minutes_leaves_the_final_plain_while_the_final_stays_valid() -> None:
    case = _unseen(break_at=423, until=440)  # the final became eligible at 105: its story is fresh until 405
    assert case.summaries() == []
    assert case.results() == [
        (
            423.0,
            case.result.id.raw,
            "Angels at D-backs: final; the play that decided it is gone; Braves in inning break",
        )
    ]


def test_a_final_corrected_while_its_summary_is_up_keeps_the_story_with_the_corrected_score() -> None:
    angels = games(3)[1]
    walk_off = big_play(angels, "wo")
    result = final(angels, ended=0, available=105, after=(walk_off.dedupe_key,))
    payload = result.payload
    assert isinstance(payload, FinalCardPayload)
    corrected = dataclasses.replace(result, payload=dataclasses.replace(payload, home_score=4))
    run = Run()
    run.publish_at(138, lambda: run.stage(corrected, when=138))
    case = _unseen(walk_off=walk_off, result=result, run=run)
    drawn = case.cue(138)
    assert drawn.id.raw.endswith(":result") and isinstance(drawn.payload, BigPlayCardPayload)
    assert drawn.payload.home_score == 4


def test_a_perfect_game_named_on_its_final_that_ran_out_airs_as_that_final_s_own_result() -> None:
    """The feat's offer names the Angels' final itself: on proxy leverage it can't cut into the Braves' action, and
    its lifetime runs out. Its story is the final's own, which airs plainly as its ordered result at the break."""
    slate = games(3)
    braves, angels = slate[0], slate[1]
    perfect = final(angels, ended=0, available=0)
    run = Run()
    run.stage(live(braves), perfect)
    run.describe(facts(braves, 0.5))
    run.offers.append(major(perfect, 1.1375, model=False, event=InterruptClass.PERFECT_GAME))
    stopped = evidence(braves, BreakKind.MLB_INNING_BREAK, received=_BREAK)
    run.publish_at(_BREAK, lambda: run.describe(facts(braves, 0.5, breaks=(stopped,))))
    run.publish()
    run.run(3, 160)
    case = _Unseen(run, braves, perfect, perfect)
    assert case.summaries() == [] and run.promotions() == []
    assert case.results() == [(_BREAK, perfect.id.raw, "Angels at D-backs: final; Braves in inning break")]


@pytest.mark.parametrize("event", list(InterruptClass))
def test_only_a_decisive_event_or_a_confirmed_feat_outlives_its_promotion(event: InterruptClass) -> None:
    assert terminal(event) is (event in (InterruptClass.DECISIVE_LATE_WINNER, *_FEATS))


_FEATS = (InterruptClass.PERFECT_GAME, InterruptClass.NO_HITTER)


def test_a_summary_s_terms_are_checked() -> None:
    angels = games(3)[1]
    walk_off = big_play(angels, "wo")
    with pytest.raises(ValueError, match="positive"):
        SummaryPolicy(fresh_for=timedelta(0))
    key = PromotionKey(angels.id, "wo")
    with pytest.raises(ValueError, match="outlives"):
        ResultSummary(key=key, revision=0, event=InterruptClass.MAJOR, card=walk_off, until=at(0).monotonic)
    story = ResultSummary(key=key, revision=0, event=InterruptClass.NO_HITTER, card=walk_off, until=at(0).monotonic)
    assert story.drawn(live(angels)) is None  # only a baseball final has a score to draw it with
    assert DirectorPolicy().summary == SummaryPolicy()
