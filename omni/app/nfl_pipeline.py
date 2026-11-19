"""LiveFootballPipeline: NFL's answers into TV-safe cards on the shared board.

The NFL counterpart of `omni.app.pipeline.LiveBaseballPipeline`, under the same rules wherever they apply. Each pass
the loop hands it the board's NFL games and a reading of the clock. It takes each live or final game's new answers
from its `NflFeeds`, each once and oldest first, so no play the feed delivered between two passes is lost, and cards
the game for its phase:

- **upcoming** — no card yet. No renderer draws an upcoming NFL game as it is: the pregame card is baseball's (it
  counts down to a "first pitch"), and the football card's kickoff state is a game already under way.
- **live** — a `LIVE_GAME` card drawn by the live football renderer, from the newest state to have cleared the TV
  delay, with when a halftime ends (delayed to the viewer's clock) so the break counts down. No win meter yet.
  - **The caption** (`last_play`) is the newest released play, staged with which entry it is and its bounds
    (`StagedCaption`). It starts only when a frame that carried it is confirmed shown (`presented`): a routine entry
    must start within 60 s of its release and is gone by 90 s; a major one (a score, a turnover, a ruling) by 120 s
    and 150 s (`NflCardPolicy`; `CLOCK_AND_SCHEDULING.md`, the strip's hard ends). One no confirmed frame carried by
    its latest start is dropped, and the pass says so (`CaptionMiss`): it's never shown late. Once a newer answer's
    field has moved on from the one the play's own answer showed, the play no longer describes the field, and goes.
    It airs only beside a field whose score agrees with its own answer's: until that field is released, the card
    shows the field alone, as it does in a break or while the feed has gone quiet.
  - The plays in a game's first answer are from before the board tuned in, and what an outage's catch-up brings is
    history: none of them captions the card, nor is offered.
- **final** — a result comes only from an answer that itself says the game is over. The first is held for the TV
  delay from its receipt; then a `FINAL` card, drawn by the same renderer in its final state, lives out the
  post-game window (30 minutes, as baseball's). It names the game's live card as the one it replaces, so a board
  showing that card hands off rather than cutting it, and finals air in the order their games ended (by when the
  feed first said so). It names the play that decided it, when that play was offered (`decided_by`, and its own card
  to air first). Until it reveals, the live card carries on from the states already in the delay. A later final
  answer with a different result is a new observation, held through the delay from its own receipt, and then
  corrects the card in place, within the card's own window; a final whose window is over stays gone. While the
  schedule calls a game final and its feed doesn't yet, the live card carries on, and goes stale `confirm_within`
  after the schedule first said so.
  - **The drain.** The feed is read for a while after the final, for late plays and fixes. A play it brings that
    decided the game (`InterruptClass.DECISIVE_LATE_WINNER`, read against the last live state) updates the revealed
    final once, as its caption and the play that decided it; one that can't (the final's window is over, or it
    already names one) is dropped, and the pass says why. Nothing the drain brings is offered for promotion.

**Release.** Every hold (a state, a play, a result) is pinned when it is admitted, on the monotonic clock, by the
board's `ReleaseGuard` for NFL's own delay: a play whose write time is credible at receipt + the delay less its age,
told on the source's own clock, and everything else at receipt + the full delay. A wall-clock jump after that changes
nothing already held.

**Staleness.** A live card is current while its game's newest answer is: for that answer's `current_for` from its
arrival when the source says, and otherwise for two of the cadence the answer recommends; through a halftime hold,
until the break the answer names is over and that long again. A live game with no answer at all is stale at once.
Once not current, the card is marked stale (drawn dimmed), and `stale_for` after it, it comes down. It's current again
only once a state its feed sent after it went stale has cleared the delay: until then, what it shows is from before.
It is measured on the monotonic clock.

**The policy's view.** Each pass ends with what the board's policy may read (`omni.policy.view.PolicyView`), from
released state only (`omni.app.policy_facts`):
- **each game carded live:** its leverage (the labeled proxy) from the newest state to have cleared the delay, whether
  its card is current, and the break and action evidence the detectors (`omni.events.nfl_breaks`) found in each
  answer, held through the delay with that answer's state. A stoppage row shows on one look only, so it's remembered
  until a later snap supersedes it. NFL has no records source yet: its records are unknown.
- **offers:** a released play whose class scores (`classify_football_event`, on the released states either side of
  it) is offered once the state after it has cleared the delay too. Each names a card of its own, fixed for that
  play's revision: the field its answer showed, with the play as its caption (a `BIG_PLAY` card the live football
  renderer draws). Its moment is the play it's about: a review's ruling is that play's next revision, and a try
  merges into its touchdown. Once a ruling on a play is released, the play's older card and offer are withdrawn.

**Notes.** What the feed says about a poll reaches the warnings in our own words alone (`FeedNote.ours`): its kind,
how many, and which entry by our own label. The feed's own words go only to a log stamped to expire.

**Priority** is the scheduler's bands: a favorite's game (`Favorites`, for NFL's own clubs) is in the favorite band;
the others are normal.

A game that leaves the live and final phases has its live card and its plays' cards dropped; one that leaves the
slate has its final dropped too. The pipeline reads only what the feeds already hold, so a refresh never waits on the
network.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable

from omni.app.football_board import MAJOR_HARD_END, MAJOR_START_WITHIN, ROUTINE_HARD_END, ROUTINE_START_WITHIN
from omni.app.nfl_feeds import NflFeeds, NflReading
from omni.app.pipeline import CONFIRM_WITHIN, STALE_FOR, PipelineResult
from omni.app.policy_facts import (
    OFFER_LIFETIME,
    AwaitingSuccessor,
    PlayContext,
    ReleasedBreaks,
    StandingOffers,
    interrupt_stakes,
    major_offer,
)
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
from omni.cards.football import LiveFootballCardPayload, PlayRef, StagedCaption
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import DisplayPriority, GameStatus, PanelProfile, StrEnumMixin
from omni.core.ids import LeagueScopedId
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.domain.football import FootballGameState
from omni.events.football import FootballEvent, FootballEventKind
from omni.events.nfl_breaks import nfl_break_evidence, nfl_latest_action
from omni.policy.interrupts import InterruptClass, classify_football_event
from omni.policy.leverage import football_proxy
from omni.policy.promotion import CandidateOrigin, PromotionKey
from omni.policy.view import GameFacts, PolicyView
from omni.preferences.favorites import Favorites
from omni.providers.nflmeta import FeedNote, FeedNoteKind, NflPhase
from omni.queue.delay_policy import Arrival, DelayPolicy, Held, ReleaseGuard
from omni.queue.delayed_event_stream import DelayedEventStream
from omni.queue.delayed_feed import DelayedFeed
from omni.queue.scheduler import InterleavedCardQueue
from omni.renderers.live_football import NO_STRIP_NOTE
from omni.tower.interruption import Interruption

__all__ = ["CaptionMiss", "LiveFootballPipeline", "NflCardPolicy"]

# The live football renderer draws all three profiles; the stack and the single make do without the strip.
_LAYOUT = LayoutSupport(profiles=frozenset(PanelProfile), compromise_notes=(NO_STRIP_NOTE,))
_FAVORITE_SCORE = 30.0  # the scheduler's favorite weight, as baseball's scorer gives it
_NORMAL = CardPriority(band=DisplayPriority.NORMAL, score=0.0)


class CaptionMiss(StrEnumMixin, str, Enum):
    """Why an entry staged to caption a card never did."""

    TOO_LATE = "too late to start"  # no confirmed frame carried it by its latest start
    FINAL_OVER = "its final's window is over"  # the drain brought the deciding play after its final had gone
    FINAL_DECIDED = "its final already names the play that decided it"


@dataclass(frozen=True, slots=True, kw_only=True)
class NflCardPolicy:
    """How NFL's cards are timed: their turns, the caption's lifetimes, a play's own card, and when a quiet feed's
    card dims and goes."""

    live_min_display: DurationSeconds = DurationSeconds(8)
    live_max_display: DurationSeconds = DurationSeconds(30)
    final_min_display: DurationSeconds = DurationSeconds(8)
    final_max_display: DurationSeconds = DurationSeconds(20)
    play_min_display: DurationSeconds = DurationSeconds(8)  # a play's own card: a promoted major's airtime
    play_max_display: DurationSeconds = DurationSeconds(12)
    play_window: timedelta = OFFER_LIFETIME  # and it's staged as long as its offer stands
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
    """A play held in the delay: its place in the feed, the answer that brought it, and the moment it is."""

    order: int
    event: FootballEvent
    answer: MonotonicTime  # that answer's receipt
    field: tuple[object, ...] | None  # the field that answer showed: where the play left the game
    context: PlayContext[FootballGameState]  # the released states either side of it, once they clear
    moment: PromotionKey  # the play it's about: a review's ruling and a try share their play's
    revision: int  # how many rulings that play has had by this entry

    @property
    def ref(self) -> PlayRef:
        return PlayRef(self.event.key, self.moment.play, self.revision)

    @property
    def score(self) -> tuple[int, int] | None:
        """The score its own answer showed: what a field beside its caption must agree with."""
        after = self.context.after
        return _score(after) if after is not None else None


@dataclass(frozen=True, slots=True)
class _ReleasedPlay:
    """A play that has cleared the delay: its game, the play, and when it cleared."""

    game: TeamGame
    play: _Play
    eligible: MonotonicTime


@dataclass(slots=True)
class _Caption:
    """An entry staged to caption a card, from its release until it's gone; started once a confirmed frame carried it
    by its latest start."""

    play: _Play
    staged: StagedCaption
    started: bool = False


@dataclass(frozen=True, slots=True)
class _PlayCard:
    """An offered play's own card: its dedupe key, the play, and whether that play decided the game."""

    key: str
    play: PlayRef
    decisive: bool


