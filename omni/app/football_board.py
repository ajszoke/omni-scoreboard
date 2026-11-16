"""Follow one live football game: poll it, hold it back to the broadcast, and draw it.

The single-game loop the board runs until football joins the multi-league pipeline. Each `step` takes
whatever answers its feed has collected (`omni.app.football_feed`, which asks NFLMeta and the market
off the frame path), and draws the newest reading whose broadcast delay has passed. Readings are dated
from when each answer arrived, and released once the delay has passed since that arrival on the
monotonic clock, so a wall-clock jump can't release one early; the wall clock is only for what the card
shows. It draws every step, so a marqueeing strip keeps moving; the sink decides whether a frame is new
enough to send.

The play strip is a lane on the tower's shared primitives (`omni.tower`): each event becomes a segment
of the strip, in frames: its description, then the named players' lines for the game. Each frame gets
10 seconds, and 20 while nothing else is waiting; then the strip goes blank until the next event. A
stoppage (a timeout, a TV timeout, an injury, a review's ruling) is held instead, until play resumes.

A strip frame's time counts from when the display first showed it, by the display's own word: each frame
drawn is handed over with an id, and the board credits the strip frame (and the marquee's start) only
when the display says it showed that frame. A frame that failed to draw, or was replaced before it was
shown, keeps the strip frame's time. While the display is slow to answer, what it showed last stays up
and keeps its time. When the display goes away (its panel goes blank), the strip frame it showed loses
its credit, and gets its whole time again once a new display shows it. A display that never answers (an
older display process) is credited a few seconds in with each frame written to it, unconfirmed.

A caption the renderer can't draw comes off the strip, and that frame is drawn without it, so a broken
caption never holds up the strip or a break.

A field change shows at once. If its play comes with it, the play comes straight up; if not, what the
strip still shows no longer describes the field, so it clears (once its frame has had 10 seconds) and
the strip waits, blank, for the play.

A break (the end of a quarter, halftime, the final) is shown only once the plays that led to it have
been: the board keeps the field as it stood, with the new score, while each of those plays gets its 10
seconds in turn (a walk-off field goal, then FINAL). The close is bounded: the break shows by
`CLOSING_WAIT` after its reading is released, whether or not the plays have landed or the display is
showing frames. Room is kept for the period's last play (the decisive one), which keeps its 10 seconds
once it's up, even past the bound; older captions that don't fit before it are dropped as having
missed their air window. Through the break itself the strip stays empty.

A feed that stops answering is shown as stale: once no answer has come for `FEED_STALE_AFTER` past when
the next was due, the card dims, the strip clears (a stoppage held on it too) and the meter goes, until
the next answer is released. A halftime hold is quiet on purpose and never stale, nor is a final the
feed has parked on.

The market moves on a play as it happens, while the feed reports it 20 to 30 seconds later, so a fresh
price beside the feed's score would give the play away early. Each reading takes the market's price
from `meter_lag` before it instead, and none at all once the market has been quiet for `METER_MAX_AGE`
past that. The price is aged again at each draw, on the broadcast's clock, so a reading that stays up
(an outage, a hold) loses its meter once its price is that old, and a stale feed shows none. The lag is
measured from when each price was asked for, not from when the market set it, so it can still run
ahead of the feed: the meter is opt-in until it's anchored to the market's own clock.

The log keeps apart what arrives (`>`), what the strip puts up (`=`, when the display shows it), what it
clears early (`x`: the field moved on without its play, the feed went stale, it failed to draw), what a
display that went away took off the panel (`~`), and what it drops unseen (`-`), with notes on the feed
itself (`!`).
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
from omni.app.football_feed import AFTER_FINAL, RETRY_AFTER_FAILURE, FeedResult, FeedSource
from omni.app.frame_stream import AcknowledgingSink, DisplayLost, DisplayReceipt, FrameShown
from omni.cards.base import CardId, CardKind, CardPriority, DedupeKey, DisplayTiming, LayoutSupport, ScoreboardCard
from omni.cards.football import LiveFootballCardPayload
from omni.core.enum import DisplayPriority, PanelProfile
from omni.core.time import DurationSeconds
from omni.domain.baseball import WinProbability
from omni.domain.football import FootballGameState, PlaySummary
from omni.events.football import FootballEvent, FootballEventKind
from omni.providers.nflmeta import NflLiveSnapshot
from omni.renderers.base import Renderer
from omni.renderers.canvas import Canvas, DimmedCanvas
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.live_football import NO_STRIP_NOTE, STRIP_PROFILES, LiveFootballRenderer
from omni.renderers.text_lane import DEFAULT_MARQUEE, LaneClock, LaneFrame, Marquee, MarqueeStyle
from omni.tower import ChangeKind, Cue, Dwell, EndReason, Hold, Lane, LaneChange, Provenance, Segment, SegmentKey
from omni.tower import Surface, earliest

__all__ = [
    "CLOSING_WAIT",
    "FEED_STALE_AFTER",
    "METER_LAG",
    "METER_MAX_AGE",
    "FRAME_MAX",
    "FRAME_MIN",
    "STRIP_DWELL",
    "UNCONFIRMED_NOTE",
    "UNSHOWN_AFTER",
    "FootballBoard",
]

FRAME_MIN = timedelta(seconds=10)  # every frame of the strip (a description, a players' line) gets this
FRAME_MAX = timedelta(seconds=20)  # and keeps going to this while nothing else is pending
METER_LAG = timedelta(seconds=15)
METER_MAX_AGE = timedelta(seconds=60)  # a price this much older than its lag is no reading at all
CLOSING_WAIT = timedelta(seconds=30)  # a break shows by this long after its reading is released
# How late the feed's next answer can be before the board shows it as stale. One failed ask waits
# `RETRY_AFTER_FAILURE` to try again, so a single failure stays inside it.
FEED_STALE_AFTER = RETRY_AFTER_FAILURE + timedelta(seconds=10)
STRIP_DWELL = Dwell(minimum=FRAME_MIN, maximum=FRAME_MAX)
UNSHOWN_AFTER = timedelta(seconds=30)  # a frame the display hasn't shown this long after it was drawn
UNCONFIRMED_NOTE = (
    "display: no frame acknowledgements; showing frames unconfirmed (update board_frame_sink.py on the board)"
)
# What can close a period's story on the strip: a play, a score, a try, a ruling. Not a stoppage or an injury.
_PLAYS = frozenset(
    {FootballEventKind.PLAY, FootballEventKind.SCORE, FootballEventKind.CONVERSION, FootballEventKind.REVIEW}
)
# What comes down early, and how the log words it.
_EARLY = frozenset({EndReason.INVALIDATED, EndReason.SOURCE_STALE, EndReason.UNDRAWN})
_CLEARED_WORDS = {EndReason.INVALIDATED: "the field moved on without its play"}


@dataclass(frozen=True, slots=True)
class _Price:
    """The market's price a reading carries, and when it was asked for, on the monotonic clock."""

    probability: WinProbability | None
    asked: MonotonicTime


