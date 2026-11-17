"""The board's input and its rules, through `AppLoop.refresh` and `AppLoop.paint`.

A refresh publishes the board's input whole, so a paint on the frame thread sees one refresh or the one
before it, never half; a card drawn from a view books only if the latest still holds it as drawn. Past
the takeover cap a base card's turn is reserved; a transient leaves at its own turn; a live card its own
final replaced hands off at its minimum; and a card too late to start holds nothing up behind it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from omni.app.contest_store import ContestStore
from omni.app.display import RecordingDisplaySink
from omni.app.loop import AppLoop, Painted
from omni.app.pipeline import LiveBaseballPipeline
from omni.app.supervisor import ProviderSupervisor
from omni.cards.attention import AttentionMode, AttentionPolicy
from omni.cards.base import CardId, CardPriority, DedupeKey, EndBasis, GameEnd, ScoreboardCard
from omni.cards.baseball import LiveBaseballCardPayload
from omni.cards.factory import CardFactory
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.providers.base import ProviderError, ProviderUpdate
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue, Missed, MissReason, SelectionReason
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.registry import RendererRegistry, default_registry
from omni.tower.board import BoardNote, BoardPolicy, FrameKind, SourceHealth
from omni.tower.lane import ChangeKind, EndReason

T = datetime(2026, 6, 17, 23, 0, tzinfo=timezone.utc)
QUAD = PanelProfile.QUAD_128X64
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
_REG = MlbTeamRegistry.from_color_file()
_TEAMS = {
    "atl-nym": (144, 121),
    "bos-nyy": (111, 147),
    "chc-stl": (112, 138),
    "col-lad": (115, 119),
    "sf-sd": (137, 135),
}
_BURST = AttentionPolicy(mode=AttentionMode.BURST, takeover_for=DurationSeconds(12))
_REMINDER = AttentionPolicy(mode=AttentionMode.RECURRING, cooldown=DurationSeconds(60))


def _at(seconds: float) -> ClockReading:
    return ClockReading(T + timedelta(seconds=seconds), MonotonicTime(seconds))


def _game(raw: str, status: GameStatus = GameStatus.LIVE) -> TeamGame:
    away, home = _TEAMS[raw]
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, raw),
        league=League.MLB,
        status=status,
        scheduled_start=T,
        away=_REG.resolve(away),
        home=_REG.resolve(home),
    )


def _state(away: int = 1, home: int = 2) -> BaseballGameState:
    return BaseballGameState(
        away_score=away,
        home_score=home,
        inning=5,
        phase=InningPhase.TOP,
        count=BaseballCount(balls=0, strikes=0, outs=0),
        bases=BaseballBaseState(),
        away_hits=4,
        home_hits=5,
    )


class _Slate:
    """The schedule and each game's feed, as a test sets them: games listed in order, each feed agreeing with
    the schedule on its game's status, and a source that can fail."""

    source = SOURCE
    league = League.MLB

    def __init__(self, *games: str) -> None:
        self.status = {raw: GameStatus.LIVE for raw in games}
        self.scores = {raw: (1, 2) for raw in games}
        self.fail = False

    def refresh(self, now: datetime) -> ProviderUpdate:
        if self.fail:
            raise ProviderError("schedule fetch down")
        contests = tuple(_game(raw, status) for raw, status in self.status.items())
        return ProviderUpdate(source=SOURCE, observed_at=now, contests=contests)

    def feed(self, game: TeamGame, now: datetime) -> LiveBaseballFeed:
        return LiveBaseballFeed(state=_state(*self.scores[game.id.raw]), status=self.status[game.id.raw])


class _PausingQueue(InterleavedCardQueue):
    """A queue that lets the frame thread in mid-refresh: right after a chosen card is staged, or once the
    refresh's cards are all staged and before it publishes them with the source's health."""

    def __init__(self) -> None:
        super().__init__()
        self.after: str | None = None
        self.before_publishing: Callable[[], None] | None = None
        self.pause: Callable[[], None] = lambda: None

    def ingest(self, card: ScoreboardCard[Any]) -> None:
        super().ingest(card)
        if card.dedupe_key.raw == self.after:
            self.pause()

    def prune(self, now: datetime) -> list[Missed]:
        missed = super().prune(now)
        if self.before_publishing is not None:
            self.before_publishing()
        return missed


