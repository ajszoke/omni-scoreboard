"""LiveBaseballPipeline: turn the day's slate into TV-safe queued cards.

The middle of the orchestration spine. Each pass the loop hands it the current games, a reading of
the clock, and a way to fetch a game's live feed; the pipeline cards each game for its phase:

- **upcoming** (scheduled / pre-game) — a `pregame` card carrying the matchup and a
  first-pitch countdown. There is no score to spoil, so it needs no live feed and no
  delay; the renderer derives the live countdown from the render clock. When sources are
  given it also carries each club's record, probable starter and odds, read every pass from
  what their sources last answered (the poll plan asks them, slowly, on its own workers); a
  missing or failed answer only leaves the card showing less.
- **paused** (delayed / suspended) — a `status` card carrying the matchup and a banner, so a
  game in a rain delay or suspension stays on the board instead of falling out of every phase.
  It reveals no score, so (like the pregame card) it needs no feed fetch and no delay; it is
  replaced by the live card on resume or the final card once the game ends.
- **live** — for each LIVE game, three delay-safe paths off one feed fetch:
  - **live state** — held in a per-game `DelayedFeed` so what surfaces is lag-old and
    never spoils the broadcast; scored, built into a live card, ingested.
  - **big plays** — notable events held in a per-game `DelayedEventStream` and flashed,
    each once, the moment they clear the same TV delay (measured from when the play
    happened, when that is credible). The score on a big-play card comes from the event
    itself, so a delayed flash never reveals a later, un-aired score.
  - **no-hitters** — derived from the same delay-safe state: while the batting side is
    hitless past a minimum inning a `RECURRING` no-hitter card is surfaced and refreshed,
    and removed the moment a hit (in the *delayed* state) breaks the bid — so the bid is
    revealed and un-revealed only as the broadcast would show it.
  - **win probability** — the live card's per-team meter. Each time a state clears the delay,
    the meter takes the newest reading that *arrived* no later than that state's feed answer, so
    it never knows more than the score beside it. A reading is dated by its own arrival, held
    through a gap, and dropped once it is too old to stand beside the score; past that a fallback
    source takes over. A market's price comes first, since unlike a model of the game state it
    knows how good the clubs are. A read failure is non-fatal; with nothing that can be shown, the
    card goes without a meter.
- **final** — a result comes only from a feed answer that itself says the game is final. A
  schedule that calls a game final while its feed still answers live, or not at all, never puts a
  score on a final card: the game's live card carries on as the feed has it, and if the feed still
  hasn't agreed `CONFIRM_WITHIN` after the schedule first said so, the card is marked stale and
  comes down `STALE_FOR` later, like any stale card. Its feed is still asked, so a final answer
  that comes later still reveals the result. The first answer that says final is held for the TV
  delay from its receipt (StatsAPI cannot report FINAL before the last out, so receipt + lag never
  predates the broadcast's ending); the card is then ingested and lives out a finite post-game
  window. Through that embargo the game's **last delay-safe live frame is held on screen**, and
  the final names that live card as the one it replaces, so a board showing it hands off rather
  than cutting it. Finals air in the order their games ended: by the time of the play that ended
  the game (a walk-off) when the feed names one, otherwise by when the feed first said final. A
  walk-off that ended the game keeps draining from the same delay-safe event stream, so it still
  flashes at its own release time — at or just before the final. After the reveal the feed is
  still read: a later final answer with a different result (a scoring change, decisions filled in
  late) is a new observation, held through the delay from its own receipt, and then corrects the
  final card in place, within the card's own window; a final whose window is over stays gone.

**Release.** Every hold (a state, a play, a result) is pinned when it is admitted, on the monotonic
clock, by the board's `ReleaseGuard`: never before its receipt; for a play whose time is credible, at
receipt + the delay less the play's age, told on the source's own clock (its time against its answer's);
and otherwise at receipt + the full delay. A wall-clock jump after that changes nothing already held,
and while the guard finds the wall clock suspect, everything waits its full delay from receipt. The queue
pins each card's window to the monotonic clock as it takes the card in, so a final's post-game window and
a big play's run their whole length whatever the wall clock does after.

**The policy's view.** Each pass ends with what the board's policy may read (`omni.policy.view.PolicyView`), from
released state only (`omni.app.policy_facts`):
- **each game carded live:** its leverage (the labeled proxy) from the newest state to have cleared the delay; whether
  its card is current; both clubs' records as the pregame details had them when the game started (unknown for a game
  already live when the board started, so today's result never moves them); and the break and action evidence the
  detectors (`omni.events.mlb_breaks`) found in each answer, held through the delay with that answer's state;
- **offers:** a released big play whose class scores (`classify_baseball_play`, on the released states either side
  of it) offers its big-play card, once the state after it has cleared the delay too; a confirmed feat at the final
  (`classify_baseball_final`) offers the final card, and a correction offers it again under its next revision; an
  active no-hit or perfect bid offers its no-hitter card as a reminder, a new moment each inning it carries deeper.
  What a game's first look or an outage's catch-up brings is offered flagged so (`CandidateOrigin`), never as live.

**Staleness.** A live card is current while its feed's newest answer is: for that answer's
`current_for` from its receipt when the source says how long that is, and otherwise until a read
fails. From that point, or from the first failed read if that comes first, the game's cards are
marked stale (drawn dimmed), and `STALE_FOR` after it they come down. A state clearing the delay
meanwhile still goes up, marked stale. It is measured on the monotonic clock.

A card is dropped when its game leaves that phase: the pregame card yields when the game
goes live; the no-hitter card is removed when the game is no longer live; the live card is
removed too, except a game the schedule calls final holds its last delay-safe live frame until its
final reveals (or its staleness takes it down). A big play's own card lingers out its short window,
then self-expires; the final card lingers its post-game window. So the queue mirrors the slate.
Per-game fetch failures are isolated and reported, never fatal. The pipeline's sources are reads of
what was last heard (the poll plan's answers, in the running app), so a refresh never waits on the
network.

Baseball-only for now (it calls `pregame` / `score_live_baseball` / `live_baseball` /
`big_play` / `no_hitter` / `final`), mirroring the per-sport renderer split; a generic
pipeline can dispatch later.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Callable, Iterable

from omni.app.policy_facts import (
    AwaitingSuccessor,
    Offer,
    PlayContext,
    ReleasedBreaks,
    StandingOffers,
    interrupt_stakes,
    major_offer,
)
from omni.cards.base import CardId, DedupeKey, EndBasis, GameEnd
from omni.cards.baseball import STATUS_CARD_STATUSES
from omni.cards.factory import CardFactory
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import DisplayPriority, GameStatus
from omni.core.ids import LeagueScopedId
from omni.domain.baseball import (
    BaseballGameState,
    GameLines,
    PregameDetails,
    PregameSide,
    WinProbability,
    pitching_feat_progress,
)
from omni.domain.contest import TeamGame
from omni.events.baseball import BaseballGameEvent, LiveBaseballFeed
from omni.events.mlb_breaks import mlb_break_evidence, mlb_latest_action
from omni.policy.interrupts import classify_baseball_final, classify_baseball_play
from omni.policy.leverage import baseball_proxy
from omni.policy.pool import MatchupRecords, StartingRecords, TeamRecord
from omni.policy.promotion import CandidateOrigin, PromotionCandidate, PromotionKey, PromotionKind
from omni.policy.view import EMPTY_POLICY_VIEW, GameFacts, PolicyView
from omni.providers.base import ProviderError
from omni.queue.delay_policy import Arrival, DelayPolicy, Held, ReleaseGuard
from omni.queue.delayed_event_stream import DelayedEventStream
from omni.queue.delayed_feed import DelayedFeed
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue

__all__ = [
    "CONFIRM_WITHIN",
    "STALE_FOR",
    "FeedFetcher",
    "MeterReading",
    "WinProbFetcher",
    "PregameDetailsFetcher",
    "GameLinesFetcher",
    "PipelineResult",
    "LiveBaseballPipeline",
]

# How the pipeline pulls one game's live feed as of `now` (wraps the provider's
# per-game fetch): the current state plus the play-by-play events from the same fetch.
FeedFetcher = Callable[[TeamGame, datetime], LiveBaseballFeed]


@dataclass(frozen=True, slots=True)
class MeterReading:
    """A game's win probability, and when it arrived: which scores it may stand beside, and its age."""

    value: WinProbability
    received: ClockReading