@dataclass(frozen=True, slots=True)
class _Reading:
    """A poll's answer, when it arrived, and when the broadcast catches up to it."""

    received_at: datetime
    release_at: MonotonicTime  # its arrival plus the delay, on the monotonic clock: when it's released
    show_at: datetime  # the same moment on the wall clock, for what the card shows
    snapshot: NflLiveSnapshot
    price: _Price | None
    next_in: timedelta  # how soon after it the feed said it would answer again
    held: bool = False  # a halftime hold's repeat: it says only that the feed is quiet on purpose


@dataclass(frozen=True, slots=True)
class _Handed:
    """A frame handed to the display, and what to credit once the display shows it."""

    frame: int  # its id
    drawn: ClockReading
    cue: Cue[PlaySummary] | None  # the strip frame it carried
    lanes: LaneFrame  # its marquee lines' starts
    lanes_version: int  # how many line starts had been committed when it was drawn


class _ShownOnCommit:
    """A display whose commit shows the frame before it returns: the word that it did comes at once."""

    def __init__(self, sink: DisplaySink) -> None:
        self._sink = sink
        self._last = 0
        self._shown: list[DisplayReceipt] = []

    @property
    def profile(self) -> PanelProfile:
        return self._sink.profile

    def new_frame(self) -> Canvas:
        return self._sink.new_frame()

    def send(self, frame: Canvas) -> int:
        self._sink.commit(frame)
        self._last += 1
        self._shown.append(FrameShown(self._last, generation=1))
        return self._last

    def receipts(self) -> list[DisplayReceipt]:
        shown, self._shown = self._shown, []
        return shown


