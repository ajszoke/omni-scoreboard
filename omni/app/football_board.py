"""Follow one live football game: poll it, hold it back to the broadcast, and draw it.

The single-game loop the board runs until football joins the multi-league pipeline. Each `step` takes
whatever answers its feed has collected (`omni.app.football_feed`, which asks NFLMeta and the market
off the frame path), and draws the newest reading whose broadcast delay has passed. Readings are dated
from when each answer arrived. It draws every step, so a marqueeing strip keeps moving; the sink
decides whether a frame is new enough to send.

The play strip keeps its own schedule (`PlayStrip`). An event shows in frames: its description, then
the named players' lines for the game. Each frame gets 10 seconds, and 20 while nothing else is
waiting; then the strip goes blank until the next event. A stoppage (a timeout, a TV timeout, an
injury, a review's ruling) stays up instead until play resumes.

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

The log keeps three things apart: what arrives (`>`), what the strip puts up (`=`), and what it drops
unseen (`-`), with notes on the feed itself (`!`).
"""

from __future__ import annotations

import dataclasses
from collections import deque
from enum import Enum
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

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
from omni.renderers.live_football import LiveFootballRenderer
from omni.renderers.text_lane import DEFAULT_MARQUEE, LaneClock, Marquee, MarqueeStyle

__all__ = [
    "CLOSING_WAIT",
    "METER_LAG",
    "METER_MAX_AGE",
    "FRAME_MAX",
    "FRAME_MIN",
    "FootballBoard",
    "PlayStrip",
]

FRAME_MIN = timedelta(seconds=10)  # every frame of the strip (a description, a players' line) gets this
FRAME_MAX = timedelta(seconds=20)  # and keeps going to this while nothing else is pending
METER_LAG = timedelta(seconds=15)
METER_MAX_AGE = timedelta(seconds=60)  # a price this much older than its lag is no reading at all
CLOSING_WAIT = timedelta(seconds=30)  # longest a break waits for the play that ended the period


class _Frame(Enum):
    """What of an event the strip shows: its description, then its players' lines."""

    DESCRIPTION = "description"
    PLAYERS = "players"


class PlayStrip:
    """What the play strip shows when: each event's frames in turn, then nothing until the next.

    Every frame gets `FRAME_MIN`, and keeps going to `FRAME_MAX` while nothing is pending: no event
    waiting, no field change, no break coming. Once something is, the frame ends at its minimum and the
    event with it, so a waiting play comes straight up, and a field that moved on its own leaves the
    strip blank until its play arrives. A stoppage holds until something is pending.
    """

    def __init__(self) -> None:
        self._waiting: deque[FootballEvent] = deque()
        self._seen: set[str] = set()
        self._current: FootballEvent | None = None
        self._frame = _Frame.DESCRIPTION
        self._frame_since: datetime | None = None
        self._stale = False  # the field has moved on since the current event went up
        self._brief = False  # closing out a period: one frame each, at its minimum
        self._presented: list[FootballEvent] = []  # put up since the last `presented`

    def add(self, events: Iterable[FootballEvent]) -> None:
        """Queue events behind any still waiting; one already queued is ignored."""
        for event in events:
            if event.key not in self._seen:
                self._seen.add(event.key)
                self._waiting.append(event)

    def field_changed(self) -> None:
        """The scoreboard moved, so what's up goes as soon as its frame has had its minimum."""
        self._stale = True

    def close_out(self) -> None:
        """A break is coming: what's up and what's waiting get one frame each, at its minimum."""
        self._stale = self._brief = True

    def resume(self) -> None:
        """Play is on again: frames get their full time."""
        self._brief = False

    def clear(self) -> list[FootballEvent]:
        """Empty the strip; what was still waiting, and so goes unseen."""
        dropped = list(self._waiting)
        self._waiting.clear()
        self._current = None
        return dropped

    def presented(self) -> list[FootballEvent]:
        """The events put up since the last ask, oldest first."""
        presented, self._presented = self._presented, []
        return presented

    @property
    def idle(self) -> bool:
        """Nothing up and nothing waiting, as of the last `showing`."""
        return self._current is None and not self._waiting

    def showing(self, now: datetime) -> PlaySummary | None:
        """The strip at ``now``: an event's description, its players' lines, or nothing."""
        current = self._current
        if current is not None and self._frame_since is not None:
            pending = bool(self._waiting) or self._stale or self._brief
            holds = current.kind.spans and not self._brief
            if now - self._frame_since >= FRAME_MIN and pending:
                self._current = None
            elif now - self._frame_since >= FRAME_MAX and not holds:
                if self._frame is _Frame.DESCRIPTION and current.players_line:
                    # The players' frame starts when the description's ran out, however late this is asked.
                    self._frame, self._frame_since = _Frame.PLAYERS, self._frame_since + FRAME_MAX
                    if now - self._frame_since >= FRAME_MAX:
                        self._current = None
                else:
                    self._current = None
        if self._current is None and self._waiting:
            self._current, self._frame, self._frame_since = self._waiting.popleft(), _Frame.DESCRIPTION, now
            self._stale = False
            self._presented.append(self._current)
        if self._current is None:
            return None
        if self._frame is _Frame.PLAYERS:
            return dataclasses.replace(self._current.summary, detail=self._current.players_line)
        return self._current.summary


