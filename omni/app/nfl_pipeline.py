"""LiveFootballPipeline: NFL's answers into TV-safe cards on the shared board.

The NFL counterpart of `omni.app.pipeline.LiveBaseballPipeline`, under the same rules wherever they apply. Each pass
the loop hands it the board's NFL games and a reading of the clock. It takes each live or final game's new answers
from its `NflFeeds`, each once and oldest first, so no play the feed delivered between two passes is lost, and cards
the game for its phase:

- **upcoming** — no card yet. No renderer draws an upcoming NFL game as it is: the pregame card is baseball's (it
  counts down to a "first pitch"), and the football card's kickoff state is a game already under way.
- **live** — a `LIVE_GAME` card drawn by the live football renderer, from the newest state to have cleared the TV
  delay, with when a halftime ends (delayed to the viewer's clock) so the break counts down. No win meter yet.
  - **The caption** (`last_play`) is the newest released play, for a while. A routine entry must start within 60 s
    of its release and is gone by 90 s; a major one (a score, a turnover, a ruling) by 120 s and 150 s
    (`NflCardPolicy`; `CLOCK_AND_SCHEDULING.md`, the strip's hard ends). Once a newer answer's field has moved on
    from the one the play's own answer showed, the play no longer describes the field, and goes. In a break, or
    while the feed has gone quiet, the card shows the field alone.
  - The plays in a game's first answer are from before the board tuned in, so none of them captions the card.
- **final** — a result comes only from an answer that itself says the game is over. The first is held for the TV
  delay from its receipt; then a `FINAL` card, drawn by the same renderer in its final state, lives out the
  post-game window (30 minutes, as baseball's). It names the game's live card as the one it replaces, so a board
  showing that card hands off rather than cutting it, and finals air in the order their games ended (by when the
  feed first said so). Until it reveals, the live card carries on from the states already in the delay. A later
  final answer with a different result is a new observation, held through the delay from its own receipt, and then
  corrects the card in place, within the card's own window; a final whose window is over stays gone. While the
  schedule calls a game final and its feed doesn't yet, the live card carries on, and goes stale `confirm_within`
  after the schedule first said so.

**Release.** Every hold (a state, a play, a result) is pinned when it is admitted, on the monotonic clock, by the
board's `ReleaseGuard` for NFL's own delay: a play whose write time is credible at receipt + the delay less its age,
told on the source's own clock, and everything else at receipt + the full delay. A wall-clock jump after that changes
nothing already held.

**Staleness.** A live card is current while its game's newest answer is: for that answer's `current_for` from its
arrival when the source says, and otherwise for two of the cadence the answer recommends; through a halftime hold,
until the break the answer names is over and that long again. A live game with no answer at all is stale at once.
Once not current, the card is marked stale (drawn dimmed), and `stale_for` after it, it comes down until a newer
state clears the delay. It is measured on the monotonic clock.

**Priority** is the scheduler's bands: a favorite's game (`Favorites`, for NFL's own clubs) is in the favorite band;
the others are normal.

A game that leaves the live and final phases has its live card dropped; one that leaves the slate has its final
dropped too. The pipeline reads only what the feeds already hold, so a refresh never waits on the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Iterable

from omni.app.football_board import MAJOR_HARD_END, MAJOR_START_WITHIN, ROUTINE_HARD_END, ROUTINE_START_WITHIN
from omni.app.nfl_feeds import NflFeeds, NflReading
from omni.app.pipeline import CONFIRM_WITHIN, STALE_FOR, PipelineResult
from omni.cards.base import (
    CardId,
    CardKind,
    CardPriority,
    DedupeKey,
    DisplayTiming,
    EndBasis,
    GameEnd,
    LayoutSupport,
    ScoreboardCard,
)
from omni.cards.football import LiveFootballCardPayload
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import DisplayPriority, GameStatus, PanelProfile
from omni.core.ids import LeagueScopedId
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.domain.football import FootballGameState, PlaySummary
from omni.events.football import FootballEvent
from omni.preferences.favorites import Favorites
from omni.providers.nflmeta import NflPhase
from omni.queue.delay_policy import Arrival, DelayPolicy, Held, ReleaseGuard
from omni.queue.delayed_event_stream import DelayedEventStream
from omni.queue.delayed_feed import DelayedFeed
from omni.queue.scheduler import InterleavedCardQueue
from omni.renderers.live_football import NO_STRIP_NOTE

__all__ = ["LiveFootballPipeline", "NflCardPolicy"]

# The live football renderer draws all three profiles; the stack and the single make do without the strip.
_LAYOUT = LayoutSupport(profiles=frozenset(PanelProfile), compromise_notes=(NO_STRIP_NOTE,))
_FAVORITE_SCORE = 30.0  # the scheduler's favorite weight, as baseball's scorer gives it
_NORMAL = CardPriority(band=DisplayPriority.NORMAL, score=0.0)


@dataclass(frozen=True, slots=True, kw_only=True)
class NflCardPolicy:
    """How NFL's cards are timed: their turns, the caption's lifetimes, and when a quiet feed's card dims and goes."""

    live_min_display: DurationSeconds = DurationSeconds(8)
    live_max_display: DurationSeconds = DurationSeconds(30)
    final_min_display: DurationSeconds = DurationSeconds(8)
    final_max_display: DurationSeconds = DurationSeconds(20)
    postgame_window: timedelta = timedelta(minutes=30)  # how long a final lingers after its reveal
    routine_start_within: timedelta = ROUTINE_START_WITHIN  # a routine caption not up by then never goes up
    routine_hard_end: timedelta = ROUTINE_HARD_END  # and is gone by then, up or not
    major_start_within: timedelta = MAJOR_START_WITHIN  # a score, a turnover, a ruling
    major_hard_end: timedelta = MAJOR_HARD_END
    current_cadences: int = 2  # an answer that doesn't say how long it's current is current for this many cadences
    stale_for: timedelta = STALE_FOR  # a stale live card comes down this long after it went stale
    confirm_within: timedelta = CONFIRM_WITHIN  # how long the schedule may call a game final before its feed must

    def caption_bounds(self, major: bool) -> tuple[timedelta, timedelta]:
        """How long after its release a caption may first go up, and when it's gone."""
        if major:
            return self.major_start_within, self.major_hard_end
        return self.routine_start_within, self.routine_hard_end


