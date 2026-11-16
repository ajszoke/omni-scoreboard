"""Follow one live football game: poll it, hold it back to the broadcast, and draw it.

The single-game loop the board runs until football joins the multi-league pipeline. Each `step` takes
whatever answers its feed has collected (`omni.app.football_feed`, which asks NFLMeta and the market
off the frame path), and draws the newest reading whose broadcast delay has passed. Readings are dated
from when each answer arrived. It draws every step, so a marqueeing strip keeps moving; the sink
decides whether a frame is new enough to send.

The play strip is a lane on the tower's shared primitives (`omni.tower`): each event becomes a segment
of the strip, in frames: its description, then the named players' lines for the game. Each frame gets
10 seconds, and 20 while nothing else is waiting; then the strip goes blank until the next event. A
stoppage (a timeout, a TV timeout, an injury, a review's ruling) is held instead, until play resumes.
A frame's time counts from when the display first took it, so a frame that failed to draw keeps its
time.

A field change shows at once. If its play comes with it, the play comes straight up; if not, what the
strip still shows no longer describes the field, so it clears (once its frame has had 10 seconds) and
the strip waits, blank, for the play.

A break (the end of a quarter, halftime, the final) is shown only once the plays that led to it have
been: the board keeps the field as it stood, with the new score, while each of those plays gets its 10
seconds in turn (a walk-off field goal, then FINAL). It waits up to `CLOSING_WAIT` for the last of them
to land. Through the break itself the strip stays empty.

The market moves on a play as it happens, while the feed reports it 20 to 30 seconds later, so a fresh
price beside the feed's score would give the play away early. Each reading takes the market's price
from `meter_lag` before it instead, and none at all once the market has been quiet for `METER_MAX_AGE`
past that. The lag is measured from when each price was asked for, not from when the market set it, so
it can still run ahead of the feed: the meter is opt-in until it's anchored to the market's own clock.

The log keeps apart what arrives (`>`), what the strip puts up (`=`), what it clears early because the
field moved on without its play (`x`), and what it drops unseen (`-`), with notes on the feed itself
(`!`).
"""

from __future__ import annotations

import dataclasses
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from omni.core.clock import ClockReading, MonotonicTime
from omni.app.display import DisplaySink
from omni.app.football_feed import AFTER_FINAL, FeedResult, FeedSource
from omni.cards.base import CardId, CardKind, CardPriority, DedupeKey, DisplayTiming, LayoutSupport, ScoreboardCard
from omni.cards.football import LiveFootballCardPayload
from omni.core.enum import DisplayPriority, PanelProfile
from omni.core.time import DurationSeconds
from omni.domain.baseball import WinProbability
from omni.domain.football import FootballGameState, PlaySummary
from omni.events.football import FootballEvent
from omni.providers.nflmeta import NflLiveSnapshot
from omni.renderers.base import Renderer
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.live_football import NO_STRIP_NOTE, STRIP_PROFILES, LiveFootballRenderer
from omni.renderers.text_lane import DEFAULT_MARQUEE, LaneClock, Marquee, MarqueeStyle
from omni.tower import ChangeKind, Cue, Dwell, EndReason, Hold, Lane, LaneChange, Provenance, Segment, SegmentKey
from omni.tower import Surface, earliest

__all__ = [
    "CLOSING_WAIT",
    "METER_LAG",
    "METER_MAX_AGE",
    "FRAME_MAX",
    "FRAME_MIN",
    "STRIP_DWELL",
    "FootballBoard",
]

FRAME_MIN = timedelta(seconds=10)  # every frame of the strip (a description, a players' line) gets this
FRAME_MAX = timedelta(seconds=20)  # and keeps going to this while nothing else is pending
METER_LAG = timedelta(seconds=15)
METER_MAX_AGE = timedelta(seconds=60)  # a price this much older than its lag is no reading at all
CLOSING_WAIT = timedelta(seconds=30)  # longest a break waits for the play that ended the period
STRIP_DWELL = Dwell(minimum=FRAME_MIN, maximum=FRAME_MAX)


@dataclass(frozen=True, slots=True)
class _Reading:
    """A poll's answer, when it arrived, and when the broadcast catches up to it."""

    received_at: datetime
    show_at: datetime
    snapshot: NflLiveSnapshot
    win_probability: WinProbability | None