@dataclass(frozen=True, slots=True)
class _Reading:
    """A poll's answer, and when the broadcast catches up to it."""

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
        self._marquee = Marquee(style=marquee, clock=LaneClock())  # one memory of lines for the board's life
        self._log = log
        self._renderer: Renderer[LiveFootballCardPayload] = renderer or LiveFootballRenderer()
        self._strip = PlayStrip()
        self._pending: deque[_Reading] = deque()
        self._showing: _Reading | None = None
        self._closing: _Reading | None = None  # a break, waiting for the plays that led to it
        self._closing_since: datetime | None = None
        self._holding = False
        self._last_line: str | None = None
        self._prices: deque[tuple[datetime, WinProbability | None]] = deque()  # the market's readings, oldest first

    @property
    def showing(self) -> NflLiveSnapshot | None:
        return self._showing.snapshot if self._showing is not None else None

    def step(self, now: datetime) -> None:
        """Take the feed's new answers, then draw what the broadcast has caught up to."""
        for result in self._feed.collect(now):
            self._absorb(result)
        while self._pending and self._pending[0].show_at <= now:
            self._take(self._pending.popleft(), now)
        lines = self._strip.showing(now)
        closing = self._closing
        if closing is not None and self._strip.idle and self._closing_since is not None:
            if closing.snapshot.caught_up or now - self._closing_since >= CLOSING_WAIT:
                self._showing, self._closing = closing, None
                self._dropped(self._strip.clear(), "the break began")
                lines = None
        for event in self._strip.presented():
            self._log(f"  = {_words(event)}")
        showing = self._showing
        if showing is None or showing.snapshot.state is None:
            return
        if self._closing is not None and self._closing.snapshot.state is not None:
            self._draw(self._closing, _closing_view(showing.snapshot.state, self._closing.snapshot.state), lines, now)
        elif showing.snapshot.state.interval is None:
            self._draw(showing, showing.snapshot.state, lines, now)
        else:
            self._draw(showing, showing.snapshot.state, None, now)

    def _take(self, reading: _Reading, now: datetime) -> None:
        """A reading the broadcast has caught up to: show it, or close out the period it ends."""
        state = reading.snapshot.state
        shown = self._showing.snapshot.state if self._showing is not None else None
        if state is not None and state.interval is not None:
            if self._closing is not None:
                self._closing = reading  # still closing out: the newest break, and any plays it brings
                self._strip.add(reading.snapshot.events)
            elif shown is not None and shown.interval is None:
                self._closing, self._closing_since = reading, now  # the period just ended: its plays first
                self._strip.close_out()
                self._strip.add(reading.snapshot.events)
            else:
                self._showing = reading  # already in a break, or starting in one: the strip stays empty
                self._dropped(self._strip.clear() + list(reading.snapshot.events), "during a break")
            return
        self._closing = None
        self._strip.resume()
        if shown is not None and state is not None and _field(state) != _field(shown):
            self._strip.field_changed()
        self._strip.add(reading.snapshot.events)
        self._showing = reading

    def _dropped(self, events: list[FootballEvent], why: str) -> None:
        for event in events:
            self._log(f"  - {_words(event)} (unseen: {why})")

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
        self._pending.append(_Reading(result.received_at + self._delay, snapshot, probability))
        meter = f" | meter {probability.away:.0f}-{probability.home:.0f}" if probability is not None else ""
        line = _describe(snapshot) + meter
        if line != self._last_line:
            self._last_line = line
            self._log(line)
        for event in snapshot.events:
            self._log(f"  > {_words(event)}")
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

    def _draw(self, reading: _Reading, state: FootballGameState, strip: PlaySummary | None, now: datetime) -> None:
        game = reading.snapshot.game
        key = f"nfl-{game.id.raw}:live"
        card = ScoreboardCard(
            id=CardId(key),
            kind=CardKind.LIVE_GAME,
            contest=game,
            timing=DisplayTiming(
                available_at=reading.show_at, min_display=DurationSeconds(5), max_display=DurationSeconds(30)
            ),
            priority=CardPriority(band=DisplayPriority.FAVORITE, score=50.0),
            layout_support=LayoutSupport(profiles=frozenset(PanelProfile)),
            dedupe_key=DedupeKey(key),
            payload=LiveFootballCardPayload(
                state=state,
                last_play=strip,
                win_probability=reading.win_probability,
                break_ends_at=_delayed(reading.snapshot.break_ends_at, self._delay),
            ),
        )
        frame = self._sink.new_frame()
        context = RenderContext(
            profile=self._sink.profile, now=now, logos=self._logos, timezone=self._timezone, marquee=self._marquee
        )
        self._renderer.render(card, context, frame)
        self._sink.commit(frame)


def _delayed(instant: datetime | None, delay: timedelta) -> datetime | None:
    """An instant on the feed's clock, on the broadcast's: when the viewer will see it."""
    return instant + delay if instant is not None else None


def _words(event: FootballEvent) -> str:
    summary = event.summary
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