@dataclass(frozen=True, slots=True)
class _Field:
    """What a live answer puts on the card: the game's state, and when the break it's in ends, if it says."""

    state: FootballGameState
    break_ends_at: datetime | None


@dataclass(frozen=True, slots=True)
class _Play:
    """A play held in the delay: its place in the feed, and the answer that brought it."""

    order: int
    event: FootballEvent
    answer: MonotonicTime  # that answer's receipt
    field: tuple[object, ...] | None  # the field that answer showed: where the play left the game


@dataclass(slots=True)
class _Caption:
    """The play captioning a game's live card, from its release until its lifetime runs out."""

    play: _Play
    release: MonotonicTime
    started: bool = False  # it has been on the card


@dataclass(slots=True)
class _Live:
    """What the pipeline keeps of one NFL game while it cards the game live."""

    states: DelayedFeed[_Field]
    plays: DelayedEventStream[_Play]
    primed: bool = False  # its first answer's plays were set aside as from before the board tuned in
    offered: int = 0  # plays offered so far: each one's place in the feed
    shown: Held[_Field] | None = None  # the newest state to have cleared the delay
    caption: _Caption | None = None
    captioned: int = 0  # the place of the newest play to have captioned the card: no older one ever does
    card: str | None = None  # its live card's dedupe key, while that card is staged


@dataclass(slots=True)
class _Final:
    """A game the feed has said is over: its final answers through the delay, and its card once revealed."""

    answers: DelayedFeed[FootballGameState]
    seen: datetime  # when the first answer that said so arrived: the end, as far as the board can tell
    reveal: MonotonicTime  # when that answer clears the delay
    key: str | None = None  # its card's dedupe key, once revealed