# How the pipeline reads one game's win probability as it stood at a moment (the arrival of the feed
# answer whose state is on the card): the newest reading that arrived no later than that, or None.
WinProbFetcher = Callable[[TeamGame, ClockReading], MeterReading | None]

# How the pipeline reads an upcoming game's records + probable starters, or None while none are known.
# Read every pass, so it answers from what was last heard rather than asking the network.
PregameDetailsFetcher = Callable[[TeamGame], PregameDetails | None]

# How the pipeline reads an upcoming game's odds as of `now`, or None when none are posted. Read every
# pass, so it answers from what was last heard rather than asking the network.
GameLinesFetcher = Callable[[TeamGame, datetime], GameLines | None]

# How long a meter reading may stand once the TV delay has passed since it arrived. Long enough to ride
# out a market going quiet for a few plays; past it, the reading is no longer a fair picture of the game.
_WIN_PROB_EXPIRY = timedelta(minutes=3)

# Pre-first-pitch states: both carry a pregame card (a scheduled game later today and one
# in warmups are both "upcoming"). Mid-game oddities (DELAYED/SUSPENDED) are not pregame.
_UPCOMING_STATUSES = frozenset({GameStatus.SCHEDULED, GameStatus.PREGAME})

# An event needs at least this intrinsic band to flash as a big play; below it (a single,
# a walk) the play informs the live card but is not worth a takeover. HR/triple/DP/TP clear it.
_BIG_PLAY_MIN_BAND = DisplayPriority.HIGH_LEVERAGE

# A no-hitter only becomes news once the pitching side has *finished* this many hitless innings;
# earlier hitless innings are routine. Counted per side so it never triggers a half-inning early.
_NO_HITTER_MIN_COMPLETED_INNINGS = 6

# How long a game's cards stay up, marked stale, once its feed has stopped answering. Past it the
# last-good state no longer says anything trustworthy about the game, so its cards come down.
STALE_FOR = timedelta(minutes=2)

# How long the schedule may call a game final before its feed must agree. Two of a live game's feed
# cadences: an answer is due within one, and has another to arrive. Past it, the game's live card
# is marked stale.
CONFIRM_WITHIN = timedelta(seconds=30)


class _MeterTrack:
    """One source's reading on the meter: the newest that arrived no later than the state on the card, held
    through a gap until it is too old to stand beside the score."""

    def __init__(self, delay: DelayPolicy) -> None:
        self._max_age = delay.broadcast_lag.as_timedelta() + _WIN_PROB_EXPIRY
        self._held: MeterReading | None = None

    def admit(self, reading: MeterReading, visible: ClockReading) -> None:
        """Take ``reading`` beside a state whose feed answer arrived at ``visible``, unless it arrived later."""
        arrived = reading.received.monotonic
        if arrived > visible.monotonic:
            return  # it knows more than the score it would stand beside
        if self._held is None or arrived >= self._held.received.monotonic:
            self._held = reading

    def current(self, at: ClockReading) -> WinProbability | None:
        """The reading held, aged from its own arrival; None once it is too old."""
        if self._held is not None and at.monotonic.since(self._held.received.monotonic) >= self._max_age:
            self._held = None
        return self._held.value if self._held is not None else None


class _MeterTracks:
    """One game's two meter sources, kept apart so a gap in one never mixes into the other's timeline."""

    def __init__(self, delay: DelayPolicy) -> None:
        self.primary = _MeterTrack(delay)
        self.fallback = _MeterTrack(delay)


def _read_win_probability(
    fetcher: WinProbFetcher | None, game: TeamGame, visible: ClockReading, warnings: list[str], *, label: str
) -> MeterReading | None:
    """Ask one source for its reading as of ``visible``; a failure is a warning, and just no reading this pass."""
    if fetcher is None:
        return None
    try:
        return fetcher(game, visible)
    except ProviderError as exc:
        warnings.append(f"{label} fetch failed: {exc}")
        return None


@dataclass(slots=True)
class _Final:
    """A game the feed has said is final: its final answers through the delay, and its card once revealed."""

    answers: DelayedFeed[LiveBaseballFeed]
    seen: datetime  # when the first answer that said final arrived: the end, failing a decisive play
    reveal: MonotonicTime  # when that answer clears the delay
    shown: Held[LiveBaseballFeed] | None = None  # the newest final answer to have cleared the delay
    key: str | None = None  # its card's dedupe key, once revealed
    origin: CandidateOrigin = CandidateOrigin.LIVE  # where that first answer came from, for a feat's offer
    corrections: int = 0  # how many times a later answer has corrected the card