class _Drawing(RendererRegistry):
    """Today's renderers, keeping what each frame drew, running a hook while a frame is drawn, and failing
    to draw the cards it's told to."""

    def __init__(self) -> None:
        super().__init__()
        self._real = default_registry()
        self.drawn: list[ScoreboardCard[Any]] = []
        self.while_drawing: Callable[[ScoreboardCard[Any]], None] | None = None
        self.broken: set[str] = set()

    def render(self, card: ScoreboardCard[Any], context: RenderContext, canvas: Canvas) -> None:
        if card.dedupe_key.raw in self.broken:
            raise ValueError("can't draw this one")
        hook, self.while_drawing = self.while_drawing, None
        if hook is not None:
            hook(card)
        self.drawn.append(card)
        self._real.render(card, context, canvas)


def _loop(
    slate: _Slate,
    *,
    lag: int = 0,
    favorites: frozenset[str] = frozenset(),
    policy: BoardPolicy = BoardPolicy(),
) -> tuple[AppLoop, _PausingQueue, _Drawing]:
    queue = _PausingQueue()
    registry = _Drawing()
    pipeline = LiveBaseballPipeline(
        scorer=PriorityScorer(favorites=favorites),
        delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(lag)),
        factory=CardFactory(),
        queue=queue,
    )
    loop = AppLoop(
        supervisor=ProviderSupervisor(slate, max_age=DurationSeconds(120)),
        store=ContestStore(),
        pipeline=pipeline,
        queue=queue,
        registry=registry,
        sink=RecordingDisplaySink(QUAD),
        fetch_feed=slate.feed,
        policy=policy,
    )
    return loop, queue, registry


def _step(loop: AppLoop, seconds: float) -> Painted:
    """A refresh, then a paint, at ``seconds``."""
    loop.refresh(_at(seconds))
    return loop.paint(_at(seconds))


def _up(painted: Painted) -> str | None:
    """The card the paint put up, if it put one up."""
    ups = [change for change in painted.changes if change.kind is ChangeKind.UP]
    payload = ups[0].segment.frames[0] if ups else None
    return payload.dedupe_key.raw if isinstance(payload, ScoreboardCard) else None


def _down(painted: Painted) -> tuple[str, EndReason | None, timedelta | None]:
    [down] = [change for change in painted.changes if change.kind is not ChangeKind.UP]
    payload = down.segment.frames[0]
    assert isinstance(payload, ScoreboardCard)
    return payload.dedupe_key.raw, down.reason, down.aired


# --- one coherent input per refresh -----------------------------------------------------------------


def test_a_paint_between_two_final_inserts_sees_the_refresh_before_and_the_earlier_final_airs_first() -> None:
    slate = _Slate("bos-nyy", "atl-nym")  # the schedule lists the game that ended later first
    loop, queue, _ = _loop(slate, lag=30)
    _step(loop, 0)
    assert _step(loop, 30).shown is not None  # both live cards clear the delay; one goes up
    slate.status["atl-nym"] = GameStatus.FINAL
    loop.refresh(_at(40))  # first seen final at 40: its result shows at 70
    slate.status["bos-nyy"] = GameStatus.FINAL
    loop.refresh(_at(50))  # first seen final at 50: its result shows at 80
    seen: list[Painted] = []
    generations: list[int] = []

    def frame_thread() -> None:
        generations.append(queue.published.generation)
        seen.append(loop.paint(_at(80)))

    queue.after, queue.pause = "bos-nyy:final", frame_thread
    before = queue.published.generation
    refresh = loop.refresh(_at(80))  # both results show: bos-nyy's is staged first
    [mid] = seen
    assert generations == [before] and queue.published.generation == before + 1  # one refresh, one view
    assert mid.shown is not None and mid.shown.raw.endswith(":live")  # the last whole refresh: no final yet
    assert [card.raw for card, _ in refresh.pipeline.ends] == ["bos-nyy:final", "atl-nym:final"]
    finals = [_up(loop.paint(_at(seconds))) for seconds in (80, 88, 100)]
    assert finals == [None, "atl-nym:final", "bos-nyy:final"]  # the live card's minimum, then in order