class FootballBoard:
    """One game on one display."""

    def __init__(
        self,
        feed: FeedSource,
        sink: DisplaySink | AcknowledgingSink,
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
        self._display = sink if isinstance(sink, AcknowledgingSink) else _ShownOnCommit(sink)
        self._handed: deque[_Handed] = deque()  # frames handed over, not yet shown, oldest first
        self._unconfirmed = False  # the display has been credited without its word
        self._delay = delay
        self._meter_lag = meter_lag
        self._logos = logos
        self._timezone = timezone
        self._marquee = marquee  # how lines scroll
        self._lanes = LaneClock()  # when each line went up, by the strip frame it belongs to
        self._lanes_version = 0  # line starts committed so far
        self._log = log
        self._renderer: Renderer[LiveFootballCardPayload] = renderer or LiveFootballRenderer()
        self._strip: Lane[PlaySummary] = Lane(Surface.PLAY_STRIP)
        self._strip_drawn = self._display.profile in STRIP_PROFILES  # the small profiles' cards have no strip
        self._paint_error: str | None = None
        self._pending: deque[_Reading] = deque()
        self._showing: _Reading | None = None
        self._closing: _Reading | None = None  # a break, waiting for the plays that led to it
        self._closing_by: MonotonicTime | None = None  # when the break shows, whatever the strip is doing
        self._last_play: SegmentKey | None = None  # the newest play offered to the strip
        self._heard: _Reading | None = None  # the newest answer released, held repeats included
        self._holding = False
        self._stale = False  # the feed has gone quiet: the card is dimmed
        self._last_line: str | None = None
        self._prices: deque[_Price] = deque()  # the market's readings, oldest first

    @property
    def showing(self) -> NflLiveSnapshot | None:
        return self._showing.snapshot if self._showing is not None else None

    def step(self, at: ClockReading) -> None:
        """Take what the display showed and the feed's new answers, then draw what the broadcast has caught
        up to."""
        self._credit(at)
        for result in self._feed.collect(at):
            self._absorb(result, at)
        while self._pending and self._pending[0].release_at <= at.monotonic:
            reading = self._pending.popleft()
            self._heard = reading
            if not reading.held:
                self._take(reading, at)
        stale = self._check_stale(at)
        cue = self._strip.cue(at)
        closing, by = self._closing, self._closing_by
        if closing is not None and by is not None:
            landed = closing.snapshot.caught_up and self._strip.idle
            if landed or (at.monotonic >= by and not self._strip.keeping(at)):
                self._showing, self._closing = closing, None
                self._dropped(self._strip.clear(at), EndReason.MISSED_WINDOW.value)
                cue = None
        showing = self._showing
        if showing is not None and showing.snapshot.state is not None:
            if self._closing is not None and self._closing.snapshot.state is not None:
                view = _closing_view(showing.snapshot.state, self._closing.snapshot.state)
                self._draw(self._closing, view, cue, at, stale=stale)
            elif showing.snapshot.state.interval is None:
                self._draw(showing, showing.snapshot.state, cue, at, stale=stale)
            else:
                self._draw(showing, showing.snapshot.state, None, at, stale=stale)
        self._credit(at)  # a display that shows a frame as it takes it already has
        self._trace(self._strip.changes())

    def next_deadline(self, at: ClockReading) -> MonotonicTime | None:
        """When the board will next decide something by itself, as of ``at``: a strip frame's time running
        out, a reading clearing the broadcast delay, a break's bound, or the feed going stale. New answers
        from the feed come on top of these."""
        deadlines = [self._strip.next_deadline(at)]
        if self._pending:
            deadlines.append(self._pending[0].release_at)
        if self._closing is not None:
            deadlines.append(self._closing_by)
        if not self._stale:
            deadlines.append(self._stale_at())
        return earliest(deadlines)

    def _stale_at(self) -> MonotonicTime | None:
        """When the feed goes stale if nothing more is heard: past when its next answer was due, by
        `FEED_STALE_AFTER`. Never while a halftime hold keeps it quiet on purpose."""
        heard = self._heard
        if heard is None or self._holding:
            return None
        return heard.release_at + heard.next_in + FEED_STALE_AFTER

    def _check_stale(self, at: ClockReading) -> bool:
        """Whether the feed is stale as of ``at``. Going stale clears the strip (a held stoppage too); the
        next answer released undoes it."""
        stale_at = self._stale_at()
        stale = stale_at is not None and at.monotonic >= stale_at
        if stale and not self._stale:
            self._log("feed stale: no answer; dimming the card until it answers")
            self._dropped(self._strip.clear(at, EndReason.SOURCE_STALE), EndReason.SOURCE_STALE.value)
        elif self._stale and not stale:
            self._log("feed answering again")
        self._stale = stale
        return stale

    def _take(self, reading: _Reading, at: ClockReading) -> None:
        """A reading the broadcast has caught up to: show it, or close out the period it ends."""
        state = reading.snapshot.state
        shown = self._showing.snapshot.state if self._showing is not None else None
        if state is not None and state.interval is not None:
            if self._closing is not None:
                self._closing = reading  # still closing out: the newest break, and any plays it brings
                self._offer(reading, at)
                self._strip.close_out(by=self._closing_by, last=self._last_play)
            elif shown is not None and shown.interval is None:
                self._closing, self._closing_by = reading, reading.release_at + CLOSING_WAIT  # its plays first
                self._offer(reading, at)
                self._strip.close_out(by=self._closing_by, last=self._last_play)
            else:
                self._showing = reading  # already in a break, or starting in one: the strip stays empty
                self._dropped(self._strip.clear(at) + self._segments(reading, at), "during a break")
            return
        self._closing = None
        self._strip.resume()
        if shown is not None and state is not None and _field(state) != _field(shown):
            self._strip.invalidate()
        self._offer(reading, at)
        self._showing = reading

    def _offer(self, reading: _Reading, at: ClockReading) -> None:
        """Put a reading's events on the strip, noting the newest play among them."""
        segments = self._segments(reading, at)
        plays = [event.key for event in reading.snapshot.events if event.kind in _PLAYS]
        if segments and plays:
            self._last_play = SegmentKey(Surface.PLAY_STRIP, plays[-1])
        self._strip.offer(segments)

    def _segments(self, reading: _Reading, at: ClockReading) -> list[Segment[PlaySummary]]:
        """A reading's events for the strip, on a profile that draws one, available from its release."""
        return _segments(reading, min(reading.show_at, at.utc)) if self._strip_drawn else []

    def _dropped(self, segments: list[Segment[PlaySummary]], why: str) -> None:
        for segment in segments:
            self._log(f"  - {_words(segment.frames[0])} (unseen: {why})")

    def _credit(self, at: ClockReading) -> None:
        """Credit what the display says it showed since the last ask, as of ``at``, when the board learns
        of it: the strip frame each shown frame carried, and its marquee starts. A frame handed over before
        one that was shown, and not shown itself, was replaced: it's never credited."""
        for receipt in self._display.receipts():
            if isinstance(receipt, DisplayLost):
                self._handed.clear()  # anything in flight went to the display that's gone
                self._lanes, self._lanes_version = LaneClock(), self._lanes_version + 1  # lines restart
                self._strip.blanked(at)
                self._trace(self._strip.changes())
                continue
            if not receipt.confirmed and not self._unconfirmed:
                self._unconfirmed = True
                self._log(UNCONFIRMED_NOTE)
            handed = self._shown(receipt.frame)
            if handed is None:
                continue
            if handed.lanes_version == self._lanes_version:  # drawn on the starts now standing
                handed.lanes.commit()
                self._lanes_version += 1
            if handed.cue is not None:
                self._strip.committed(handed.cue, at)
            self._trace(self._strip.changes(), unconfirmed=not receipt.confirmed)
        while self._handed and at.monotonic.since(self._handed[0].drawn.monotonic) >= UNSHOWN_AFTER:
            self._handed.popleft()

    def _shown(self, frame: int) -> _Handed | None:
        """The frame handed over with id ``frame``, once shown; any handed over before it go unshown."""
        if all(handed.frame != frame for handed in self._handed):
            return None  # not one the board is waiting on (drawn before the display went away, or long ago)
        while self._handed[0].frame != frame:
            self._handed.popleft()
        return self._handed.popleft()

    def _trace(self, changes: list[LaneChange[PlaySummary]], *, unconfirmed: bool = False) -> None:
        """The strip's changes in the log: what went up, what came down early, what a display that went
        away lost, what went unseen."""
        mark = " (unconfirmed)" if unconfirmed else ""
        for change in changes:
            words = _words(change.segment.frames[0])
            if change.kind is ChangeKind.UP:
                self._log(f"  = {words}{mark}")
            elif change.kind is ChangeKind.AGAIN:
                self._log(f"  = {words} (again, on the new display){mark}")
            elif change.kind is ChangeKind.BLANKED:
                self._log(f"  ~ {words} (blanked: the display went away)")
            elif change.kind is ChangeKind.DOWN and change.reason is not None and change.reason in _EARLY:
                self._log(f"  x {words} (cleared: {_CLEARED_WORDS.get(change.reason, change.reason.value)})")
            elif change.kind is ChangeKind.DROPPED:
                why = change.reason.value if change.reason is not None else "dropped"
                self._log(f"  - {words} (unseen: {why})")

    def _absorb(self, result: FeedResult, at: ClockReading) -> None:
        """An answer from the feed: queue its reading behind the broadcast delay, and log what came."""
        if self._holding and not result.held:
            self._holding = False
            self._log("halftime hold over: polling again")
        snapshot = result.snapshot
        if snapshot is None:
            self._log(f"feed: {result.error}")
            return
        for note in snapshot.notes:
            self._log(f"  ! {note.kind.value}: {note.detail}")
        received = result.received_monotonic if result.received_monotonic is not None else at.monotonic
        price = self._probability(result, received)
        reading = _Reading(
            received_at=result.received_at,
            release_at=received + self._delay,
            show_at=result.received_at + self._delay,
            snapshot=snapshot,
            price=price,
            next_in=result.next_ask - result.asked_at,
            held=result.held,
        )
        self._pending.append(reading)
        if result.held:
            return
        probability = price.probability if price is not None else None
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
        if reading.next_in >= AFTER_FINAL:
            self._log("final: no more polling")

    def _probability(self, result: FeedResult, received: MonotonicTime) -> _Price | None:
        """The market's price from `meter_lag` before this answer arrived, if it's fresh enough."""
        quote = result.quote
        if quote is None:
            return None
        if quote.error is not None:
            self._log(f"market: {quote.error}")
        else:
            self._prices.append(_Price(quote.probability, received))
        while len(self._prices) > 1 and received.since(self._prices[1].asked) >= self._meter_lag:
            self._prices.popleft()
        if self._prices and self._meter_lag <= received.since(self._prices[0].asked) <= self._meter_lag + METER_MAX_AGE:
            return self._prices[0]
        return None  # nothing old enough yet, or the market has gone quiet

    def _meter(self, reading: _Reading, at: ClockReading, *, stale: bool) -> WinProbability | None:
        """The reading's price, as of ``at``: none from a stale feed, nor once the price is older, on the
        broadcast's clock, than its lag and `METER_MAX_AGE`."""
        price = reading.price
        if price is None or stale:
            return None
        if at.monotonic.since(price.asked) > self._delay + self._meter_lag + METER_MAX_AGE:
            return None
        return price.probability

    def _draw(
        self,
        reading: _Reading,
        state: FootballGameState,
        cue: Cue[PlaySummary] | None,
        at: ClockReading,
        *,
        stale: bool,
    ) -> None:
        """Draw the card and hand it to the display, noting the strip frame it carries for when it's shown."""
        try:
            frame, lanes, cue = self._paint(reading, state, cue, at, stale=stale)
            handed = self._display.send(frame)
        except Exception as exc:  # a frame nobody saw spends no strip time, and never stops the board
            error = f"{type(exc).__name__}: {exc}"
            if error != self._paint_error:
                self._paint_error = error
                self._log(f"paint failed: {error}")
            return
        if self._paint_error is not None:
            self._paint_error = None
            self._log("paint recovered")
        self._handed.append(_Handed(handed, at, cue, lanes, self._lanes_version))

    def _paint(
        self,
        reading: _Reading,
        state: FootballGameState,
        cue: Cue[PlaySummary] | None,
        at: ClockReading,
        *,
        stale: bool,
    ) -> tuple[Canvas, LaneFrame, Cue[PlaySummary] | None]:
        """The card drawn into a new frame, dimmed when the feed is stale, with the strip frame it carries.

        A caption the renderer can't draw comes off the strip, and the card is drawn again without it.
        """
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
                win_probability=self._meter(reading, at, stale=stale),
                break_ends_at=_delayed(reading.snapshot.break_ends_at, self._delay),
            ),
        )
        # The strip's lines belong to the strip frame up: a new one starts its lines at their heads, and
        # again once the display has shown it, so a frame shown late doesn't scroll in midway.
        presentation = (cue.segment.key, cue.frame, self._strip.dwelling) if cue is not None else None
        lanes = self._lanes.frame(presentation, at.monotonic)
        marquee = Marquee(style=self._marquee, clock=lanes)
        context = RenderContext(
            profile=self._display.profile, now=at.utc, logos=self._logos, timezone=self._timezone, marquee=marquee
        )
        frame = self._display.new_frame()
        try:
            self._renderer.render(card, context, DimmedCanvas(frame) if stale else frame)
        except Exception as exc:
            if cue is None:
                raise
            self._strip.failed(cue, at)
            self._log(f"strip: a caption failed to draw: {type(exc).__name__}: {exc}")
            self._trace(self._strip.changes())
            return self._paint(reading, state, None, at, stale=stale)
        return frame, lanes, cue


def _delayed(instant: datetime | None, delay: timedelta) -> datetime | None:
    """An instant on the feed's clock, on the broadcast's: when the viewer will see it."""
    return instant + delay if instant is not None else None


def _card_key(reading: _Reading) -> str:
    return f"nfl-{reading.snapshot.game.id.raw}:live"


def _segments(reading: _Reading, available: datetime) -> list[Segment[PlaySummary]]:
    """A reading's events as strip segments: each a description, then its players' lines if it has them."""
    game = reading.snapshot.game
    return [
        Segment(
            key=SegmentKey(Surface.PLAY_STRIP, event.key),
            frames=_frames(event),
            dwell=STRIP_DWELL,
            provenance=Provenance(source=game.id.source, received_at=reading.received_at),
            available_at=available,
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