@dataclass(slots=True)
class _Live:
    """What the pipeline keeps of one NFL game's plays and field, while it cards the game live and through its final's
    drain."""

    game: TeamGame  # as the slate last listed it
    states: DelayedFeed[_Field]
    plays: DelayedEventStream[_Play]
    breaks: ReleasedBreaks  # its break and action evidence, held with the states
    primed: bool = False  # its first answer's plays were set aside as from before the board tuned in
    last: FootballGameState | None = None  # the newest live answer's state: the one before the next answer's plays
    rulings: dict[str, int] = field(default_factory=dict)  # each play's rulings so far, by the play it's about
    offered: int = 0  # plays offered so far: each one's place in the feed
    shown: Held[_Field] | None = None  # the newest state to have cleared the delay
    caption: _Caption | None = None
    captioned: int = 0  # the place of the newest play to have captioned the card: no older one ever does
    card: str | None = None  # its live card's dedupe key, while that card is staged
    awaiting: AwaitingSuccessor[_ReleasedPlay] = field(default_factory=AwaitingSuccessor)  # the state after?
    released: dict[str, int] = field(default_factory=dict)  # each play's newest revision released, by its moment
    cards: dict[str, _PlayCard] = field(default_factory=dict)  # each offered play's own card, by its moment


@dataclass(slots=True)
class _Final:
    """A game the feed has said is over: its final answers through the delay, and its card once revealed."""

    answers: DelayedFeed[FootballGameState]
    seen: datetime  # when the first answer that said so arrived: the end, as far as the board can tell
    reveal: MonotonicTime  # when that answer clears the delay
    key: str | None = None  # its card's dedupe key, once revealed
    live: _Live | None = None  # the game's plays, kept for the drain once revealed
    caption: _Caption | None = None  # a deciding play the drain brought, captioning the card
    decided: bool = False  # it names the play that decided it


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
        self._answering: set[LeagueScopedId] = set()  # stale games whose feed is current again: the outage is ending
        self._current_until: dict[LeagueScopedId, MonotonicTime] = {}  # when a game's newest answer stops being
        self._offers = StandingOffers()
        self._woke: MonotonicTime = MonotonicTime(0.0)  # the last host interruption's end, on the monotonic clock
        self._woken: dict[LeagueScopedId, Held[_Field] | None] = {}  # each game's field released before it
        self._lock = threading.Lock()  # `presented` comes from the frame thread
        self._presented: dict[tuple[LeagueScopedId, PlayRef], MonotonicTime] = (
            {}
        )  # first confirmed, since the last pass

    def refresh(self, games: Iterable[TeamGame], *, at: ClockReading) -> PipelineResult:
        """Take each live or final game's new answers as of ``at``, surface its phase's card into the queue, and drop
        what's done.

        Another game's answers wait in its feed: one the feed already calls live while the slate hasn't caught up
        is taken, from its first answer, once the slate says so.
        """
        self._guard.observe(at)
        self._take_presented()
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
            policy=self._policy_view(at),
        )

    def presented(self, card: ScoreboardCard[Any], at: ClockReading) -> None:
        """The display confirmed a frame of ``card`` shown at ``at``: a caption it carried first went up then, if that
        was before its latest start. Safe from the frame thread; the next refresh takes it in."""
        payload = card.payload
        if not isinstance(payload, LiveFootballCardPayload) or payload.last_play is None or payload.caption is None:
            return
        with self._lock:
            self._presented.setdefault((card.contest.id, payload.caption.play), at.monotonic)

    def next_deadline(self, at: ClockReading) -> MonotonicTime | None:
        """When the pipeline would next surface or drop something by itself, as of ``at``: a state, a play or a
        result clearing the delay, a caption's latest start or hard end, a live card going stale, or a stale card
        coming down."""
        instants: list[MonotonicTime | None] = []
        for live in self._live.values():
            instants += [live.states.next_eligible(at), live.plays.next_eligible(), *_bounds(live.caption)]
        for final in self._finals.values():
            instants += [final.answers.next_eligible(at), *_bounds(final.caption)]
            if final.live is not None:
                instants.append(final.live.plays.next_eligible())
        instants += [since + self._policy.stale_for for since in self._stale.values()]
        instants += [until for game_id, until in self._current_until.items() if game_id not in self._stale]
        instants += [
            since + self._policy.confirm_within
            for game_id, since in self._unconfirmed.items()
            if game_id not in self._stale
        ]
        return min((instant for instant in instants if instant is not None and instant > at.monotonic), default=None)

    def _take_presented(self) -> None:
        """Start each staged caption a confirmed frame carried by its latest start."""
        with self._lock:
            presented, self._presented = self._presented, {}
        for (game_id, play), shown in presented.items():
            for kept in (self._live.get(game_id), self._finals.get(game_id)):
                caption = kept.caption if kept is not None else None
                if caption is not None and caption.staged.play == play and shown < caption.staged.latest_start:
                    caption.started = True

    # --- one game -----------------------------------------------------------------------------------------------

    def _card(self, game: TeamGame, readings: list[NflReading], at: ClockReading, out: _Pass) -> None:
        """A live or final game: take its answers in, then card it for where it stands."""
        final = self._finals.get(game.id)
        if final is not None and final.key is not None:
            out.over.add(game.id)
            self._drain(game, final, readings, at, out)
            return
        live = self._live.get(game.id)
        if live is None:
            live = _Live(game, DelayedFeed(self._guard), DelayedEventStream(self._guard), ReleasedBreaks(self._guard))
            self._live[game.id] = live
        live.game = game
        # Answers taken while its feed is out of date end an outage: what they bring is a catch-up.
        outage = game.id in self._stale and game.id not in self._answering
        origin = CandidateOrigin.CATCH_UP if outage else CandidateOrigin.LIVE
        for reading in readings:
            self._admit(game, live, reading, at, out, origin)
        final = self._finals.get(game.id)
        if final is not None or game.status is GameStatus.FINAL:
            out.over.add(game.id)
        if final is None:
            if game.status is GameStatus.FINAL:
                self._unconfirmed.setdefault(game.id, at.monotonic)  # the feed hasn't caught up with the schedule
            self._note_staleness(game, at, out)
        self._release_plays(game, live, at, out)
        self._settle_plays(game, live, at, out)
        self._surface_live(game, live, at, out)
        if final is None:
            self._enforce_staleness(game.id, live, at)
        else:
            self._reveal_final(game, final, live, at, out)
        if final is None or final.key is None:
            out.carding.add(game.id)

    def _admit(
        self, game: TeamGame, live: _Live, reading: NflReading, at: ClockReading, out: _Pass, origin: CandidateOrigin
    ) -> None:
        """One answer: its plays into the delay, and its state as the field or, once the game is over, the result. A
        live state's break and action evidence is held with it. ``origin`` is where the answer came from."""
        snapshot = reading.snapshot
        out.warnings.extend(_noted(game.id, snapshot.notes))
        arrival = Arrival(received=reading.arrived, as_of=snapshot.observed_at)
        state = snapshot.state
        context = PlayContext(live.last, state, self._guard.pin(arrival, at), origin)
        self._offer_plays(
            game, live, snapshot.events, _field_of(state) if state is not None else None, arrival, at, context
        )
        if state is None:
            return  # before kickoff: nothing to show
        if snapshot.phase is NflPhase.POST:
            self._confirm_final(game, state, arrival, at)
        elif game.id not in self._finals:
            live.last = state  # a final's plays are read against the last live state
            live.states.push(_Field(state, snapshot.break_ends_at), arrival, at)
            evidence = nfl_break_evidence(snapshot, reading.arrived)
            live.breaks.push(evidence, nfl_latest_action(snapshot, reading.arrived), arrival, at)

    @staticmethod
    def _offer_plays(
        game: TeamGame,
        live: _Live,
        events: tuple[FootballEvent, ...],
        field: tuple[object, ...] | None,
        arrival: Arrival,
        at: ClockReading,
        context: PlayContext[FootballGameState],
    ) -> None:
        """Hold an answer's plays in the delay, each from its own write time when that's credible; the first
        answer's plays are from before the board tuned in, so they're set aside. ``field`` is the answer's, and
        ``context`` its place in the game. A review's ruling is a new revision of the play it rules on."""
        if not live.primed:
            live.primed = True
            for event in events:
                live.plays.mark_seen(event.key)
            return
        for event in events:
            live.offered += 1
            about = str(event.play) if event.play is not None else event.key
            ruled = event.kind is FootballEventKind.REVIEW
            revision = live.rulings.get(about, 0) + (1 if ruled else 0)
            play = _Play(
                live.offered, event, arrival.received.monotonic, field, context, PromotionKey(game.id, about), revision
            )
            if live.plays.push(play, replace(arrival, occurred=event.written_at), at, key=event.key) and ruled:
                live.rulings[about] = revision

    def _release_plays(self, game: TeamGame, live: _Live, at: ClockReading, out: _Pass) -> None:
        """The newest play to clear the delay becomes the caption; an older one never takes a newer one's place. A
        ruling withdraws what stood for the play it rules on. Each live play waits for the state after it to clear
        too, to be offered for promotion; an outage's catch-up is history, and set aside."""
        caught_up = 0
        for held in live.plays.release(at):
            play = held.value
            self._supersede(live, play)
            current = play.context.origin is CandidateOrigin.LIVE
            if play.order > live.captioned:
                live.captioned = play.order
                live.caption = self._caption_of(play, held.release) if current else None
            if current:
                live.awaiting.add(play.context.settled, _ReleasedPlay(game, play, held.release))
            else:
                caught_up += 1
        if caught_up:
            out.skipped.append(f"{game.id}: {caught_up} from an outage's catch-up set aside")

    def _supersede(self, live: _Live, play: _Play) -> None:
        """A play's newest revision is out: its older revision's own card and offer no longer stand."""
        moment = play.moment.play
        newest = live.released.get(moment)
        if newest is not None and play.revision <= newest:
            return
        live.released[moment] = play.revision
        staged = live.cards.get(moment)
        if staged is not None:
            del live.cards[moment]
            self._queue.remove(staged.key)
            self._offers.withdraw(play.moment)

    def _settle_plays(self, game: TeamGame, live: _Live, at: ClockReading, out: _Pass) -> None:
        """Each released play whose successor has cleared too: offered if its class scores, naming a card of its own
        (the field its answer showed, with the play as its caption), unless a ruling on it is out already."""
        for released in live.awaiting.ready(at.monotonic):
            play = released.play
            moment = play.moment.play
            if play.revision < live.released[moment]:
                continue  # a ruling on it is out: it no longer stands
            context = play.context
            classification = classify_football_event(play.event, before=context.before, after=context.after)
            staged = live.cards.get(moment)
            key = staged.key if staged is not None else _play_key(game, play)
            offer = major_offer(
                classification.event_class,
                key=play.moment,
                card=DedupeKey(key),
                eligible=released.eligible,
                predecessor=football_proxy(context.before) if context.before is not None else None,
                stakes=interrupt_stakes(game, None),  # NFL has no records source yet
                revision=play.revision,
                origin=context.origin,
            )
            if offer is None:
                continue
            if staged is None:  # a try merges into its touchdown's card
                card = self._play_card(game, play, released.eligible, at)
                self._queue.ingest(card, at)
                out.ingested.append(card.id)
                decisive = classification.event_class is InterruptClass.DECISIVE_LATE_WINNER
                live.cards[moment] = _PlayCard(key, play.ref, decisive)
            self._offers.add(offer)

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
        since = self._recover(game.id, shown)
        if since is not None and at.monotonic.since(since) >= self._policy.stale_for:
            return  # its card is down for staleness: nothing goes back up until a newer state clears the delay
        caption = self._caption(game, live, shown, at, out, stale=since is not None)
        card = self._live_card(game, shown.value, caption, at)
        current = self._queue.card(card.dedupe_key.raw)
        recovered = current is not None and current.stale_since is not None and since is None and safe is not None
        if current is None or current.payload != card.payload or current.priority != card.priority or recovered:
            self._queue.ingest(card, at)
            out.ingested.append(card.id)
        live.card = card.dedupe_key.raw

    def _recover(self, game_id: LeagueScopedId, shown: Held[_Field]) -> MonotonicTime | None:
        """When a game's live card went stale, or None once it's current: its feed answering again, and a state it
        sent since it went stale released."""
        since = self._stale.get(game_id)
        if since is None or game_id not in self._answering or shown.arrival.received.monotonic < since:
            return since
        del self._stale[game_id]
        self._answering.discard(game_id)
        return None

    def _caption(
        self, game: TeamGame, live: _Live, shown: Held[_Field], at: ClockReading, out: _Pass, *, stale: bool
    ) -> _Caption | None:
        """The play to caption the card beside ``shown``, or None for the field alone.

        A caption goes for good once it has run out its lifetime, a newer answer's field has moved on from its own
        answer's, or no confirmed frame carried it by its latest start (said so). While the feed is quiet, in a
        break, or before the field that agrees with its score is released, the card shows the field alone.
        """
        caption = live.caption
        if caption is None:
            return None
        now = at.monotonic
        state = shown.value.state
        moved = shown.arrival.received.monotonic > caption.play.answer and _field_of(state) != caption.play.field
        late = not caption.started and now >= caption.staged.latest_start
        if late or moved or now >= caption.staged.hard_end:
            live.caption = None
            if late:
                self._missed(game, caption.play, CaptionMiss.TOO_LATE, out)
            return None
        if stale or state.interval is not None or _score(state) != caption.play.score:
            return None
        return caption

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
        self._answering.discard(game.id)
        self._current_until.pop(game.id, None)

    def _reveal_final(self, game: TeamGame, final: _Final, live: _Live, at: ClockReading, out: _Pass) -> None:
        """Reveal the final card once its result clears the delay, or hold it (reported) while it can't yet. It names
        the play that decided it, when that play was offered: its own card airs first."""
        shown = final.answers.latest_eligible(at)
        if shown is None:
            out.held.add(game.id)
            out.releases.append((game.id, at.project(final.reveal)))
            return
        decisive = [card for card in live.cards.values() if card.decisive]
        decided = decisive[-1] if decisive else None
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
            payload=LiveFootballCardPayload(state=shown.value, decided_by=decided.play if decided else None),
            replaces=DedupeKey(live.card) if live.card is not None else None,
            ended=ended,
            after=(DedupeKey(decided.key),) if decided is not None else (),
        )
        self._queue.ingest(card, at)
        final.key, final.live, final.decided = card.dedupe_key.raw, live, decided is not None
        out.finals.append(card.id)
        out.ends.append((card.id, ended))

    def _drain(self, game: TeamGame, final: _Final, readings: list[NflReading], at: ClockReading, out: _Pass) -> None:
        """A revealed final, while its feed drains: each later final answer, and the plays it brings, through the
        delay. A deciding play updates the final; a different result corrects it."""
        live = final.live
        assert live is not None  # a revealed final keeps its game's plays
        for reading in readings:
            snapshot = reading.snapshot
            out.warnings.extend(_noted(game.id, snapshot.notes))
            state = snapshot.state
            if snapshot.phase is not NflPhase.POST or state is None:
                continue  # a live answer after the final changes nothing
            arrival = Arrival(received=reading.arrived, as_of=snapshot.observed_at)
            final.answers.push(state, arrival, at)
            context = PlayContext(live.last, state, self._guard.pin(arrival, at))
            self._offer_plays(game, live, snapshot.events, _field_of(state), arrival, at, context)
        for held in live.plays.release(at):
            live.awaiting.add(held.value.context.settled, _ReleasedPlay(game, held.value, held.release))
        for released in live.awaiting.ready(at.monotonic):
            self._decided(game, final, released, at, out)
        self._correct_final(final, at, out)
        self._final_caption(game, final, at, out)

    def _decided(self, game: TeamGame, final: _Final, released: _ReleasedPlay, at: ClockReading, out: _Pass) -> None:
        """A play the drain brought, once the answer after it has cleared too: if it decided the game, it updates the
        revealed final once, as its caption and the play that decided it; one that can't is dropped, said so. Nothing
        else the drain brings is told: the game is over, and none of it is offered."""
        play = released.play
        context = play.context
        classification = classify_football_event(play.event, before=context.before, after=context.after)
        if classification.event_class is not InterruptClass.DECISIVE_LATE_WINNER:
            return
        assert final.key is not None
        card = self._queue.card(final.key)
        if card is None:
            self._missed(game, play, CaptionMiss.FINAL_OVER, out)
            return
        if final.decided:
            self._missed(game, play, CaptionMiss.FINAL_DECIDED, out)
            return
        caption = self._caption_of(play, released.eligible)
        final.caption, final.decided = caption, True
        payload = replace(card.payload, last_play=play.event.summary, caption=caption.staged, decided_by=play.ref)
        self._queue.ingest(replace(card, payload=payload), at)
        out.ingested.append(card.id)

    def _correct_final(self, final: _Final, at: ClockReading, out: _Pass) -> None:
        """A revealed final: a later final answer with a different result corrects the card once it clears the
        delay, while the card's own window lasts."""
        assert final.key is not None
        card = self._queue.card(final.key)
        if card is None:
            return  # its window is over: a correction doesn't bring it back
        newest = final.answers.latest_eligible(at)
        if newest is None:
            return
        shown: FootballGameState = card.payload.state
        if _result(newest.value) != _result(shown):
            self._queue.ingest(replace(card, payload=replace(card.payload, state=newest.value)), at)
            out.corrections.append(card.id)

    def _final_caption(self, game: TeamGame, final: _Final, at: ClockReading, out: _Pass) -> None:
        """A revealed final's caption, from the drain: gone at its hard end, once a correction leaves its score
        behind, or at its latest start (said so) when no confirmed frame has carried it."""
        caption = final.caption
        if caption is None:
            return
        assert final.key is not None
        card = self._queue.card(final.key)
        now = at.monotonic
        late = not caption.started and now >= caption.staged.latest_start
        if card is None:
            final.caption = None
            return
        if not late and now < caption.staged.hard_end and _score(card.payload.state) == caption.play.score:
            return
        final.caption = None
        if late:
            self._missed(game, caption.play, CaptionMiss.TOO_LATE, out)
        self._queue.ingest(replace(card, payload=replace(card.payload, last_play=None, caption=None)), at)

    # --- staleness ------------------------------------------------------------------------------------------

    def _note_staleness(self, game: TeamGame, at: ClockReading, out: _Pass) -> None:
        """Whether a game's live card is current as of ``at``, and if not, since when.

        It stops being current when its newest answer stops being, at once when it has no answer at all, or, for a
        game the schedule calls final, `confirm_within` after the schedule first said so while its feed hasn't.
        A stale game whose newest answer is current again is answering: it's current once a state from then on
        clears the delay (`_recover`).
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
            if game.id in self._stale:
                self._answering.add(game.id)
            return
        self._answering.discard(game.id)
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
        if self._woken_field(game_id, live):  # its field is from before the host's gap: stale since the gap
            since = self._stale[game_id] = self._woke if since is None else min(since, self._woke)
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
        and what was kept for it; one that left with no final revealed loses its plays' own cards too. A game no
        longer over by its schedule or its feed (it left the slate) loses its final too.
        """
        removed: list[LeagueScopedId] = []
        for game_id in set(self._live) - out.carding:
            live = self._live.pop(game_id)
            if live.card is not None:
                self._queue.remove(live.card)
                removed.append(game_id)
            final = self._finals.get(game_id)
            if final is None or final.live is not live:
                self._forget_plays(live)
        for game_id in set(self._finals) - out.over:
            final = self._finals.pop(game_id)
            if final.key is not None:
                self._queue.remove(final.key)
            if final.live is not None:
                self._forget_plays(final.live)
        for game_id in set(self._unconfirmed) - out.over:
            del self._unconfirmed[game_id]
        for game_id in set(self._stale) - out.carding:
            del self._stale[game_id]
        self._answering &= set(self._stale)
        for game_id in set(self._current_until) - out.carding:
            del self._current_until[game_id]
        return tuple(sorted(removed, key=str))

    def _forget_plays(self, live: _Live) -> None:
        """A game's plays leave the board: their own cards come down, and their offers are withdrawn."""
        for moment, card in live.cards.items():
            self._queue.remove(card.key)
            self._offers.withdraw(PromotionKey(live.game.id, moment))
        live.cards.clear()

    # --- what the board's policy reads --------------------------------------------------------------------------

    def _policy_view(self, at: ClockReading) -> PolicyView:
        """What the board's policy may read after this pass: each game carded live, from its newest released state,
        and the offers whose cards are staged."""
        games: dict[LeagueScopedId, GameFacts] = {}
        for game_id, live in self._live.items():
            live.breaks.release(at)
            if live.card is None:
                continue
            assert live.shown is not None  # a staged card shows the newest released state
            games[game_id] = GameFacts(
                game=live.game,
                leverage=football_proxy(live.shown.value.state),
                fresh=game_id not in self._stale,
                breaks=live.breaks.breaks,
                action=live.breaks.action,
            )
        offers = self._offers.current(at.monotonic, lambda card: self._queue.card(card.raw) is not None)
        return PolicyView(MappingProxyType(games), offers)

    # --- cards ------------------------------------------------------------------------------------------------

    def _caption_of(self, play: _Play, release: MonotonicTime) -> _Caption:
        """``play`` staged as a caption from its ``release``, with its bounds."""
        start_within, hard_end = self._policy.caption_bounds(play.event.major)
        staged = StagedCaption(play=play.ref, latest_start=release + start_within, hard_end=release + hard_end)
        return _Caption(play, staged)

    def _live_card(
        self, game: TeamGame, shown: _Field, caption: _Caption | None, at: ClockReading
    ) -> ScoreboardCard[LiveFootballCardPayload]:
        """A live card for ``game`` showing ``shown`` and ``caption``, its halftime counting down on the viewer's
        clock."""
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
                state=shown.state,
                last_play=caption.play.event.summary if caption is not None else None,
                break_ends_at=ends + lag if ends is not None else None,
                caption=caption.staged if caption is not None else None,
            ),
        )

    def _play_card(
        self, game: TeamGame, play: _Play, eligible: MonotonicTime, at: ClockReading
    ) -> ScoreboardCard[LiveFootballCardPayload]:
        """An offered play's own card, fixed for its revision: the field its answer showed, with the play as its
        caption, from the play's release for as long as its offer stands."""
        after = play.context.after
        assert after is not None  # its class read the state after it
        key = _play_key(game, play)
        available = at.project(eligible)
        return ScoreboardCard(
            id=CardId(key),
            kind=CardKind.BIG_PLAY,
            contest=game,
            timing=DisplayTiming(
                available_at=available,
                min_display=self._policy.play_min_display,
                max_display=self._policy.play_max_display,
                expires_at=available + self._policy.play_window,
            ),
            priority=self._priority(game),
            layout_support=_LAYOUT,
            dedupe_key=DedupeKey(key),
            payload=LiveFootballCardPayload(
                state=after, last_play=play.event.summary, caption=self._caption_of(play, eligible).staged
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

    # --- a host's interruption ---------------------------------------------------------------------------------

    def interrupted(self, interruption: Interruption) -> None:
        """The host slept or stalled (`BOARD_POLICY.md` §6, "Wake or output loss"): what the pipeline held from before
        the gap is history. Each live game's states, plays and break evidence still in the delay go, with its caption
        and the offers waiting; its next answer's plays are set aside, as a first answer's are, so a batch the gap
        held back can't caption or promote; and the field its card shows, released before the gap, is drawn stale
        from the gap's end (coming down `stale_for` later) until a state answered after it clears the delay. A final
        stands. (An output loss is the display's: the loop tells the pipelines only of a host's gap.)"""
        self._woke = interruption.after.monotonic
        for game_id, live in self._live.items():
            self._forget_plays(live)  # a play's own card from before the gap is history, as its offer is
            live.states, live.plays = DelayedFeed(self._guard), DelayedEventStream(self._guard)
            live.breaks = ReleasedBreaks(self._guard)
            live.primed, live.last, live.caption = False, None, None
            live.awaiting = AwaitingSuccessor()
            self._woken[game_id] = live.shown
        self._offers = StandingOffers()
        with self._lock:
            self._presented.clear()  # a confirmation from before the gap starts nothing after it

    def _woken_field(self, game_id: LeagueScopedId, live: _Live) -> bool:
        """Whether a game's card still shows the field it showed when the host's gap ended."""
        if game_id not in self._woken:
            return False
        if live.shown is self._woken[game_id]:
            return True
        del self._woken[game_id]  # a field released since: current again, as its answers say
        return False

    @staticmethod
    def _missed(game: TeamGame, play: _Play, why: CaptionMiss, out: _Pass) -> None:
        """A staged entry that never aired, said by our own label: never the feed's words."""
        revision = f" r{play.revision}" if play.revision else ""
        out.skipped.append(f"{game.id}: {play.event.label}{revision} unseen ({why.value})")


def _play_key(game: TeamGame, play: _Play) -> str:
    """The dedupe key of a play's own card: its game, the play it's about, and the revision."""
    return f"{game.id}:play:{play.moment.play}:r{play.revision}"


def _bounds(caption: _Caption | None) -> list[MonotonicTime]:
    """When a staged caption next changes by itself: its latest start while it hasn't started, and its hard end."""
    if caption is None:
        return []
    staged = caption.staged
    return [staged.hard_end] if caption.started else [staged.latest_start, staged.hard_end]


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


def _score(state: FootballGameState) -> tuple[int, int]:
    return state.away_score, state.home_score


def _noted(game: LeagueScopedId, notes: Iterable[FeedNote]) -> list[str]:
    """An answer's notes as warning lines, in our own words alone: a line a kind, with how many there were and what
    each was about. The feed's own words never go into one."""
    kinds: dict[FeedNoteKind, list[FeedNote]] = {}
    for note in notes:
        kinds.setdefault(note.kind, []).append(note)
    lines = []
    for kind, noted in kinds.items():
        count = f" x{len(noted)}" if len(noted) > 1 else ""
        said = "; ".join(dict.fromkeys(part for note in noted for part in note.ours))
        lines.append(f"{game}: {kind.value}{count}" + (f": {said}" if said else ""))
    return lines


def _result(state: FootballGameState) -> tuple[int, int, int]:
    """What a final card says the result was: the score, and the period it ended in (overtime or not)."""
    return state.away_score, state.home_score, state.period