@dataclass(slots=True)
class _Pass:
    """What one pass did, gathered as it goes."""

    ingested: list[CardId] = field(default_factory=list)
    finals: list[CardId] = field(default_factory=list)
    ends: list[tuple[CardId, GameEnd]] = field(default_factory=list)
    corrections: list[CardId] = field(default_factory=list)
    held: set[LeagueScopedId] = field(default_factory=set)
    releases: list[tuple[LeagueScopedId, datetime]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    carding: set[LeagueScopedId] = field(default_factory=set)  # games whose live card is kept up this pass
    over: set[LeagueScopedId] = field(default_factory=set)  # games over by their schedule or their feed


class LiveFootballPipeline:
    """Composes NFL answers → delay → cards → queue for the board's NFL games."""

    def __init__(
        self,
        *,
        feeds: NflFeeds,
        delay_policy: DelayPolicy,
        queue: InterleavedCardQueue,
        favorites: Favorites = Favorites(),
        policy: NflCardPolicy = NflCardPolicy(),
    ) -> None:
        self._feeds = feeds
        self._guard = ReleaseGuard(delay_policy)
        self._queue = queue
        self._favorites = favorites
        self._policy = policy
        self._live: dict[LeagueScopedId, _Live] = {}  # games carded live, or holding their live card for a final
        self._finals: dict[LeagueScopedId, _Final] = {}  # games whose feed has said they're over
        self._unconfirmed: dict[LeagueScopedId, MonotonicTime] = {}  # final by the schedule, not yet the feed: since
        self._stale: dict[LeagueScopedId, MonotonicTime] = {}  # when a game's live card stopped being current
        self._current_until: dict[LeagueScopedId, MonotonicTime] = {}  # when a game's newest answer stops being

    def refresh(self, games: Iterable[TeamGame], *, at: ClockReading) -> PipelineResult:
        """Take each live or final game's new answers as of ``at``, surface its phase's card into the queue, and drop
        what's done.

        Another game's answers wait in its feed: one the feed already calls live while the slate hasn't caught up
        is taken, from its first answer, once the slate says so.
        """
        self._guard.observe(at)
        out = _Pass()
        for game in games:
            if game.status is GameStatus.LIVE or game.status is GameStatus.FINAL:
                self._card(game, self._feeds.take(game), at, out)
        removed = self._drop(out)
        return PipelineResult(
            pregames=(),
            statuses=(),
            ingested=tuple(out.ingested),
            big_plays=(),
            no_hitters=(),
            finals=tuple(out.finals),
            held=tuple(sorted(out.held, key=str)),
            removed=removed,
            skipped=tuple(out.skipped),
            warnings=tuple(out.warnings),
            stale=tuple(sorted(self._stale, key=str)),
            releases=tuple(sorted(out.releases, key=lambda release: str(release[0]))),
            ends=tuple(out.ends),
            unconfirmed=tuple(sorted(self._unconfirmed, key=str)),
            corrections=tuple(out.corrections),
        )

    def next_deadline(self, at: ClockReading) -> MonotonicTime | None:
        """When the pipeline would next surface or drop something by itself, as of ``at``: a state, a play or a
        result clearing the delay, a caption's lifetime running out, a live card going stale, or a stale card
        coming down."""
        instants: list[MonotonicTime | None] = []
        for live in self._live.values():
            instants += [live.states.next_eligible(at), live.plays.next_eligible()]
            caption = live.caption
            if caption is not None:
                start_within, hard_end = self._policy.caption_bounds(caption.play.event.major)
                instants.append(caption.release + hard_end)
                if not caption.started:
                    instants.append(caption.release + start_within)
        instants += [final.answers.next_eligible(at) for final in self._finals.values()]
        instants += [since + self._policy.stale_for for since in self._stale.values()]
        instants += [until for game_id, until in self._current_until.items() if game_id not in self._stale]
        instants += [
            since + self._policy.confirm_within
            for game_id, since in self._unconfirmed.items()
            if game_id not in self._stale
        ]
        return min((instant for instant in instants if instant is not None and instant > at.monotonic), default=None)

    # --- one game -----------------------------------------------------------------------------------------------

    def _card(self, game: TeamGame, readings: list[NflReading], at: ClockReading, out: _Pass) -> None:
        """A live or final game: take its answers in, then card it for where it stands."""
        final = self._finals.get(game.id)
        if final is not None and final.key is not None:
            out.over.add(game.id)
            self._correct_final(final, readings, at, out)
            return
        live = self._live.setdefault(game.id, _Live(DelayedFeed(self._guard), DelayedEventStream(self._guard)))
        for reading in readings:
            self._admit(game, live, reading, at, out)
        final = self._finals.get(game.id)
        if final is not None or game.status is GameStatus.FINAL:
            out.over.add(game.id)
        if final is None:
            if game.status is GameStatus.FINAL:
                self._unconfirmed.setdefault(game.id, at.monotonic)  # the feed hasn't caught up with the schedule
            self._note_staleness(game, at, out)
        self._release_plays(live, at)
        self._surface_live(game, live, at, out)
        if final is None:
            self._enforce_staleness(game.id, live, at)
        else:
            self._reveal_final(game, final, live, at, out)
        if final is None or final.key is None:
            out.carding.add(game.id)

    def _admit(self, game: TeamGame, live: _Live, reading: NflReading, at: ClockReading, out: _Pass) -> None:
        """One answer: its plays into the delay, and its state as the field or, once the game is over, the result."""
        snapshot = reading.snapshot
        out.warnings.extend(f"{game.id}: {note.kind.value}: {note.detail}" for note in snapshot.notes)
        arrival = Arrival(received=reading.arrived, as_of=snapshot.observed_at)
        state = snapshot.state
        self._offer_plays(live, snapshot.events, _field_of(state) if state is not None else None, arrival, at)
        if state is None:
            return  # before kickoff: nothing to show
        if snapshot.phase is NflPhase.POST:
            self._confirm_final(game, state, arrival, at)
        elif game.id not in self._finals:
            live.states.push(_Field(state, snapshot.break_ends_at), arrival, at)

    @staticmethod
    def _offer_plays(
        live: _Live,
        events: tuple[FootballEvent, ...],
        field: tuple[object, ...] | None,
        arrival: Arrival,
        at: ClockReading,
    ) -> None:
        """Hold an answer's plays in the delay, each from its own write time when that's credible; the first
        answer's plays are from before the board tuned in, so they're set aside. ``field`` is the answer's."""
        if not live.primed:
            live.primed = True
            for event in events:
                live.plays.mark_seen(event.key)
            return
        for event in events:
            live.offered += 1
            play = _Play(live.offered, event, arrival.received.monotonic, field)
            live.plays.push(play, replace(arrival, occurred=event.written_at), at, key=event.key)

    @staticmethod
    def _release_plays(live: _Live, at: ClockReading) -> None:
        """The newest play to clear the delay becomes the caption; an older one never takes a newer one's place."""
        for held in live.plays.release(at):
            if held.value.order > live.captioned:
                live.captioned = held.value.order
                live.caption = _Caption(held.value, held.release)

    def _surface_live(self, game: TeamGame, live: _Live, at: ClockReading, out: _Pass) -> None:
        """Card the newest state to have cleared the delay, with its caption; report the game held while a newer
        one is still inside it."""
        safe = live.states.latest_eligible(at)
        if safe is not None:
            live.shown = safe
        else:
            release = live.states.next_eligible(at)
            if release is not None:
                out.held.add(game.id)  # a newer state is still inside the TV delay
                out.releases.append((game.id, at.project(release)))
        shown = live.shown
        if shown is None:
            return
        since = self._stale.get(game.id)
        if since is not None and at.monotonic.since(since) >= self._policy.stale_for:
            return  # its card is down for staleness: nothing goes back up until a newer state clears the delay
        card = self._live_card(game, shown.value, self._caption(live, shown, at, stale=since is not None), at)
        current = self._queue.card(card.dedupe_key.raw)
        recovered = current is not None and current.stale_since is not None and since is None and safe is not None
        if current is None or current.payload != card.payload or current.priority != card.priority or recovered:
            self._queue.ingest(card, at)
            out.ingested.append(card.id)
        live.card = card.dedupe_key.raw

    def _caption(self, live: _Live, shown: Held[_Field], at: ClockReading, *, stale: bool) -> PlaySummary | None:
        """The play to caption the card beside ``shown``, or None for the field alone.

        A caption goes for good once it has run out its lifetime, didn't start in time, or a newer answer's field has
        moved on from its own answer's. While the feed is quiet, or in a break, the card shows the field alone.
        """
        caption = live.caption
        if caption is None:
            return None
        state = shown.value.state
        start_within, hard_end = self._policy.caption_bounds(caption.play.event.major)
        over = at.monotonic >= caption.release + hard_end
        late = not caption.started and at.monotonic >= caption.release + start_within
        newer = shown.arrival.received.monotonic > caption.play.answer
        if over or late or (newer and _field_of(state) != caption.play.field):
            live.caption = None
            return None
        if stale or state.interval is not None:
            return None
        caption.started = True
        return caption.play.event.summary

    # --- the final --------------------------------------------------------------------------------------------

    def _confirm_final(self, game: TeamGame, state: FootballGameState, arrival: Arrival, at: ClockReading) -> None:
        """An answer that says the game is over: the first starts its reveal, and each is held through the delay."""
        final = self._finals.get(game.id)
        if final is not None:
            final.answers.push(state, arrival, at)
            return
        answers: DelayedFeed[FootballGameState] = DelayedFeed(self._guard)
        answers.push(state, arrival, at)
        self._finals[game.id] = _Final(answers, seen=arrival.received.utc, reveal=self._guard.pin(arrival, at))
        # The feed agrees with the schedule now, and the live card is held for the reveal.
        self._unconfirmed.pop(game.id, None)
        self._stale.pop(game.id, None)
        self._current_until.pop(game.id, None)

    def _reveal_final(self, game: TeamGame, final: _Final, live: _Live, at: ClockReading, out: _Pass) -> None:
        """Reveal the final card once its result clears the delay, or hold it (reported) while it can't yet."""
        shown = final.answers.latest_eligible(at)
        if shown is None:
            out.held.add(game.id)
            out.releases.append((game.id, at.project(final.reveal)))
            return
        ended = GameEnd(final.seen, EndBasis.FIRST_SEEN)
        available = at.project(final.reveal)
        card = ScoreboardCard(
            id=CardId(f"{game.id}:final"),
            kind=CardKind.FINAL,
            contest=game,
            timing=DisplayTiming(
                available_at=available,
                min_display=self._policy.final_min_display,
                max_display=self._policy.final_max_display,
                expires_at=available + self._policy.postgame_window,
            ),
            priority=_NORMAL,
            layout_support=_LAYOUT,
            dedupe_key=DedupeKey(f"{game.id}:final"),
            payload=LiveFootballCardPayload(state=shown.value),
            replaces=DedupeKey(live.card) if live.card is not None else None,
            ended=ended,
        )
        self._queue.ingest(card, at)
        final.key = card.dedupe_key.raw
        out.finals.append(card.id)
        out.ends.append((card.id, ended))

    def _correct_final(self, final: _Final, readings: list[NflReading], at: ClockReading, out: _Pass) -> None:
        """A revealed final: a later final answer with a different result corrects the card once it clears the
        delay, while the card's own window lasts."""
        assert final.key is not None
        card = self._queue.card(final.key)
        if card is None:
            return  # its window is over: a correction doesn't bring it back
        for reading in readings:
            snapshot = reading.snapshot
            if snapshot.phase is NflPhase.POST and snapshot.state is not None:
                final.answers.push(snapshot.state, Arrival(received=reading.arrived, as_of=snapshot.observed_at), at)
        newest = final.answers.latest_eligible(at)
        if newest is None:
            return
        shown: FootballGameState = card.payload.state
        if _result(newest.value) != _result(shown):
            self._queue.ingest(replace(card, payload=replace(card.payload, state=newest.value)), at)
            out.corrections.append(card.id)

    # --- staleness ------------------------------------------------------------------------------------------

    def _note_staleness(self, game: TeamGame, at: ClockReading, out: _Pass) -> None:
        """Whether a game's live card is current as of ``at``, and if not, since when.

        It stops being current when its newest answer stops being, at once when it has no answer at all, or, for a
        game the schedule calls final, `confirm_within` after the schedule first said so while its feed hasn't.
        A current answer makes it current again.
        """
        latest = self._feeds.latest(game)
        points: list[tuple[MonotonicTime, str]] = []
        if latest is None:
            out.skipped.append(f"{game.id}: no answer yet")
            points.append((at.monotonic, ""))  # reported as skipped
            self._current_until.pop(game.id, None)
        else:
            until = self._current_until[game.id] = self._current_until_of(latest)
            if until <= at.monotonic:
                points.append((until, "its feed's newest answer is out of date"))
        unconfirmed = self._unconfirmed.get(game.id)
        if unconfirmed is not None and unconfirmed + self._policy.confirm_within <= at.monotonic:
            points.append((unconfirmed + self._policy.confirm_within, "the schedule says final, and its feed doesn't"))
        if not points:
            self._stale.pop(game.id, None)
            return
        point, why = min(points, key=lambda candidate: candidate[0])
        since = self._stale.get(game.id)
        if since is not None and since <= point:
            return
        self._stale[game.id] = point
        if since is None and why:
            out.warnings.append(f"{game.id}: stale: {why}")

    def _current_until_of(self, reading: NflReading) -> MonotonicTime:
        """When an answer stops being current: its stated span from its arrival, or two of its cadences; through a
        halftime hold, not before the break it names is over and that span again."""
        snapshot = reading.snapshot
        span = reading.current_for
        if span is None:
            span = snapshot.poll_every.as_timedelta() * self._policy.current_cadences
        until = reading.arrived.monotonic + span
        ends = snapshot.break_ends_at
        if ends is not None:
            until = max(until, reading.arrived.monotonic + (ends - reading.arrived.utc) + span)
        return until

    def _enforce_staleness(self, game_id: LeagueScopedId, live: _Live, at: ClockReading) -> None:
        """A game whose live card isn't current: the card is marked stale, and comes down `stale_for` after."""
        since = self._stale.get(game_id)
        if since is None or live.card is None:
            return
        if at.monotonic.since(since) >= self._policy.stale_for:
            self._queue.remove(live.card)
            live.card, live.shown, live.caption = None, None, None
            return
        card = self._queue.card(live.card)
        if card is not None and card.stale_since is None:
            self._queue.ingest(replace(card, stale_since=at.project(since)), at)

    # --- teardown ---------------------------------------------------------------------------------------------

    def _drop(self, out: _Pass) -> tuple[LeagueScopedId, ...]:
        """Drop what the games this pass left behind; return the games whose live card came down.

        A game no longer carded live (its final revealed, or it left the live and final phases) loses its live card
        and what was kept for it. A game no longer over by its schedule or its feed (it left the slate) loses its
        final too.
        """
        removed: list[LeagueScopedId] = []
        for game_id in set(self._live) - out.carding:
            key = self._live.pop(game_id).card
            if key is not None:
                self._queue.remove(key)
                removed.append(game_id)
        for game_id in set(self._finals) - out.over:
            key = self._finals.pop(game_id).key
            if key is not None:
                self._queue.remove(key)
        for game_id in set(self._unconfirmed) - out.over:
            del self._unconfirmed[game_id]
        for game_id in set(self._stale) - out.carding:
            del self._stale[game_id]
        for game_id in set(self._current_until) - out.carding:
            del self._current_until[game_id]
        return tuple(sorted(removed, key=str))

    # --- cards ------------------------------------------------------------------------------------------------

    def _live_card(
        self, game: TeamGame, shown: _Field, last_play: PlaySummary | None, at: ClockReading
    ) -> ScoreboardCard[LiveFootballCardPayload]:
        """A live card for ``game`` showing ``shown``, its halftime counting down on the viewer's clock."""
        ends = shown.break_ends_at
        lag = self._guard.policy.broadcast_lag.as_timedelta()
        key = f"{game.id}:live"
        return ScoreboardCard(
            id=CardId(key),
            kind=CardKind.LIVE_GAME,
            contest=game,
            timing=DisplayTiming(
                available_at=at.utc,
                min_display=self._policy.live_min_display,
                max_display=self._policy.live_max_display,
            ),
            priority=self._priority(game),
            layout_support=_LAYOUT,
            dedupe_key=DedupeKey(key),
            payload=LiveFootballCardPayload(
                state=shown.state, last_play=last_play, break_ends_at=ends + lag if ends is not None else None
            ),
        )

    def _priority(self, game: TeamGame) -> CardPriority:
        """The favorite band for a game with one of NFL's favorites in it, naming the better-ranked; else normal."""
        rank = self._favorites.of(game)
        if rank is None:
            return _NORMAL
        leader = game.away if self._favorites.rank(game.away) == rank else game.home
        return CardPriority(
            band=DisplayPriority.FAVORITE,
            score=_FAVORITE_SCORE,
            reasons=(f"favorite #{rank} {leader.abbreviation}",),
        )


def _field_of(state: FootballGameState) -> tuple[object, ...]:
    """What the field shows: the score, the period or break, and the next snap. The clock alone doesn't count."""
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


def _result(state: FootballGameState) -> tuple[int, int, int]:
    """What a final card says the result was: the score, and the period it ended in (overtime or not)."""
    return state.away_score, state.home_score, state.period