def test_a_paint_between_the_new_cards_and_their_health_sees_the_old_cards_with_the_old_health() -> None:
    slate = _Slate("col-lad")
    loop, queue, drawn = _loop(slate)
    assert not _step(loop, 0).stale
    slate.fail, slate.scores["col-lad"] = True, (3, 2)  # the schedule stops answering; the feed moves on
    seen: list[Painted] = []
    queue.before_publishing = lambda: seen.append(loop.paint(_at(121)))
    refresh = loop.refresh(_at(121))  # the new score, and the source past its age
    queue.before_publishing = None
    [mid] = seen
    old = drawn.drawn[-1].payload
    assert isinstance(old, LiveBaseballCardPayload) and old.away_line.runs == 1 and not mid.stale
    assert refresh.health.source is SourceHealth.STALE
    after = loop.paint(_at(121.1))
    new = drawn.drawn[-1].payload
    assert isinstance(new, LiveBaseballCardPayload) and new.away_line.runs == 3 and after.stale


# --- a cue superseded while it's drawn ----------------------------------------------------------------


def test_a_card_removed_while_its_first_frame_is_drawn_books_nothing_and_comes_down_unaired() -> None:
    slate = _Slate("atl-nym", "bos-nyy")
    loop, queue, drawn = _loop(slate)
    loop.refresh(_at(0))

    def game_ends(card: ScoreboardCard[Any]) -> None:
        del slate.status[card.contest.id.raw]  # its game leaves the schedule while the frame is drawn
        loop.refresh(_at(0.05))

    drawn.while_drawing = game_ends
    painted = loop.paint(_at(0))
    gone = drawn.drawn[-1].dedupe_key.raw
    assert painted.shown is not None and painted.presented is None and painted.changes == ()  # shown, not counted
    assert not queue.aired(gone)
    after = loop.paint(_at(0.1))
    assert _down(after) == (gone, EndReason.INVALIDATED, None)  # dropped: it never counted as aired
    assert _up(after) is not None and _up(after) != gone


def test_a_card_replaced_while_its_first_frame_is_drawn_counts_from_the_new_version_s_first_frame() -> None:
    slate = _Slate("col-lad")
    loop, queue, drawn = _loop(slate)
    loop.refresh(_at(0))

    def scored(card: ScoreboardCard[Any]) -> None:
        slate.scores["col-lad"] = (3, 2)
        loop.refresh(_at(0.05))

    drawn.while_drawing = scored
    painted = loop.paint(_at(0))
    assert painted.presented is None and not queue.aired("col-lad:live")  # the old version doesn't book the new
    after = loop.paint(_at(0.1))
    assert after.presented is not None and after.presented.at == T + timedelta(seconds=0.1)
    [up] = after.changes
    card = up.segment.frames[0]
    assert isinstance(card, ScoreboardCard) and card.payload.away_line.runs == 3 and queue.aired("col-lad:live")


# --- takeovers, the recovery turn, and transients ---------------------------------------------------------


def _card(
    gid: str, suffix: str = "live", *, attention: AttentionPolicy | None = None, expires: float | None = None
) -> ScoreboardCard[LiveBaseballCardPayload]:
    """A card for ``gid`` keyed ``gid:suffix``: a live card, or a takeover or reminder like one."""
    card = CardFactory().live_baseball(_game(gid), _state(), now=T)
    key = f"{gid}:{suffix}"
    card = dataclasses.replace(card, id=CardId(key), dedupe_key=DedupeKey(key))
    if attention is not None:
        priority = CardPriority(band=DisplayPriority.ALERT, score=0.0)
        card = dataclasses.replace(card, attention=attention, priority=priority)
    if expires is not None:
        timing = dataclasses.replace(card.timing, expires_at=T + timedelta(seconds=expires))
        card = dataclasses.replace(card, timing=timing)
    return card


def _staged(loop: AppLoop, queue: InterleavedCardQueue, seconds: float, *cards: ScoreboardCard[Any]) -> Painted:
    """Stage ``cards`` as a refresh's pipeline would, then refresh and paint at ``seconds``."""
    queue.ingest_all(cards)
    return _step(loop, seconds)