class FootballBoard:
    """One game on one display."""

    def __init__(
        self,
        feed: FeedSource,
        sink: DisplaySink,
        *,
        delay: timedelta = timedelta(0),
        meter_lag: timedelta = METER_LAG,
        logos: LogoStore | None = None,
        timezone: tzinfo | None = None,
        marquee: MarqueeStyle = DEFAULT_MARQUEE,
        log: Callable[[str], None] = print,
        renderer: Renderer[LiveFootballCardPayload] | None = None,
    ) -> None:
        self._feed = feed
        self._sink = sink
        self._delay = delay
        self._meter_lag = meter_lag
        self._logos = logos
        self._timezone = timezone
        self._marquee = marquee  # how lines scroll
        self._lanes = LaneClock()  # when each line went up, by the strip frame it belongs to
        self._log = log
        self._renderer: Renderer[LiveFootballCardPayload] = renderer or LiveFootballRenderer()
        self._strip: Lane[PlaySummary] = Lane(Surface.PLAY_STRIP)
        self._strip_drawn = sink.profile in STRIP_PROFILES  # the small profiles' cards have no strip to fill
        self._paint_error: str | None = None
        self._pending: deque[_Reading] = deque()
        self._showing: _Reading | None = None
        self._closing: _Reading | None = None  # a break, waiting for the plays that led to it
        self._closing_since: MonotonicTime | None = None
        self._holding = False
        self._last_line: str | None = None
        self._prices: deque[tuple[datetime, WinProbability | None]] = deque()  # the market's readings, oldest first

    @property
    def showing(self) -> NflLiveSnapshot | None:
        return self._showing.snapshot if self._showing is not None else None

    def step(self, at: ClockReading) -> None:
        """Take the feed's new answers, then draw what the broadcast has caught up to."""
        for result in self._feed.collect(at.utc):
            self._absorb(result)
        while self._pending and self._pending[0].show_at <= at.utc:
            self._take(self._pending.popleft(), at)
        cue = self._strip.cue(at)
        closing = self._closing
        if closing is not None and self._strip.idle and self._closing_since is not None:
            if closing.snapshot.caught_up or at.monotonic.since(self._closing_since) >= CLOSING_WAIT:
                self._showing, self._closing = closing, None
                self._dropped(self._strip.clear(at), "the break began")
                cue = None
        showing = self._showing
        if showing is not None and showing.snapshot.state is not None:
            if self._closing is not None and self._closing.snapshot.state is not None:
                view = _closing_view(showing.snapshot.state, self._closing.snapshot.state)
                self._draw(self._closing, view, cue, at)
            elif showing.snapshot.state.interval is None:
                self._draw(showing, showing.snapshot.state, cue, at)
            else:
                self._draw(showing, showing.snapshot.state, None, at)
        self._trace(self._strip.changes())

    def next_deadline(self, at: ClockReading) -> MonotonicTime | None:
        """When the board will next decide something by itself, as of ``at``: a strip frame's time running
        out, a reading clearing the broadcast delay, or a break's wait for its last play ending. New
        answers from the feed come on top of these."""
        deadlines = [self._strip.next_deadline(at)]
        if self._pending:
            deadlines.append(at.monotonic + max(timedelta(0), self._pending[0].show_at - at.utc))
        if self._closing is not None and self._closing_since is not None:
            deadlines.append(self._closing_since + CLOSING_WAIT)
        return earliest(deadlines)

    def _take(self, reading: _Reading, at: ClockReading) -> None:
        """A reading the broadcast has caught up to: show it, or close out the period it ends."""
        state = reading.snapshot.state
        shown = self._showing.snapshot.state if self._showing is not None else None
        if state is not None and state.interval is not None:
            if self._closing is not None:
                self._closing = reading  # still closing out: the newest break, and any plays it brings
                self._strip.offer(self._segments(reading))
            elif shown is not None and shown.interval is None:
                self._closing, self._closing_since = reading, at.monotonic  # the period just ended: its plays first
                self._strip.close_out()
                self._strip.offer(self._segments(reading))
            else:
                self._showing = reading  # already in a break, or starting in one: the strip stays empty
                self._dropped(self._strip.clear(at) + self._segments(reading), "during a break")
            return
        self._closing = None
        self._strip.resume()
        if shown is not None and state is not None and _field(state) != _field(shown):
            self._strip.invalidate()
        self._strip.offer(self._segments(reading))
        self._showing = reading

    def _segments(self, reading: _Reading) -> list[Segment[PlaySummary]]:
        """A reading's events for the strip, on a profile that draws one."""
        return _segments(reading) if self._strip_drawn else []

    def _dropped(self, segments: list[Segment[PlaySummary]], why: str) -> None:
        for segment in segments:
            self._log(f"  - {_words(segment.frames[0])} (unseen: {why})")

    def _trace(self, changes: list[LaneChange[PlaySummary]]) -> None:
        """The strip's changes in the log: what went up, what the field cleared early, what went unseen."""
        for change in changes:
            if change.kind is ChangeKind.UP:
                self._log(f"  = {_words(change.segment.frames[0])}")
            elif change.kind is ChangeKind.DOWN and change.reason is EndReason.INVALIDATED:
                self._log(f"  x {_words(change.segment.frames[0])} (cleared: the field moved on without its play)")
            elif change.kind is ChangeKind.DROPPED:
                why = change.reason.value if change.reason is not None else "dropped"
                self._log(f"  - {_words(change.segment.frames[0])} (unseen: {why})")

    def _absorb(self, result: FeedResult) -> None:
        """An answer from the feed: queue its reading behind the broadcast delay, and log what came."""
        snapshot = result.snapshot
        if snapshot is None:
            self._log(f"feed: {result.error}")
            return
        for note in snapshot.notes:
            self._log(f"  ! {note.kind.value}: {note.detail}")
        if result.held:
            return
        if self._holding:
            self._holding = False
            self._log("halftime hold over: polling again")
        probability = self._probability(result)
        self._pending.append(_Reading(result.received_at, result.received_at + self._delay, snapshot, probability))
        meter = f" | meter {probability.away:.0f}-{probability.home:.0f}" if probability is not None else ""
        line = _describe(snapshot) + meter
        if line != self._last_line:
            self._last_line = line
            self._log(line)
        for event in snapshot.events:
            self._log(f"  > {_words(event.summary)}")
        if result.holding:
            self._holding = True
            self._log("halftime: holding off the feed")
        if result.next_ask - result.asked_at >= AFTER_FINAL:
            self._log("final: no more polling")

    def _probability(self, result: FeedResult) -> WinProbability | None:
        """The market's price from `meter_lag` before this answer arrived, if it's fresh enough."""
        quote = result.quote
        if quote is None:
            return None
        if quote.error is not None:
            self._log(f"market: {quote.error}")
        else:
            self._prices.append((result.received_at, quote.probability))
        cutoff = result.received_at - self._meter_lag
        while len(self._prices) > 1 and self._prices[1][0] <= cutoff:
            self._prices.popleft()
        if self._prices and cutoff - METER_MAX_AGE <= self._prices[0][0] <= cutoff:
            return self._prices[0][1]
        return None  # nothing old enough yet, or the market has gone quiet

    def _draw(
        self, reading: _Reading, state: FootballGameState, cue: Cue[PlaySummary] | None, at: ClockReading
    ) -> None:
        """Draw the card, and tell the strip which of its frames went up."""
        game = reading.snapshot.game
        key = _card_key(reading)
        card = ScoreboardCard(
            id=CardId(key),
            kind=CardKind.LIVE_GAME,
            contest=game,
            timing=DisplayTiming(
                available_at=reading.show_at, min_display=DurationSeconds(5), max_display=DurationSeconds(30)
            ),
            priority=CardPriority(band=DisplayPriority.FAVORITE, score=50.0),
            layout_support=LayoutSupport(profiles=frozenset(PanelProfile), compromise_notes=(NO_STRIP_NOTE,)),
            dedupe_key=DedupeKey(key),
            payload=LiveFootballCardPayload(
                state=state,
                last_play=cue.payload if cue is not None else None,
                win_probability=reading.win_probability,
                break_ends_at=_delayed(reading.snapshot.break_ends_at, self._delay),
            ),
        )
        # The strip's lines belong to the strip frame up: a new one starts its lines at their heads.
        lanes = self._lanes.frame((cue.segment.key, cue.frame) if cue is not None else None, at.monotonic)
        marquee = Marquee(style=self._marquee, clock=lanes)
        context = RenderContext(
            profile=self._sink.profile, now=at.utc, logos=self._logos, timezone=self._timezone, marquee=marquee
        )
        try:
            frame = self._sink.new_frame()
            self._renderer.render(card, context, frame)
            self._sink.commit(frame)
        except Exception as exc:  # a frame nobody saw spends no strip time, and never stops the board
            error = f"{type(exc).__name__}: {exc}"
            if error != self._paint_error:
                self._paint_error = error
                self._log(f"paint failed: {error}")
            return
        if self._paint_error is not None:
            self._paint_error = None
            self._log("paint recovered")
        lanes.commit()
        if cue is not None:
            self._strip.committed(cue, at)