@dataclass(frozen=True, slots=True)
class _Stated:
    """A live state held in the delay, and where its answer came from: live, or an outage's catch-up."""

    state: BaseballGameState
    origin: CandidateOrigin


@dataclass(frozen=True, slots=True)
class _Answered:
    """A game's newest answer: when it arrived, its state, the state of the answer before it, and when it clears
    the delay."""

    received: MonotonicTime
    state: BaseballGameState
    before: BaseballGameState | None
    settled: MonotonicTime


@dataclass(frozen=True, slots=True)
class _ReleasedPlay:
    """A big play whose card is up: the play, its card, when it cleared the delay, and its place in the game."""

    game: TeamGame
    event: BaseballGameEvent
    card: DedupeKey
    eligible: MonotonicTime
    context: PlayContext[BaseballGameState]


@dataclass(frozen=True, slots=True)
class _Bid:
    """A no-hit or perfect bid at one depth: the moment it is, when it first cleared the delay, and from where."""

    key: PromotionKey
    eligible: MonotonicTime
    origin: CandidateOrigin


@dataclass(slots=True)
class _Pass:
    """What one pipeline pass did, gathered as it goes."""

    pregames: list[CardId] = field(default_factory=list)
    statuses: list[CardId] = field(default_factory=list)
    ingested: list[CardId] = field(default_factory=list)
    big_plays: list[CardId] = field(default_factory=list)
    no_hitters: list[CardId] = field(default_factory=list)
    finals: list[CardId] = field(default_factory=list)
    ends: list[tuple[CardId, GameEnd]] = field(default_factory=list)
    corrections: list[CardId] = field(default_factory=list)
    held: list[LeagueScopedId] = field(default_factory=list)
    releases: list[tuple[LeagueScopedId, datetime]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineResult:
    """What one pipeline pass did (ids sorted for deterministic traces)."""

    pregames: tuple[CardId, ...]  # pregame cards surfaced/refreshed for upcoming games this pass
    statuses: tuple[CardId, ...]  # status cards surfaced/refreshed for paused (delayed/suspended) games
    ingested: tuple[CardId, ...]  # live-state cards built/refreshed this pass
    big_plays: tuple[CardId, ...]  # big-play cards surfaced this pass (chronological)
    no_hitters: tuple[CardId, ...]  # active no-hitter cards surfaced/refreshed this pass
    finals: tuple[CardId, ...]  # final cards revealed this pass (their post-game delay elapsed)
    held: tuple[LeagueScopedId, ...]  # games whose card is still inside the TV delay (live buffering / final reveal)
    removed: tuple[LeagueScopedId, ...]  # games no longer live whose card was dropped
    skipped: tuple[str, ...]  # per-game fetch failures — the game is dropped this pass
    warnings: tuple[str, ...] = ()  # non-fatal per-play parse drops — the game still shows
    stale: tuple[LeagueScopedId, ...] = ()  # games whose live card's feed has stopped answering, or being current
    releases: tuple[tuple[LeagueScopedId, datetime], ...] = ()  # when each held game's next card clears the delay
    ends: tuple[tuple[CardId, GameEnd], ...] = ()  # each final revealed this pass: when its game ended, and why
    unconfirmed: tuple[LeagueScopedId, ...] = ()  # games the schedule calls final that their feed hasn't yet
    corrections: tuple[CardId, ...] = ()  # finals whose result a later final answer changed this pass
    policy: PolicyView = EMPTY_POLICY_VIEW  # what the board's policy may read after this pass: released state only


class LiveBaseballPipeline:
    """Composes delay-feed → scorer → factory → queue for live MLB games."""

    def __init__(
        self,
        *,
        scorer: PriorityScorer,
        delay_policy: DelayPolicy,
        factory: CardFactory,
        queue: InterleavedCardQueue,
    ) -> None:
        self._scorer = scorer
        self._delay = delay_policy
        self._guard = ReleaseGuard(delay_policy)
        self._factory = factory
        self._queue = queue
        self._pregame_keys: dict[LeagueScopedId, str] = {}  # contest id -> its pregame card's key
        self._status_keys: dict[LeagueScopedId, str] = {}  # contest id -> its (delay/suspension) status card's key
        self._feeds: dict[LeagueScopedId, DelayedFeed[_Stated]] = {}
        # contest id -> its win-probability timelines; delayed in lockstep with the state feed so the
        # meter shows the reading from the same lag-old moment as the score (never a fresher one).
        self._meter_tracks: dict[LeagueScopedId, _MeterTracks] = {}
        self._card_keys: dict[LeagueScopedId, str] = {}  # contest id -> its live card's dedupe key
        self._event_streams: dict[LeagueScopedId, DelayedEventStream[BaseballGameEvent]] = {}
        self._no_hitter_keys: dict[LeagueScopedId, str] = {}  # contest id -> its no-hitter card's key
        self._finals: dict[LeagueScopedId, _Final] = {}  # games whose feed has said final
        self._unconfirmed: dict[LeagueScopedId, MonotonicTime] = {}  # final by the schedule, not yet the feed: since
        self._stale: dict[LeagueScopedId, MonotonicTime] = {}  # when a game's live card stopped being current
        self._current_until: dict[LeagueScopedId, MonotonicTime] = {}  # when a game's newest answer stops being
        # What the board's policy reads, from released state only.
        self._games: dict[LeagueScopedId, TeamGame] = {}  # each game on the slate, as it last listed it
        self._shown: dict[LeagueScopedId, BaseballGameState] = {}  # the newest state to have cleared the delay
        self._baselined: set[LeagueScopedId] = set()  # games a state has cleared the delay for: the first is a baseline
        self._answered: dict[LeagueScopedId, _Answered] = {}  # each game's newest answer, for its plays' context
        self._breaks: dict[LeagueScopedId, ReleasedBreaks] = {}
        self._pregame_records: dict[LeagueScopedId, MatchupRecords] = {}  # the records an upcoming game's card has
        self._records = StartingRecords()
        self._play_context: dict[tuple[LeagueScopedId, str], PlayContext[BaseballGameState]] = {}  # big plays held
        self._awaiting: AwaitingSuccessor[_ReleasedPlay] = AwaitingSuccessor()
        self._offers = StandingOffers()
        self._bids: dict[LeagueScopedId, _Bid] = {}

    def refresh(
        self,
        games: Iterable[TeamGame],
        *,
        at: ClockReading,
        fetch_feed: FeedFetcher,
        fetch_win_probability: WinProbFetcher | None = None,
        fetch_fallback_win_probability: WinProbFetcher | None = None,
        fetch_pregame_details: PregameDetailsFetcher | None = None,
        fetch_game_lines: GameLinesFetcher | None = None,
    ) -> PipelineResult:
        """Observe the slate at ``at``, surface each game's phase card into the queue, drop stale ones.

        `fetch_win_probability`, when given, supplies each live game's win probability as it stood when
        the state on the card arrived, so the meter never leads the shown score. Omitted (a test, or the
        meter off) the live card carries no meter. `fetch_fallback_win_probability`, when given, takes
        over once that source has gone quiet for longer than a reading may stand, or for a game it has
        no reading on at all. `fetch_pregame_details` and `fetch_game_lines`, when given, fill out the
        pregame card; a failure of either is non-fatal, and omitted the card stays at matchup + countdown.
        """
        self._guard.observe(at)
        now = at.utc
        slate = list(games)
        listed = {game.id: game for game in slate}
        for gone in set(self._games) - set(listed):
            self._forget(gone)
        self._games = listed
        live = [game for game in slate if game.status is GameStatus.LIVE]
        upcoming = [game for game in slate if game.status in _UPCOMING_STATUSES]
        irregular = [game for game in slate if game.status in STATUS_CARD_STATUSES]
        final = [game for game in slate if game.status is GameStatus.FINAL]
        out = _Pass()

        # Upcoming games card without a live feed — there is no score to spoil, so no delay.
        for game in upcoming:
            details, detail_warnings = self._pregame_details_for(game, fetch_pregame_details)
            self._note_records(game, details)
            lines, line_warnings = self._game_lines_for(game, fetch_game_lines, now=now)
            out.warnings.extend(f"{game.id}: {warning}" for warning in (*detail_warnings, *line_warnings))
            out.pregames.append(self._surface_pregame(game, details, lines, at=at))

        # Paused (delayed/suspended) games also card without a fetch — the banner reveals no score,
        # so a long rain delay never hammers the per-game feed.
        for game in irregular:
            out.statuses.append(self._surface_status(game, at=at))

        meters = (fetch_win_probability, fetch_fallback_win_probability)
        ended: set[LeagueScopedId] = set()  # live by the schedule, but final by their own feed
        for game in live:
            origin = self._origin(game.id)  # an answer while its card is stale ends an outage: it's a catch-up
            feed = self._read_feed(game, fetch_feed, at, out)
            if feed is not None and feed.status is GameStatus.FINAL:
                ended.add(game.id)  # the feed's own word on its result comes first, whatever the schedule says yet
                self._final_game(game, feed, at, out, meters, origin)
                continue
            self._note_staleness(game.id, feed, at, out)
            if feed is not None:
                arrival = Arrival(received=feed.received or at, as_of=feed.as_of)
                context = self._admit(game, feed, arrival, at, origin)
                out.big_plays.extend(self._surface_big_plays(game, feed.events, arrival, at, context))
                self._surface_state(game, feed, arrival, at, out, meters, origin, no_hitter=True)
            self._enforce_staleness(game.id, at)

        for game in final:
            known = self._finals.get(game.id)
            shown = known.key if known is not None else None  # its final card's key, once revealed
            if shown is not None and self._queue.card(shown) is None:
                continue  # its card's window is over: a correction can't bring it back, so its feed isn't read
            # A revealed final's feed is read for corrections only: a failed read there changes nothing.
            origin = self._origin(game.id)
            feed = self._read_feed(game, fetch_feed, at, out, report=shown is None)
            self._final_game(game, feed, at, out, meters, origin)

        live_ids = {game.id for game in live} - ended
        final_ids = {game.id for game in final} | ended
        removed = self._drop_absent(live_ids, final_ids)
        self._drop_stale_pregame({game.id for game in upcoming})
        self._drop_stale_status({game.id for game in irregular})
        self._drop_stale_finals(final_ids)
        return PipelineResult(
            pregames=tuple(out.pregames),
            statuses=tuple(out.statuses),
            ingested=tuple(out.ingested),
            big_plays=tuple(out.big_plays),
            no_hitters=tuple(out.no_hitters),
            finals=tuple(out.finals),
            held=tuple(sorted(out.held, key=str)),
            removed=tuple(removed),
            skipped=tuple(out.skipped),
            warnings=tuple(out.warnings),
            stale=tuple(sorted(self._stale, key=str)),
            releases=tuple(sorted(out.releases, key=lambda release: str(release[0]))),
            ends=tuple(out.ends),
            unconfirmed=tuple(sorted(self._unconfirmed, key=str)),
            corrections=tuple(out.corrections),
            policy=self._policy_view(at),
        )

    @staticmethod
    def _read_feed(
        game: TeamGame, fetch_feed: FeedFetcher, at: ClockReading, out: _Pass, *, report: bool = True
    ) -> LiveBaseballFeed | None:
        """A game's feed answer, or None when there is none; its failure and parse warnings are reported, if
        ``report``."""
        try:
            feed = fetch_feed(game, at.utc)
        except ProviderError as exc:
            if report:
                out.skipped.append(f"{game.id}: {exc}")
            return None
        if report:
            out.warnings.extend(f"{game.id}: {warning}" for warning in feed.warnings)
        return feed

    def _final_game(
        self,
        game: TeamGame,
        feed: LiveBaseballFeed | None,
        at: ClockReading,
        out: _Pass,
        meters: tuple[WinProbFetcher | None, WinProbFetcher | None],
        origin: CandidateOrigin,
    ) -> None:
        """A game that is over by its schedule or by its feed, with its feed's answer (None when there was
        none): reveal its result once the feed says final and the delay allows, keep correcting it after, and
        until then keep its live card going as the feed has it, for a while. ``origin`` says whether the answer
        ends an outage."""
        final = self._finals.get(game.id)
        if final is not None and final.key is not None:
            self._correct_final(game, final, feed, at, out)
            return
        if feed is not None:
            arrival = Arrival(received=feed.received or at, as_of=feed.as_of)
            context = self._admit(game, feed, arrival, at, origin)
            # Drain any walk-off held from the live passes: it clears the delay at its own play time, which
            # is at or before the final's reveal, so it flashes, then the final confirms it.
            out.big_plays.extend(self._surface_big_plays(game, feed.events, arrival, at, context))
            if feed.status is GameStatus.FINAL:
                final = self._confirm_final(game, feed, arrival, at, context)
            elif final is None:
                # The feed hasn't caught up with the schedule: the live card carries on as the feed has it.
                self._unconfirmed.setdefault(game.id, at.monotonic)
                self._note_staleness(game.id, feed, at, out)
                self._surface_state(game, feed, arrival, at, out, meters, origin, no_hitter=False)
        if final is None:
            if feed is None:
                self._unconfirmed.setdefault(game.id, at.monotonic)
                self._note_staleness(game.id, None, at, out)
            self._enforce_staleness(game.id, at)
            return
        self._reveal_final(game, final, at, out)

    def _confirm_final(
        self,
        game: TeamGame,
        feed: LiveBaseballFeed,
        arrival: Arrival,
        at: ClockReading,
        context: PlayContext[BaseballGameState],
    ) -> _Final:
        """An answer that says the game is final: the first starts its reveal, and each is held through the delay.
        The first answer the board ever had of the game being final is a baseline: its feat is offered so flagged."""
        final = self._finals.get(game.id)
        if final is None:
            answers: DelayedFeed[LiveBaseballFeed] = DelayedFeed(self._guard)
            answers.push(feed, arrival, at)
            origin = CandidateOrigin.BASELINE if context.before is None else context.origin
            final = _Final(answers, seen=arrival.received.utc, reveal=self._guard.pin(arrival, at), origin=origin)
            self._finals[game.id] = final
            # The feed agrees with the schedule now, and its live card is held for the reveal.
            self._unconfirmed.pop(game.id, None)
            self._stale.pop(game.id, None)
            self._current_until.pop(game.id, None)
        else:
            final.answers.push(feed, arrival, at)
        return final

    def _reveal_final(self, game: TeamGame, final: _Final, at: ClockReading, out: _Pass) -> None:
        """Reveal the final card once its result clears the delay, or hold it (reported) while it can't yet.

        The reveal also waits for every big play still inside the delay, and the card airs only after the
        game's decisive play (a walk-off) has. It is built from the newest final answer to have cleared the
        delay, and is available from the reveal instant. Finals air in the order their games ended: the card
        says when that was (the decisive play's time, or failing that when the feed first said final), and on
        what basis; that is returned with its id for the trace.
        """
        newest = final.answers.latest_eligible(at)
        if newest is not None:
            final.shown = newest
        stream = self._event_streams.get(game.id)
        if final.shown is None or (stream is not None and stream.pending()):
            out.held.append(game.id)  # ended, but the result (or a play before it) is still inside the delay
            out.releases.append((game.id, at.project(self._final_release(final, stream, at))))
            return
        result = final.shown.value
        walk_offs = [
            event
            for event in result.events
            if event.payload.scoring_impact is not None and event.payload.scoring_impact.walk_off
        ]
        live = self._card_keys.get(game.id)
        ended = _game_end(walk_offs, final.seen)
        card = self._factory.final(
            game,
            result.state,
            decisions=result.decisions,
            now=at.project(final.reveal),
            after=tuple(self._factory.big_play_key(game, event) for event in walk_offs),
            replaces=DedupeKey(live) if live is not None else None,
            ended=ended,
        )
        self._queue.ingest(card, at)
        final.key = card.dedupe_key.raw
        self._offer_feat(game, result.state, card.dedupe_key, final.reveal, final)
        # By the reveal, any walk-off has cleared the same delay and fired, so the stream is drained.
        self._event_streams.pop(game.id, None)
        out.finals.append(card.id)
        out.ends.append((card.id, ended))

    def _correct_final(
        self, game: TeamGame, final: _Final, feed: LiveBaseballFeed | None, at: ClockReading, out: _Pass
    ) -> None:
        """A revealed final: a later final answer with a different result corrects the card, once it clears the
        delay and while the card's own window lasts. No answer changes nothing."""
        assert final.key is not None
        card = self._queue.card(final.key)
        if card is None:
            return  # its window is over: a correction doesn't bring it back
        if feed is not None and feed.status is GameStatus.FINAL:
            final.answers.push(feed, Arrival(received=feed.received or at, as_of=feed.as_of), at)
        newest = final.answers.latest_eligible(at)
        if newest is None:
            return
        final.shown = newest
        result = newest.value
        rebuilt = self._factory.final(game, result.state, decisions=result.decisions, now=card.timing.available_at)
        if rebuilt.payload != card.payload:
            self._queue.ingest(replace(card, payload=rebuilt.payload), at)
            out.corrections.append(card.id)
            final.corrections += 1
            self._offer_feat(game, result.state, card.dedupe_key, newest.release, final)

    def _offer_feat(
        self, game: TeamGame, state: BaseballGameState, card: DedupeKey, eligible: MonotonicTime, final: _Final
    ) -> None:
        """A final the feed confirms as a perfect game or a no-hitter offers its card, under the revision of the
        correction it comes from; a correction that leaves no feat withdraws the offer."""
        key = PromotionKey(game.id, "final")
        offer = major_offer(
            classify_baseball_final(state, final=True).event_class,
            key=key,
            card=card,
            eligible=eligible,
            predecessor=None,  # a feat's value doesn't rest on leverage
            stakes=interrupt_stakes(game, self._starting_records(game.id)),
            revision=final.corrections,
            origin=final.origin,
        )
        if offer is None:
            self._offers.withdraw(key)
        else:
            self._offers.add(offer)

    @staticmethod
    def _pregame_details_for(
        game: TeamGame, fetcher: PregameDetailsFetcher | None
    ) -> tuple[PregameDetails | None, list[str]]:
        """A game's records and starters and any read-failure warning; a failure just leaves the card without."""
        if fetcher is None:
            return None, []
        try:
            return fetcher(game), []
        except ProviderError as exc:
            return None, [f"pregame details fetch failed: {exc}"]

    @staticmethod
    def _game_lines_for(
        game: TeamGame, fetcher: GameLinesFetcher | None, *, now: datetime
    ) -> tuple[GameLines | None, list[str]]:
        """A game's odds and any fetch-failure warning; a failure just leaves the card without odds."""
        if fetcher is None:
            return None, []
        try:
            return fetcher(game, now), []
        except ProviderError as exc:
            return None, [f"odds fetch failed: {exc}"]

    def _surface_pregame(
        self, game: TeamGame, details: PregameDetails | None, lines: GameLines | None, *, at: ClockReading
    ) -> CardId:
        """Surface (or refresh) the pregame card for an upcoming game, with whatever details are known."""
        away, home = (details.away, details.home) if details is not None else (None, None)
        if lines is not None:
            away = replace(away or PregameSide(), line=lines.away)
            home = replace(home or PregameSide(), line=lines.home)
        card = self._factory.pregame(game, now=at.utc, away=away, home=home)
        self._queue.ingest(card, at)
        self._pregame_keys[game.id] = card.dedupe_key.raw
        return card.id

    def _surface_status(self, game: TeamGame, *, at: ClockReading) -> CardId:
        """Surface (or refresh) the status card for a paused (delayed/suspended) game."""
        card = self._factory.status(game, status=game.status, now=at.utc)
        self._queue.ingest(card, at)
        self._status_keys[game.id] = card.dedupe_key.raw
        return card.id

    def _surface_state(
        self,
        game: TeamGame,
        feed: LiveBaseballFeed,
        arrival: Arrival,
        at: ClockReading,
        out: _Pass,
        meters: tuple[WinProbFetcher | None, WinProbFetcher | None],
        origin: CandidateOrigin,
        *,
        no_hitter: bool,
    ) -> None:
        """Hold a game's new state in its delay feed, with the break and action evidence its answer shows, and card
        the newest that has cleared it (with the no-hitter, when asked); report the game as held while nothing new
        has."""
        state_feed = self._feeds.setdefault(game.id, DelayedFeed(self._guard))
        state_feed.push(_Stated(feed.state, origin), arrival, at)
        received = arrival.received
        self._breaks.setdefault(game.id, ReleasedBreaks(self._guard)).push(
            mlb_break_evidence(feed, game, received), mlb_latest_action(feed, game, received), arrival, at
        )
        safe = state_feed.latest_eligible(at)
        if safe is None:
            out.held.append(game.id)  # still inside the TV delay — nothing safe to show yet
            release = state_feed.next_eligible(at)
            if release is not None:
                out.releases.append((game.id, at.project(release)))
            return
        since = self._stale.get(game.id)
        if since is not None and at.monotonic.since(since) >= STALE_FOR:
            return  # its cards are down for staleness: nothing goes back up until its feed is current again
        state = safe.value.state
        baseline = game.id not in self._baselined  # the first state to clear for a game is what the board tuned in to
        self._baselined.add(game.id)
        # The meter takes the newest reading that arrived no later than the state now on the card.
        win_prob, wp_warnings = self._safe_win_probability(game, *meters, visible=safe.arrival.received, at=at)
        out.warnings.extend(f"{game.id}: {warning}" for warning in wp_warnings)
        priority = self._scorer.score_live_baseball(game, state)
        card = self._factory.live_baseball(game, state, now=at.utc, priority=priority, win_probability=win_prob)
        self._queue.ingest(card, at)
        self._card_keys[game.id] = card.dedupe_key.raw
        self._shown[game.id] = state
        out.ingested.append(card.id)
        if no_hitter:
            # No-hitter rides the same delay-safe state, so it is revealed/broken only as aired.
            bid_origin = CandidateOrigin.BASELINE if baseline else safe.value.origin
            no_hit = self._surface_no_hitter(game, state, at=at, release=safe.release, origin=bid_origin)
            if no_hit is not None:
                out.no_hitters.append(no_hit)

    def _safe_win_probability(
        self,
        game: TeamGame,
        primary: WinProbFetcher | None,
        fallback: WinProbFetcher | None,
        *,
        visible: ClockReading,
        at: ClockReading,
    ) -> tuple[WinProbability | None, list[str]]:
        """The win probability for this game's live card, plus any read-failure warnings.

        ``visible`` is when the feed answer whose state the card shows arrived. Only a reading that
        arrived no later than that may stand beside it, so the meter never knows more than the score.
        A reading is dated by its own arrival, held through a gap in its source, and dropped once the
        TV delay plus `_WIN_PROB_EXPIRY` has passed since it arrived (on the monotonic clock).

        While the primary has nothing, the fallback is read the same way, so it is ready the moment the
        held reading expires; while the primary answers, the fallback isn't touched. With neither (the
        meter off) this is a no-op. Read failures are non-fatal: the live card still shows, and each is
        surfaced as a warning.
        """
        tracks = self._meter_tracks.setdefault(game.id, _MeterTracks(self._delay))
        warnings: list[str] = []
        current = _read_win_probability(primary, game, visible, warnings, label="win-probability")
        if current is not None:
            tracks.primary.admit(current, visible)
        else:
            backup = _read_win_probability(fallback, game, visible, warnings, label="fallback win-probability")
            if backup is not None:
                tracks.fallback.admit(backup, visible)
        primary_reading = tracks.primary.current(at)
        fallback_reading = tracks.fallback.current(at)
        return (primary_reading if primary_reading is not None else fallback_reading), warnings

    def _surface_big_plays(
        self,
        game: TeamGame,
        events: tuple[BaseballGameEvent, ...],
        arrival: Arrival,
        at: ClockReading,
        context: PlayContext[BaseballGameState],
    ) -> list[CardId]:
        """Flash notable events as big-play cards once they clear the TV delay (once each). Each new one keeps
        ``context``, its answer's place in the game, so its card can be offered for promotion once released."""
        stream = self._event_streams.get(game.id)
        if stream is None:
            # First sight of this game: prime the backlog so plays from before we tuned
            # in don't all flush at once — only events seen on later polls surface.
            stream = DelayedEventStream(self._guard)
            self._event_streams[game.id] = stream
            for event in events:
                stream.mark_seen(event.id.raw)
        else:
            for event in events:
                if event.importance.priority >= _BIG_PLAY_MIN_BAND:
                    if stream.push(event, replace(arrival, occurred=event.source_time), at, key=event.id.raw):
                        self._play_context[(game.id, event.id.raw)] = context
        surfaced: list[CardId] = []
        for held in stream.release(at):
            card = self._factory.big_play(game, held.value, now=at.utc)
            self._queue.ingest(card, at)
            surfaced.append(card.id)
            played = self._play_context.pop((game.id, held.value.id.raw))
            self._awaiting.add(played.settled, _ReleasedPlay(game, held.value, card.dedupe_key, held.release, played))
        return surfaced

    def _surface_no_hitter(
        self,
        game: TeamGame,
        state: BaseballGameState,
        *,
        at: ClockReading,
        release: MonotonicTime,
        origin: CandidateOrigin,
    ) -> CardId | None:
        """Surface (or refresh) an active no-hitter / perfect-game card, or remove it once broken.

        The depth shown is the pitching side's *finished* innings (not the raw current inning),
        and a confirmed clean sheet promotes the card to a perfect game — both off the same
        delay-safe state, so the feat is revealed and un-revealed only as the broadcast would show.
        Each depth (and a perfect bid turning into a plain no-hitter) is a moment of its own for a reminder,
        eligible from the ``release`` of the state that first showed it; ``origin`` is that state's.
        """
        feat = pitching_feat_progress(state, min_completed_innings=_NO_HITTER_MIN_COMPLETED_INNINGS)
        if feat is None:
            # No (longer) a bid — drop the card if this game had one.
            stale = self._no_hitter_keys.pop(game.id, None)
            if stale is not None:
                self._queue.remove(stale)
            return None
        card = self._factory.no_hitter(
            game, pitching_side=feat.side, through_inning=feat.completed_innings, perfect=feat.perfect, now=at.utc
        )
        self._queue.ingest(card, at)
        self._no_hitter_keys[game.id] = card.dedupe_key.raw
        moment = PromotionKey(game.id, f"{feat.kind.value} through {feat.completed_innings}")
        bid = self._bids.get(game.id)
        if bid is None or bid.key != moment:
            self._bids[game.id] = _Bid(moment, release, origin)
        return card.id

    def _note_staleness(
        self, game_id: LeagueScopedId, feed: LiveBaseballFeed | None, at: ClockReading, out: _Pass
    ) -> None:
        """Whether a game's live card is current as of ``at``, and if not, since when.

        It stops being current when its newest answer's `current_for` runs out, at a failed read (``feed`` is
        None) if that comes first, or, for a game the schedule calls final, `CONFIRM_WITHIN` after the schedule
        first said so while its feed hasn't. A current answer makes it current again.
        """
        if feed is not None and feed.current_for is not None:
            self._current_until[game_id] = (feed.received or at).monotonic + feed.current_for
        elif feed is not None:
            self._current_until.pop(game_id, None)
        points: list[tuple[MonotonicTime, str]] = []
        if feed is None:
            points.append((at.monotonic, ""))  # the failed read is reported as skipped
        until = self._current_until.get(game_id)
        if until is not None and until <= at.monotonic:
            points.append((until, "its feed's newest answer is out of date"))
        unconfirmed = self._unconfirmed.get(game_id)
        if unconfirmed is not None and unconfirmed + CONFIRM_WITHIN <= at.monotonic:
            points.append((unconfirmed + CONFIRM_WITHIN, "the schedule says final, and its feed still doesn't"))
        if not points:
            self._stale.pop(game_id, None)
            return
        point, why = min(points, key=lambda candidate: candidate[0])
        since = self._stale.get(game_id)
        if since is not None and since <= point:
            return
        self._stale[game_id] = point
        if since is None and why:
            last = f" (its last ask: {feed.failing})" if feed is not None and feed.failing is not None else ""
            out.warnings.append(f"{game_id}: stale: {why}{last}")

    def _enforce_staleness(self, game_id: LeagueScopedId, at: ClockReading) -> None:
        """A game whose live card isn't current: its cards are marked stale, and come down `STALE_FOR` after."""
        since = self._stale.get(game_id)
        if since is None:
            return
        keys = [key for key in (self._card_keys.get(game_id), self._no_hitter_keys.get(game_id)) if key is not None]
        if at.monotonic.since(since) >= STALE_FOR:
            for key in keys:
                self._queue.remove(key)
            self._card_keys.pop(game_id, None)
            self._shown.pop(game_id, None)
            self._no_hitter_keys.pop(game_id, None)
            return
        for key in keys:
            card = self._queue.card(key)
            if card is not None and card.stale_since is None:
                self._queue.ingest(replace(card, stale_since=at.project(since)), at)

    def _drop_stale_pregame(self, upcoming_ids: set[LeagueScopedId]) -> None:
        """Drop the pregame card for any game no longer upcoming — it went live, ended, or left the slate."""
        for contest_id in set(self._pregame_keys) - upcoming_ids:
            self._queue.remove(self._pregame_keys.pop(contest_id))

    def _drop_stale_status(self, irregular_ids: set[LeagueScopedId]) -> None:
        """Drop the status card for any game no longer paused — it resumed, ended, or left the slate.

        On resume the live card takes over; once final the final card does. The status card holds
        no live-scoped state of its own (no feed/stream), so dropping its key here is the whole teardown.
        """
        for contest_id in set(self._status_keys) - irregular_ids:
            self._queue.remove(self._status_keys.pop(contest_id))

    def _drop_stale_finals(self, final_ids: set[LeagueScopedId]) -> None:
        """Drop final tracking for any game no longer final — it left the slate (or, rarely, resumed).

        A pending reveal is simply forgotten; a revealed card is pulled (it would otherwise
        expire on its own post-game window, but its game is gone).
        """
        for contest_id in set(self._finals) - final_ids:
            key = self._finals.pop(contest_id).key
            if key is not None:
                self._queue.remove(key)
        for contest_id in set(self._unconfirmed) - final_ids:
            del self._unconfirmed[contest_id]

    def _drop_absent(self, live_ids: set[LeagueScopedId], final_ids: set[LeagueScopedId]) -> tuple[LeagueScopedId, ...]:
        """Drop per-game state for games that have moved on; return the carded ones.

        Live-scoped state (feed, live card, no-hitter card) is dropped the moment the game leaves the live
        set, with one exception: a game the schedule calls final whose final hasn't revealed yet keeps its
        last delay-safe live frame (card + feed) on screen, so the board holds that frame instead of going
        blank, until the final card reveals (the same pass then tears it down) or its staleness takes it
        down. The no-hitter card, by contrast, is removed as soon as the game leaves the live set (an
        in-progress bid is over once the game ends). A big-play card is left to expire on its own short
        window; the event stream keeps draining through the post-final window (so a held walk-off still
        fires), so it is dropped only once the game leaves the slate entirely — or earlier, at the final
        reveal, by which point it has drained. Only live-card removals are reported.
        """
        holding = {
            contest_id
            for contest_id in final_ids
            if (final := self._finals.get(contest_id)) is None or final.key is None
        }
        live_scoped = set(self._feeds) | set(self._card_keys) | set(self._no_hitter_keys)
        removed_cards: list[LeagueScopedId] = []
        for contest_id in live_scoped - live_ids:
            if contest_id not in holding:  # a final not yet revealed holds its last frame; do not tear down
                key = self._card_keys.pop(contest_id, None)
                if key is not None:
                    self._queue.remove(key)
                    removed_cards.append(contest_id)
                self._shown.pop(contest_id, None)
                self._feeds.pop(contest_id, None)
                self._breaks.pop(contest_id, None)  # its break evidence rides the state feed's lifecycle too
                self._meter_tracks.pop(contest_id, None)  # win-prob buffers ride the state feed's lifecycle
            no_hitter_key = self._no_hitter_keys.pop(contest_id, None)
            if no_hitter_key is not None:
                self._queue.remove(no_hitter_key)
        for contest_id in set(self._event_streams) - (live_ids | final_ids):
            self._event_streams.pop(contest_id, None)
        for contest_id in set(self._stale) - (live_ids | holding):
            del self._stale[contest_id]
        for contest_id in set(self._current_until) - (live_ids | holding):
            del self._current_until[contest_id]
        return tuple(sorted(removed_cards, key=str))

    # --- what the board's policy reads ---------------------------------------------------------------------------

    def _origin(self, game_id: LeagueScopedId) -> CandidateOrigin:
        """Where a game's answer this pass comes from: one while its card is stale ends an outage."""
        return CandidateOrigin.CATCH_UP if game_id in self._stale else CandidateOrigin.LIVE

    def _admit(
        self, game: TeamGame, feed: LiveBaseballFeed, arrival: Arrival, at: ClockReading, origin: CandidateOrigin
    ) -> PlayContext[BaseballGameState]:
        """A game's answer as the context of the plays it brings: the state of the answer before it, its own, and
        when its own clears the delay. The same answer read again keeps the context it had."""
        received = arrival.received.monotonic
        last = self._answered.get(game.id)
        if last is None or received > last.received:
            before = last.state if last is not None else None
            last = _Answered(received, feed.state, before, self._guard.pin(arrival, at))
            self._answered[game.id] = last
        return PlayContext(last.before, last.state, last.settled, origin)

    def _note_records(self, game: TeamGame, details: PregameDetails | None) -> None:
        """Keep an upcoming game's records, both clubs', as its pregame card has them."""
        if details is None or details.away.record is None or details.home.record is None:
            return
        away, home = TeamRecord.of(details.away.record), TeamRecord.of(details.home.record)
        self._pregame_records[game.id] = MatchupRecords(away=away, home=home)

    def _starting_records(self, game_id: LeagueScopedId) -> MatchupRecords | None:
        """A started game's records as they stood at its start; None for one the board didn't see before it."""
        return self._records.read(game_id, self._pregame_records.get(game_id), started=True)

    def _forget(self, game_id: LeagueScopedId) -> None:
        """Drop what the policy kept of a game that has left the slate."""
        self._pregame_records.pop(game_id, None)
        self._records.forget(game_id)
        self._baselined.discard(game_id)
        self._answered.pop(game_id, None)
        self._play_context = {key: played for key, played in self._play_context.items() if key[0] != game_id}
        self._awaiting.forget(lambda released: released.game.id != game_id)

    def _policy_view(self, at: ClockReading) -> PolicyView:
        """What the board's policy may read after this pass: each game carded live, from its newest released state,
        and the offers whose cards are staged."""
        for track in self._breaks.values():
            track.release(at)
        for released in self._awaiting.ready(at.monotonic):
            self._offer_play(released)
        games: dict[LeagueScopedId, GameFacts] = {}
        for game_id in self._card_keys:
            track = self._breaks[game_id]
            games[game_id] = GameFacts(
                game=self._games[game_id],
                leverage=baseball_proxy(self._shown[game_id]),
                fresh=game_id not in self._stale,
                records=self._starting_records(game_id),
                breaks=track.breaks,
                action=track.action,
            )
        self._bids = {game_id: bid for game_id, bid in self._bids.items() if game_id in self._no_hitter_keys}
        reminders = [
            PromotionCandidate(
                key=bid.key,
                kind=PromotionKind.REMINDER,
                eligible=bid.eligible,
                card=DedupeKey(self._no_hitter_keys[game_id]),
                origin=bid.origin,
            )
            for game_id, bid in self._bids.items()
        ]
        offers: tuple[Offer, ...] = (*self._offers.current(at.monotonic, self._staged), *reminders)
        return PolicyView(MappingProxyType(games), offers)

    def _offer_play(self, released: _ReleasedPlay) -> None:
        """A released big play whose class scores offers its card, valued on the released state before it."""
        context, game = released.context, released.game
        classification = classify_baseball_play(released.event, before=context.before, after=context.after)
        offer = major_offer(
            classification.event_class,
            key=PromotionKey(game.id, released.event.id.raw),
            card=released.card,
            eligible=released.eligible,
            predecessor=baseball_proxy(context.before) if context.before is not None else None,
            stakes=interrupt_stakes(game, self._starting_records(game.id)),
            origin=context.origin,
        )
        if offer is not None:
            self._offers.add(offer)

    def _staged(self, card: DedupeKey) -> bool:
        return self._queue.card(card.raw) is not None

    @staticmethod
    def _final_release(
        final: _Final, stream: DelayedEventStream[BaseballGameEvent] | None, at: ClockReading
    ) -> MonotonicTime:
        """When a held final is next due to clear: its reveal, or then its last play still in the delay."""
        plays = stream.next_eligible() if stream is not None else None
        return final.reveal if final.reveal > at.monotonic or plays is None else plays

    def next_deadline(self, at: ClockReading) -> MonotonicTime | None:
        """When the pipeline would next surface or drop something by itself, as of ``at``: a state, a play or a
        final result clearing the delay, a game's live card going stale, or a stale game's cards coming down."""
        instants: list[MonotonicTime | None] = [feed.next_eligible(at) for feed in self._feeds.values()]
        instants += [stream.next_eligible() for stream in self._event_streams.values()]
        instants += [final.answers.next_eligible(at) for final in self._finals.values()]
        instants += [since + STALE_FOR for since in self._stale.values()]
        instants += [until for game_id, until in self._current_until.items() if game_id not in self._stale]
        instants += [
            since + CONFIRM_WITHIN for game_id, since in self._unconfirmed.items() if game_id not in self._stale
        ]
        return min((instant for instant in instants if instant is not None and instant > at.monotonic), default=None)


def _game_end(decisive: list[BaseballGameEvent], seen_at: datetime) -> GameEnd:
    """When a game first seen final at ``seen_at`` ended: the time of the play that ended it, when the feed
    names one no later than that, or else ``seen_at`` itself."""
    times = [event.source_time for event in decisive if event.source_time <= seen_at]
    if times:
        return GameEnd(max(times), EndBasis.DECISIVE_PLAY)
    return GameEnd(seen_at, EndBasis.FIRST_SEEN)