def test_a_due_reminder_neither_takes_nor_cuts_the_base_card_s_turn_owed_after_the_takeover_cap() -> None:
    loop, queue, _ = _loop(_Slate(), policy=BoardPolicy(takeover_cap=timedelta(seconds=20)))
    first = _staged(loop, queue, 0, _card("atl-nym"), _card("bos-nyy"))
    burst = (_card("chc-stl", "bang1", attention=_BURST), _card("chc-stl", "bang2", attention=_BURST))
    assert _staged(loop, queue, 8, *burst).reason is SelectionReason.TAKEOVER  # at the base card's minimum
    assert _step(loop, 20).reason is SelectionReason.TAKEOVER  # 12 s of takeovers: under the cap
    reminder = _staged(loop, queue, 25, _card("col-lad", "nono", attention=_REMINDER))
    assert reminder.changes == ()  # due, but a takeover is up
    owed = _step(loop, 32)  # 24 s of takeovers: the second finishes its turn, and a base card is owed one
    assert owed.reason is SelectionReason.ROTATION and owed.shown != first.shown
    assert _step(loop, 40).changes == ()  # past its minimum, the due reminder still can't cut it
    turned = _step(loop, 44)
    assert _down(turned)[1:] == (EndReason.NEXT_WAITING, timedelta(seconds=12))  # its full turn
    assert turned.reason is SelectionReason.REMINDER


def test_a_recovery_turn_broken_by_its_card_leaving_is_still_owed() -> None:
    loop, queue, _ = _loop(_Slate(), policy=BoardPolicy(takeover_cap=timedelta(seconds=20)))
    _staged(loop, queue, 0, _card("atl-nym"), _card("bos-nyy"))
    _staged(loop, queue, 8, _card("chc-stl", "bang1", attention=_BURST), _card("chc-stl", "bang2", attention=_BURST))
    _step(loop, 20)
    owed = _staged(loop, queue, 32, _card("col-lad", "nono", attention=_REMINDER))
    assert owed.reason is SelectionReason.ROTATION and owed.shown is not None
    queue.remove(owed.shown.raw)  # its game moves on 3 s into the turn
    again = _step(loop, 35)
    assert _down(again)[1] is EndReason.INVALIDATED
    assert again.reason is SelectionReason.ROTATION and again.notes == ()  # the other base card, not the reminder


def test_with_no_base_card_to_give_the_turn_to_a_takeover_airs_and_the_board_notes_it() -> None:
    loop, queue, _ = _loop(_Slate(), policy=BoardPolicy(takeover_cap=timedelta(seconds=20)))
    bursts = [_card("chc-stl", f"bang{n}", attention=_BURST) for n in range(3)]
    assert _staged(loop, queue, 0, *bursts).reason is SelectionReason.TAKEOVER
    assert _step(loop, 12).reason is SelectionReason.TAKEOVER  # the first left at its own turn
    third = _step(loop, 24)  # 24 s of takeovers: a base card is owed a turn, but there's none
    assert third.reason is SelectionReason.TAKEOVER and third.notes == (BoardNote.NO_BASE_CARD,)
    idle = _step(loop, 36)
    assert _down(idle)[1:] == (EndReason.RAN_ITS_TIME, timedelta(seconds=12)) and idle.frame is FrameKind.IDLE


def test_a_lone_takeover_leaves_at_its_turn_and_the_board_shows_its_frame() -> None:
    loop, queue, _ = _loop(_Slate())
    bang = _card("chc-stl", "bang", attention=_BURST)
    assert _staged(loop, queue, 0, bang).reason is SelectionReason.TAKEOVER
    assert _step(loop, 11.9).changes == ()
    done = _step(loop, 12)
    assert _down(done) == ("chc-stl:bang", EndReason.RAN_ITS_TIME, timedelta(seconds=12))
    assert done.frame is FrameKind.IDLE
    assert _step(loop, 30).frame is FrameKind.IDLE  # spent, and alone: it doesn't come back
    assert _staged(loop, queue, 31, _card("atl-nym")).shown is not None  # beside a base card, it rotates again
    assert _step(loop, 43).shown == bang.id


def test_a_lone_reminder_due_again_leaves_at_its_turn_and_returns_as_a_new_beat_until_its_repeats_run_out() -> None:
    loop, queue, _ = _loop(_Slate())
    eager = AttentionPolicy(mode=AttentionMode.RECURRING, cooldown=DurationSeconds(5), max_repeats=2)
    assert _staged(loop, queue, 0, _card("sf-sd", "nono", attention=eager)).reason is SelectionReason.REMINDER
    again = _step(loop, 12)  # its turn is over, and it's due again: a new beat, booked as one
    assert _down(again) == ("sf-sd:nono", EndReason.RAN_ITS_TIME, timedelta(seconds=12))
    assert again.reason is SelectionReason.REMINDER and again.presented is not None
    done = _step(loop, 24)  # its second beat was its last
    assert _down(done)[1] is EndReason.RAN_ITS_TIME and done.frame is FrameKind.IDLE