def _delayed(instant: datetime | None, delay: timedelta) -> datetime | None:
    """An instant on the feed's clock, on the broadcast's: when the viewer will see it."""
    return instant + delay if instant is not None else None


def _card_key(reading: _Reading) -> str:
    return f"nfl-{reading.snapshot.game.id.raw}:live"


def _segments(reading: _Reading) -> list[Segment[PlaySummary]]:
    """A reading's events as strip segments: each a description, then its players' lines if it has them."""
    game = reading.snapshot.game
    return [
        Segment(
            key=SegmentKey(Surface.PLAY_STRIP, event.key),
            frames=_frames(event),
            dwell=STRIP_DWELL,
            provenance=Provenance(source=game.id.source, received_at=reading.received_at),
            available_at=reading.show_at,
            parent=_card_key(reading),
            hold=Hold.PLAY_RESUMES if event.kind.spans else None,
        )
        for event in reading.snapshot.events
    ]


def _frames(event: FootballEvent) -> tuple[PlaySummary, ...]:
    if event.players_line:
        return event.summary, dataclasses.replace(event.summary, detail=event.players_line)
    return (event.summary,)


def _words(summary: PlaySummary) -> str:
    return summary.headline + (f" | {summary.detail}" if summary.detail else "")


def _closing_view(before: FootballGameState, at_break: FootballGameState) -> FootballGameState:
    """The field while a period's last plays are shown: the new score and clock, no next snap yet."""
    return dataclasses.replace(
        at_break,
        interval=None,
        possession=None,
        down=None,
        distance=None,
        yards_to_goal=None,
        set_piece=None,
        timeouts=before.timeouts,
        challenges=before.challenges,
    )


def _field(state: FootballGameState) -> tuple[Any, ...]:
    """What the field shows: the score, the period, and the next snap. The clock alone doesn't count."""
    return (
        state.period,
        state.interval,
        state.away_score,
        state.home_score,
        state.possession,
        state.down,
        state.distance,
        state.yards_to_goal,
        state.set_piece,
    )


def _describe(snapshot: NflLiveSnapshot) -> str:
    """A log line for a snapshot, written when it changes."""
    game, state = snapshot.game, snapshot.state
    teams = f"{game.away.abbreviation} @ {game.home.abbreviation}"
    if state is None:
        return f"{teams}: {snapshot.phase.value}"
    where = ""
    if state.interval is not None:
        where = f" | {state.interval.value}"
    elif state.set_piece is not None:
        where = f" | {state.set_piece.value}" + (f", {state.yards_to_goal} to go" if state.yards_to_goal else "")
    elif state.possession is not None and state.down is not None:
        where = f" | {state.possession.value} ball, {state.down}&{state.distance}, {state.yards_to_goal} to go"
    timeouts, challenges = state.timeouts, state.challenges
    pips = (
        f" | TO {timeouts.away}-{timeouts.home} CH {challenges.away}-{challenges.home}"
        if timeouts is not None and challenges is not None
        else ""
    )
    minutes, seconds = divmod(state.clock_seconds, 60)
    return (
        f"{teams} {state.away_score}-{state.home_score} Q{state.period} {minutes}:{seconds:02d}"
        f" [{snapshot.phase.value}]{where}{pips}"
    )