# --- a game's own final, and a predecessor that can't air -------------------------------------------------


def test_a_live_card_its_own_final_replaced_keeps_its_minimum_then_the_final_airs_in_its_place() -> None:
    slate = _Slate("atl-nym", "bos-nyy")
    loop, _, _ = _loop(slate, favorites=frozenset({"ATL"}))
    assert _up(_step(loop, 0)) == "atl-nym:live"
    slate.status["atl-nym"] = GameStatus.FINAL
    held = _step(loop, 4)  # its result shows: the final replaces the live card
    assert held.shown == CardId("atl-nym:live") and held.changes == ()  # the game's last frame, through its minimum
    handed = loop.paint(_at(8))
    assert _down(handed) == ("atl-nym:live", EndReason.INVALIDATED, timedelta(seconds=8))
    assert _up(handed) == "atl-nym:final"  # in its place, ahead of the other game's turn
    assert handed.presented is not None and handed.presented.reason is SelectionReason.ROTATION


def test_a_live_card_hands_off_to_the_final_that_ended_first_while_its_own_waits_its_order() -> None:
    slate = _Slate("atl-nym", "bos-nyy")
    loop, _, _ = _loop(slate, lag=30, favorites=frozenset({"ATL"}))
    _step(loop, 0)
    assert _up(_step(loop, 30)) == "atl-nym:live"
    slate.status["bos-nyy"] = GameStatus.FINAL
    _step(loop, 31)
    slate.status["atl-nym"] = GameStatus.FINAL
    _step(loop, 32)
    assert [_up(_step(loop, seconds)) for seconds in (42, 54)] == ["bos-nyy:live", "atl-nym:live"]
    revealed = loop.refresh(_at(61))
    assert revealed.pipeline.ends == (
        (CardId("bos-nyy:final"), GameEnd(T + timedelta(seconds=31), EndBasis.FIRST_SEEN)),
    )
    assert loop.paint(_at(61)).changes == ()  # the other game's final waits for this card's turn
    handed = _step(loop, 62)  # its own result shows, after its minimum: it hands off at once
    assert _down(handed) == ("atl-nym:live", EndReason.INVALIDATED, timedelta(seconds=8))
    assert _up(handed) == "bos-nyy:final"  # the game that ended first goes first
    assert _up(_step(loop, 74)) == "atl-nym:final"


def test_a_predecessor_too_late_to_start_is_traced_and_holds_its_final_up_no_longer() -> None:
    loop, queue, _ = _loop(_Slate())
    walk_off = _card("col-lad", "walkoff", expires=5)  # 5 s left against an 8 s minimum
    final = CardFactory().final(_game("col-lad", GameStatus.FINAL), _state(), now=T, after=(walk_off.dedupe_key,))
    queue.ingest_all((walk_off, final))
    refresh = loop.refresh(_at(0))
    assert refresh.unseen == (Missed(walk_off, MissReason.TOO_LATE),)
    assert _up(loop.paint(_at(0))) == "col-lad:final"


def test_a_predecessor_that_failed_to_draw_holds_nothing_up_and_is_traced_with_why_when_it_runs_out() -> None:
    loop, queue, drawn = _loop(_Slate())
    walk_off = _card("col-lad", "walkoff", attention=_BURST, expires=30)
    final = CardFactory().final(_game("col-lad", GameStatus.FINAL), _state(), now=T, after=(walk_off.dedupe_key,))
    drawn.broken.add("col-lad:walkoff")
    queue.ingest_all((walk_off, final))
    failed = _step(loop, 0)
    assert failed.render_error is not None and _down(failed)[1] is EndReason.UNDRAWN
    assert _up(loop.paint(_at(0))) == "col-lad:final"
    assert loop.refresh_deadline(_at(0)) == MonotonicTime(22.0)  # when it can no longer start
    refresh = loop.refresh(_at(22.5))
    assert refresh.unseen == (Missed(walk_off, MissReason.UNDRAWN),)
