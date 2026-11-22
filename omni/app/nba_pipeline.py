"""LiveBasketballPipeline: NBA's reads into TV-safe cards on the shared board.

The NBA counterpart of `omni.app.nfl_pipeline.LiveFootballPipeline`, under the same rules wherever they apply. Each pass
the loop hands it the board's NBA games and a reading of the clock. It takes each game's new reads from its `NbaFeeds`,
each once and in arrival order, through the game's own `BasketballFeedDeriver`, and cards the game for where it stands:

- **upcoming** (the slate's row has it scheduled or postponed): a `PREGAME` card, the tip (or ``PPD``), each club's
  record and standing ranks once the season's finals are read, the favorite's line when the sportsbook has one, and no
  score to spoil, so no delay.
- **live** (the slate has it under way): a `LIVE_GAME` card from the newest slate state to have cleared the TV delay,
  the side whose timeout is on while one is (released through the delay, until the clock runs again: the single
  board shows ``TO`` for it), each club's record (as of the tip: the night's own final isn't counted until it's
  revealed), the win meter when the board reads one (the newest reading that arrived no later than the state beside
  it, aged from its own arrival and dropped `NbaCardPolicy.meter_expiry` past the delay, so it never knows more than
  the score), a halftime counting down to 15 minutes after the first halftime read (on the viewer's clock), and the
  bottom panel (`omni.app.nba_panel`):
  the play queue and the stat rotation, its stat panels from the newest players' lines to have cleared the delay.
  - **The captions** come from the deriver, each held through the delay from the arrival of the read that told it, then
    staged on the panel with its bounds. They start only when a frame carrying them is confirmed shown (`presented`),
    and are dropped, never shown late, past their latest start; the pass says so. What the deriver holds back (a basket
    waiting for its assist, a trip waiting to end) isn't staged until it tells it.
  - **Without the players' lines** (Free, or a key whose tier refuses them) the card runs on the slate alone: no
    captions, and the line score in the bottom panel.
  - A game's first lines read is the deriver's baseline; the reads taken while its card is stale (an outage) are a
    catch-up, and none of their captions or timeouts is staged.
  - **No run** is shown: when a run ends is still to be settled, so the card's run is always None.
- **final**: a result comes only from a slate read that itself says the game is over. The first is held for the TV
  delay from its receipt; then a `FINAL` card, the same frame in its final state, with the line score, each club's
  best player from the newest released lines, and records that include the night's game (the final is folded into the
  standings as it's revealed), lives out the post-game window (30 minutes). It names the live card as the one it
  replaces, and the finals air in the order their games ended (by when the slate first said so). Until it reveals,
  the live card carries on. Later reads drain into it: a different final result corrects it, newer lines its best
  players, while its window lasts.

**Release.** Every hold (a state, a lines read, a caption, a timeout's onset or end, a result) is pinned when it's
admitted, on the monotonic clock, by the board's `ReleaseGuard` for NBA's own delay, at receipt + the delay:
BALLDONTLIE dates nothing it sends.

**Staleness**, as NFL's: a live card is current while its game's newest read is (two of its read's cadence); once not,
it's drawn stale, and `NbaCardPolicy.stale_for` later it comes down, until a state read after it went stale clears the
delay.

**Records and ranks** come from `NbaStandings`, replaced by each complete season read the feeds hand over and with each
revealed final folded in. Until a season read is in, a card shows none.

**The policy's view** (`omni.policy.view.PolicyView`): each game carded live, from released state only: its leverage
(the labeled proxy, `basketball_proxy`), whether its card is current, both clubs' records as they stood (unknown until
the season is read), whether it's an NBA Cup knockout game, and the break evidence the detector
(`omni.events.nba_breaks`) found in each read, held through the delay with that read: its timeouts, begun and ended,
and the breaks between periods. A catch-up's timeouts lend nothing. NBA offers nothing for promotion.

**Priority** is the scheduler's bands: a game with one of NBA's favorites is in the favorite band; the others normal.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from omni.app.nba_feeds import NbaFeeds, NbaReading
from omni.app.nba_panel import BottomPanelQueue, PanelPolicy, PanelView, QueuedCaption
from omni.app.pipeline import STALE_FOR, GameMeter, PipelineResult
from omni.app.policy_facts import ReleasedBreaks
from omni.cards.base import CardId, CardKind, CardPriority, DedupeKey, DisplayTiming, EndBasis, GameEnd, ScoreboardCard
from omni.cards.basketball import (
    FINAL_LAYOUT,
    GAME_LAYOUT,
    PREGAME_LAYOUT,
    BasketballGameCardPayload,
    BasketballPregameCardPayload,
    BestPlayers,
    BottomPanel,
    LineScorePanel,
)
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import DisplayPriority, HomeAway
from omni.core.ids import LeagueScopedId
from omni.core.time import DurationSeconds
from omni.domain.baseball import WinLossRecord
from omni.domain.basketball import BasketballGameState, BasketballPhase, BySide, PlayerLine, StandingRanks
from omni.domain.basketball_reads import LinesRead, SlateRead
from omni.domain.contest import TeamGame
from omni.events.basketball_feed import BasketballFeedDeriver, FeedCaption, TimeoutEnd, TimeoutOnset
from omni.events.nba_breaks import NbaBreaks
from omni.policy.leverage import Leverage, basketball_proxy
from omni.policy.pool import MatchupRecords, TeamRecord
from omni.policy.view import GameFacts, PolicyView
from omni.preferences.favorites import Favorites
from omni.providers.balldontlie import CupStage, Pregame
from omni.providers.nba_standings import NbaStandings
from omni.providers.nba_teams import NbaTeamRegistry
from omni.queue.delay_policy import Arrival, DelayPolicy, Held, ReleaseGuard
from omni.queue.delayed_feed import DelayedFeed
from omni.queue.scheduler import InterleavedCardQueue
from omni.tower.interruption import Interruption

__all__ = ["LiveBasketballPipeline", "NbaCardPolicy"]

T = TypeVar("T")

_FAVORITE_SCORE = 30.0  # the scheduler's favorite weight, as baseball's scorer gives it
_NORMAL = CardPriority(band=DisplayPriority.NORMAL, score=0.0)
_KNOCKOUT = frozenset({CupStage.QUARTERFINAL, CupStage.SEMIFINAL, CupStage.CHAMPIONSHIP})


@dataclass(frozen=True, slots=True, kw_only=True)
class NbaCardPolicy:
    """How NBA's cards are timed: their turns, the post-game window, halftime's length for its countdown, when a quiet
    feed's card goes, and the bottom panel's timing."""

    live_min_display: DurationSeconds = DurationSeconds(8)
    live_max_display: DurationSeconds = DurationSeconds(30)
    final_min_display: DurationSeconds = DurationSeconds(8)
    final_max_display: DurationSeconds = DurationSeconds(20)
    pregame_min_display: DurationSeconds = DurationSeconds(8)
    pregame_max_display: DurationSeconds = DurationSeconds(20)
    postgame_window: timedelta = timedelta(minutes=30)
    halftime: timedelta = timedelta(minutes=15)  # the rule's halftime, which its countdown runs down
    meter_expiry: timedelta = timedelta(minutes=3)  # a meter reading stands this long past the delay since it arrived
    stale_for: timedelta = STALE_FOR
    panel: PanelPolicy = PanelPolicy()


class _Holding(Generic[T]):
    """Items held through the delay, each released once, in the order admitted."""

    def __init__(self, guard: ReleaseGuard) -> None:
        self._guard = guard
        self._held: list[Held[T]] = []

    def push(self, value: T, arrival: Arrival, at: ClockReading) -> None:
        self._held.append(Held(value, arrival, self._guard.pin(arrival, at)))

    def release(self, at: ClockReading) -> list[Held[T]]:
        ready = [held for held in self._held if held.release <= at.monotonic]
        self._held = [held for held in self._held if held.release > at.monotonic]
        return ready

    def next_release(self) -> MonotonicTime | None:
        return min((held.release for held in self._held), default=None)


@dataclass(frozen=True, slots=True)
class _Told:
    """A caption the deriver told, and the arrival of the players' lines that told it (None for a slate read's)."""

    told: FeedCaption
    lines: MonotonicTime | None


@dataclass(slots=True)
class _Live:
    """What the pipeline keeps of one game while it cards it live and through its final's drain."""

    game: TeamGame
    states: DelayedFeed[BasketballGameState]
    lines: DelayedFeed[LinesRead]
    captions: _Holding[_Told]
    timeouts: _Holding[TimeoutOnset | TimeoutEnd]
    breaks: ReleasedBreaks  # its break evidence, held with the reads that showed it
    detector: NbaBreaks
    meter: GameMeter
    deriver: BasketballFeedDeriver = field(default_factory=BasketballFeedDeriver)
    panel: BottomPanelQueue = field(default_factory=BottomPanelQueue)
    told: int = 0  # captions staged so far: each one's place
    shown: Held[BasketballGameState] | None = None  # the newest state to have cleared the delay
    shown_lines: Held[LinesRead] | None = None  # the newest players' lines to have cleared it
    open: set[tuple[HomeAway, MonotonicTime]] = field(default_factory=set)  # released timeouts not over
    halftime: datetime | None = None  # when the halftime on the card was first read
    view: PanelView | None = None  # what the card showed beside its panel at the last pass
    card: str | None = None  # its live card's dedupe key, while that card is staged


@dataclass(slots=True)
class _Final:
    """A game the slate has said is over: its final reads through the delay, and its card once revealed."""

    answers: DelayedFeed[BasketballGameState]
    seen: datetime  # when the first read that said so arrived: the end, as far as the board can tell
    reveal: MonotonicTime  # when that read clears the delay
    key: str | None = None  # its card's dedupe key, once revealed
    live: _Live | None = None  # the game's reads, kept for the drain once revealed


@dataclass(slots=True)
class _Pass:
    """What one pass did, gathered as it goes."""

    pregames: list[CardId] = field(default_factory=list)
    ingested: list[CardId] = field(default_factory=list)
    finals: list[CardId] = field(default_factory=list)
    ends: list[tuple[CardId, GameEnd]] = field(default_factory=list)
    corrections: list[CardId] = field(default_factory=list)
    held: set[LeagueScopedId] = field(default_factory=set)
    releases: list[tuple[LeagueScopedId, datetime]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    upcoming: set[LeagueScopedId] = field(default_factory=set)  # games whose pregame card is kept up this pass
    carding: set[LeagueScopedId] = field(default_factory=set)  # games whose live card is kept up this pass
    over: set[LeagueScopedId] = field(default_factory=set)  # games over by their slate


@dataclass(frozen=True, slots=True)
class _Shown:
    """A frame the display confirmed: the game whose live card it was (None for any other card), the card's turn on the
    board, when it was shown, and its bottom panel."""

    game: LeagueScopedId | None
    occurrence: int
    at: MonotonicTime
    bottom: BottomPanel | None


class LiveBasketballPipeline:
    """Composes NBA reads → deriver → delay → cards → queue for the board's NBA games."""

    def __init__(
        self,
        *,
        feeds: NbaFeeds,
        delay_policy: DelayPolicy,
        queue: InterleavedCardQueue,
        favorites: Favorites = Favorites(),
        policy: NbaCardPolicy = NbaCardPolicy(),
        registry: NbaTeamRegistry | None = None,
    ) -> None:
        self._feeds = feeds
        self._guard = ReleaseGuard(delay_policy)
        self._queue = queue
        self._favorites = favorites
        self._policy = policy
        self._standings = NbaStandings(registry if registry is not None else NbaTeamRegistry.default())
        self._season: ClockReading | None = None  # the arrival of the season read the standings stand on
        self._live: dict[LeagueScopedId, _Live] = {}
        self._finals: dict[LeagueScopedId, _Final] = {}
        self._pregames: dict[LeagueScopedId, str] = {}  # each upcoming game's pregame card key
        self._stale: dict[LeagueScopedId, MonotonicTime] = {}  # when a game's live card stopped being current
        self._answering: set[LeagueScopedId] = set()  # stale games whose feed is current again
        self._current_until: dict[LeagueScopedId, MonotonicTime] = {}
        self._woke: MonotonicTime = MonotonicTime(0.0)  # the last host interruption's end
        self._woken: dict[LeagueScopedId, Held[BasketballGameState] | None] = {}  # each game's state shown before it
        self._lock = threading.Lock()  # `presented` comes from the frame thread
        self._presented: list[_Shown] = []

    def refresh(self, games: Iterable[TeamGame], *, at: ClockReading) -> PipelineResult:
        """Take each game's new reads as of ``at``, card it for where it stands, and drop what's done."""
        self._guard.observe(at)
        self._take_season()
        self._take_presented()
        out = _Pass()
        for game in games:
            row = self._feeds.slate_game(game)
            if row is None:
                continue  # not read yet
            if row.pregame is not None and game.id not in self._live and game.id not in self._finals:
                self._surface_pregame(game, row.pregame, at, out)
            elif row.read is not None or game.id in self._live or game.id in self._finals:
                self._card(game, self._feeds.take(game), at, out)
        removed = self._drop(out)
        return PipelineResult(
            pregames=tuple(out.pregames),
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
            unconfirmed=(),
            corrections=tuple(out.corrections),
            policy=self._policy_view(at),
        )

    def presented(self, card: ScoreboardCard[Any], at: ClockReading, *, occurrence: int) -> None:
        """The display confirmed a frame of ``card`` shown at ``at``, in that card's turn ``occurrence``: a live NBA
        card's panel times its halves and starts what it carried; any other card ends the NBA cards' turns. Safe from
        the frame thread; the next refresh takes it in."""
        payload = card.payload
        live = card.kind is CardKind.LIVE_GAME and isinstance(payload, BasketballGameCardPayload)
        shown = _Shown(
            game=card.contest.id if live else None,
            occurrence=occurrence,
            at=at.monotonic,
            bottom=payload.bottom if live else None,
        )
        with self._lock:
            self._presented.append(shown)

    def next_deadline(self, at: ClockReading) -> MonotonicTime | None:
        """When the pipeline would next change something by itself, as of ``at``: a state, lines, a caption, a timeout
        or a result clearing the delay, the bottom panel's next change, a live card going stale, or a stale card coming
        down."""
        instants: list[MonotonicTime | None] = []
        for live in self._live.values():
            instants += [
                live.states.next_eligible(at),
                live.lines.next_eligible(at),
                live.captions.next_release(),
                live.timeouts.next_release(),
            ]
            if live.view is not None:
                instants.append(live.panel.next_change(at.monotonic, live.view))
        for final in self._finals.values():
            instants += [final.answers.next_eligible(at)]
            if final.live is not None:
                instants.append(final.live.lines.next_eligible(at))
        instants += [since + self._policy.stale_for for since in self._stale.values()]
        instants += [until for game_id, until in self._current_until.items() if game_id not in self._stale]
        return min((instant for instant in instants if instant is not None and instant > at.monotonic), default=None)

    def interrupted(self, interruption: Interruption) -> None:
        """The host slept or stalled: what the pipeline held from before the gap is history. Each live game's reads
        still in the delay go, with its deriver's memory (its next lines read is a fresh baseline), its panel's waiting
        captions and its open timeouts; the state its card shows, released before the gap, is drawn stale from the
        gap's end until a state read after it clears the delay. A final stands."""
        self._guard.distrust(interruption.after)
        self._woke = interruption.after.monotonic
        for game_id, live in self._live.items():
            live.states, live.lines = DelayedFeed(self._guard), DelayedFeed(self._guard)
            live.captions, live.timeouts = _Holding(self._guard), _Holding(self._guard)
            live.breaks, live.detector = ReleasedBreaks(self._guard), NbaBreaks(game_id)
            live.deriver = BasketballFeedDeriver()
            live.panel.reset()
            live.open.clear()
            self._woken[game_id] = live.shown
        with self._lock:
            self._presented.clear()

    # --- what the display showed ------------------------------------------------------------------------------

    def _take_presented(self) -> None:
        """Hand each confirmed frame to the panels: the card's own to its panel, any other ending the others' turns."""
        with self._lock:
            presented, self._presented = self._presented, []
        for shown in presented:
            for game_id, live in self._live.items():
                if game_id == shown.game and shown.bottom is not None:
                    live.panel.shown(shown.occurrence, shown.at, shown.bottom)
                else:
                    live.panel.off_air()

    def _take_season(self) -> None:
        """Count the records from a newer complete read of the season's finals, if one came in."""
        season = self._feeds.season()
        if season is None or (self._season is not None and season.arrived.monotonic <= self._season.monotonic):
            return
        self._standings.replace(season.results)
        self._season = season.arrived

    # --- one game -----------------------------------------------------------------------------------------------

    def _surface_pregame(self, game: TeamGame, pregame: Pregame, at: ClockReading, out: _Pass) -> None:
        """A game not yet under way: its pregame card, with the records and ranks once the season is read."""
        ranks: BySide[StandingRanks | None] = BySide(None, None)
        if self._season is not None:
            ranks = BySide(self._standings.ranks(game.away), self._standings.ranks(game.home))
        payload = BasketballPregameCardPayload(
            phase=pregame.phase, tip=pregame.tip, records=self._records(game), ranks=ranks, line=self._feeds.line(game)
        )
        key = f"{game.id}:pregame"
        card = ScoreboardCard(
            id=CardId(key),
            kind=CardKind.PREGAME,
            contest=game,
            timing=DisplayTiming(
                available_at=at.utc,
                min_display=self._policy.pregame_min_display,
                max_display=self._policy.pregame_max_display,
            ),
            priority=self._priority(game),
            layout_support=PREGAME_LAYOUT,
            dedupe_key=DedupeKey(key),
            payload=payload,
        )
        current = self._queue.card(key)
        if current is None or current.payload != payload or current.priority != card.priority:
            self._queue.ingest(card, at)
            out.pregames.append(card.id)
        self._pregames[game.id] = key
        out.upcoming.add(game.id)

    def _card(self, game: TeamGame, readings: list[NbaReading], at: ClockReading, out: _Pass) -> None:
        """A game under way or over: take its reads in, then card it for where it stands."""
        final = self._finals.get(game.id)
        if final is not None and final.key is not None:
            out.over.add(game.id)
            self._drain(game, final, readings, at, out)
            return
        live = self._live.get(game.id)
        if live is None:
            live = self._live[game.id] = _Live(
                game,
                DelayedFeed(self._guard),
                DelayedFeed(self._guard),
                _Holding(self._guard),
                _Holding(self._guard),
                ReleasedBreaks(self._guard),
                NbaBreaks(game.id),
                GameMeter(self._guard.policy.broadcast_lag.as_timedelta() + self._policy.meter_expiry),
                panel=BottomPanelQueue(self._policy.panel),
            )
        live.game = game
        catching_up = game.id in self._stale and game.id not in self._answering
        for reading in readings:
            self._admit(game, live, reading, at, out, catching_up=catching_up)
        final = self._finals.get(game.id)
        if final is None:
            self._note_staleness(game, at, out)
        else:
            out.over.add(game.id)
        self._release(game, live, at, out)
        self._surface_live(game, live, at, out)
        for missed in live.panel.missed():
            kind = type(missed.caption).__name__
            out.skipped.append(f"{game.id}: caption {missed.order} ({kind}) unseen (too late to start)")
        if final is None:
            self._enforce_staleness(game.id, live, at)
        else:
            self._reveal_final(game, final, live, at, out)
        if final is None or final.key is None:
            out.carding.add(game.id)

    def _admit(
        self, game: TeamGame, live: _Live, reading: NbaReading, at: ClockReading, out: _Pass, *, catching_up: bool
    ) -> None:
        """One read: through the deriver, its state or lines into the delay, what the deriver told after it, and the
        break evidence it shows. A read taken while the game's card is stale is a catch-up: the plays and timeouts it
        told are history, and lend nothing."""
        read = reading.read
        update = live.deriver.read(read)
        arrival = Arrival(received=reading.arrived)
        shown = live.detector.read(read, replace(update, timeouts=()) if catching_up else update, reading.arrived)
        live.breaks.push(shown, None, arrival, at)
        if isinstance(read, SlateRead):
            if read.state.phase is BasketballPhase.FINAL:
                self._confirm_final(game, read.state, arrival, at)
            elif game.id not in self._finals:
                live.states.push(read.state, arrival, at)
        else:
            live.lines.push(read, arrival, at)
        if catching_up:
            if update.captions:
                out.skipped.append(f"{game.id}: {len(update.captions)} from an outage's catch-up set aside")
            return
        lines = reading.arrived.monotonic if isinstance(read, LinesRead) else None
        for told in update.captions:
            live.captions.push(_Told(told, lines), arrival, at)
        for timeout in update.timeouts:
            live.timeouts.push(timeout, arrival, at)

    def _release(self, game: TeamGame, live: _Live, at: ClockReading, out: _Pass) -> None:
        """What cleared the delay: captions staged on the panel (none without the players' lines), and timeouts begun
        and ended."""
        for held in live.captions.release(at):
            if not self._feeds.lines:
                continue  # on the slate alone, the card has no captions
            caption = held.value.told.caption
            start_within, hard_end = self._policy.panel.bounds(caption)
            live.told += 1
            live.panel.offer(
                QueuedCaption(
                    order=live.told,
                    caption=caption,
                    release=held.release,
                    latest_start=held.release + start_within,
                    hard_end=held.release + hard_end,
                    after=held.value.lines,
                )
            )
        for released in live.timeouts.release(at):
            timeout = released.value
            if isinstance(timeout, TimeoutOnset):
                live.open.add((timeout.side, timeout.onset))
            else:
                live.open.discard((timeout.side, timeout.onset))

    def _surface_live(self, game: TeamGame, live: _Live, at: ClockReading, out: _Pass) -> None:
        """Card the newest state to have cleared the delay, with its panel; report the game held while a newer one is
        still inside it."""
        safe = live.states.latest_eligible(at)
        if safe is not None:
            live.shown = safe
        else:
            release = live.states.next_eligible(at)
            if release is not None:
                out.held.add(game.id)  # a newer state is still inside the TV delay
                out.releases.append((game.id, at.project(release)))
        lines = live.lines.latest_eligible(at)
        if lines is not None:
            live.shown_lines = lines
        shown = live.shown
        if shown is None:
            return
        since = self._recover(game.id, shown)
        if since is not None and at.monotonic.since(since) >= self._policy.stale_for:
            return  # its card is down for staleness: nothing goes back up until a newer state clears the delay
        state = shown.value
        if state.phase is BasketballPhase.LIVE and live.open:
            side, _ = max(live.open, key=lambda timeout: timeout[1])
            state = replace(state, timeout=side)  # the newest timeout on: the single board shows TO for it
        if state.phase is not BasketballPhase.HALFTIME:
            live.halftime = None
        elif live.halftime is None:
            live.halftime = shown.arrival.received.utc
        view = PanelView(
            live=state.phase is BasketballPhase.LIVE,
            timeout=bool(live.open),
            lines=self._lines_of(live) if self._feeds.lines else None,
            shown=shown.arrival.received.monotonic,
        )
        live.view = view
        visible = shown.arrival.received
        live.meter.admit(self._feeds.win_probability(game, visible), visible)
        payload = BasketballGameCardPayload(
            state=state,
            records=self._records(game),
            win_probability=live.meter.current(at),
            bottom=live.panel.bottom(at.monotonic, view),
            break_ends_at=self._halftime_ends(live.halftime),
        )
        key = f"{game.id}:live"
        card = ScoreboardCard(
            id=CardId(key),
            kind=CardKind.LIVE_GAME,
            contest=game,
            timing=DisplayTiming(
                available_at=at.utc,
                min_display=self._policy.live_min_display,
                max_display=self._policy.live_max_display,
            ),
            priority=self._priority(game),
            layout_support=GAME_LAYOUT,
            dedupe_key=DedupeKey(key),
            payload=payload,
        )
        current = self._queue.card(key)
        recovered = current is not None and current.stale_since is not None and since is None and safe is not None
        if current is None or current.payload != payload or current.priority != card.priority or recovered:
            self._queue.ingest(card, at)
            out.ingested.append(card.id)
        live.card = key

    def _halftime_ends(self, began: datetime | None) -> datetime | None:
        """When halftime ends on the viewer's clock: the rule's length from its first read, plus the delay."""
        if began is None:
            return None
        return began + self._policy.halftime + self._guard.policy.broadcast_lag.as_timedelta()

    # --- the final --------------------------------------------------------------------------------------------

    def _confirm_final(self, game: TeamGame, state: BasketballGameState, arrival: Arrival, at: ClockReading) -> None:
        """A read that says the game is over: the first starts its reveal, and each is held through the delay."""
        final = self._finals.get(game.id)
        if final is not None:
            final.answers.push(state, arrival, at)
            return
        answers: DelayedFeed[BasketballGameState] = DelayedFeed(self._guard)
        answers.push(state, arrival, at)
        self._finals[game.id] = _Final(answers, seen=arrival.received.utc, reveal=self._guard.pin(arrival, at))
        self._stale.pop(game.id, None)
        self._answering.discard(game.id)
        self._current_until.pop(game.id, None)

    def _reveal_final(self, game: TeamGame, final: _Final, live: _Live, at: ClockReading, out: _Pass) -> None:
        """Reveal the final once its result clears the delay, its result folded into the standings first; or hold it
        (reported) while it can't yet."""
        shown = final.answers.latest_eligible(at)
        if shown is None:
            out.held.add(game.id)
            out.releases.append((game.id, at.project(final.reveal)))
            return
        row = self._feeds.slate_game(game)
        result = row.result if row is not None else None
        if result is not None:
            self._standings.fold(result)
        ended = GameEnd(final.seen, EndBasis.FIRST_SEEN)
        available = at.project(final.reveal)
        key = f"{game.id}:final"
        card = ScoreboardCard(
            id=CardId(key),
            kind=CardKind.FINAL,
            contest=game,
            timing=DisplayTiming(
                available_at=available,
                min_display=self._policy.final_min_display,
                max_display=self._policy.final_max_display,
                expires_at=available + self._policy.postgame_window,
            ),
            priority=_NORMAL,
            layout_support=FINAL_LAYOUT,
            dedupe_key=DedupeKey(key),
            payload=self._final_payload(game, shown.value, live),
            replaces=DedupeKey(live.card) if live.card is not None else None,
            ended=ended,
        )
        self._queue.ingest(card, at)
        final.key, final.live = key, live
        out.finals.append(card.id)
        out.ends.append((card.id, ended))

    def _final_payload(self, game: TeamGame, state: BasketballGameState, live: _Live) -> BasketballGameCardPayload:
        """A final as its card draws it: the line score, the records with the result, each club's best player."""
        lines = self._lines_of(live)
        best = BestPlayers.of(lines.away, lines.home) if lines is not None else None
        return BasketballGameCardPayload(state=state, records=self._records(game), bottom=LineScorePanel(), best=best)

    def _drain(self, game: TeamGame, final: _Final, readings: list[NbaReading], at: ClockReading, out: _Pass) -> None:
        """A revealed final, while its reads drain: a later final read through the delay corrects its result, and newer
        players' lines its best players, while its window lasts."""
        live = final.live
        assert live is not None  # a revealed final keeps its game's reads
        for reading in readings:
            read = reading.read
            arrival = Arrival(received=reading.arrived)
            if isinstance(read, LinesRead):
                live.lines.push(read, arrival, at)
            elif read.state.phase is BasketballPhase.FINAL:
                final.answers.push(read.state, arrival, at)
        card = self._queue.card(final.key) if final.key is not None else None
        if card is None:
            return  # its window is over: nothing brings it back
        lines = live.lines.latest_eligible(at)
        if lines is not None:
            live.shown_lines = lines
        newest = final.answers.latest_eligible(at)
        shown: BasketballGameCardPayload = card.payload
        state = newest.value if newest is not None else shown.state
        payload = self._final_payload(game, state, live)
        if payload != shown:
            self._queue.ingest(replace(card, payload=payload), at)
            out.corrections.append(card.id)

    # --- staleness ------------------------------------------------------------------------------------------

    def _note_staleness(self, game: TeamGame, at: ClockReading, out: _Pass) -> None:
        """Whether a game's live card is current as of ``at``, and if not, since when: it stops being current when its
        newest read stops being, and at once when it has none. A stale game whose newest read is current again is
        answering: it's current once a state from then on clears the delay (`_recover`)."""
        latest = self._feeds.latest(game)
        if latest is None:
            out.skipped.append(f"{game.id}: no read yet")
            self._current_until.pop(game.id, None)
            point = at.monotonic
        else:
            until = self._current_until[game.id] = latest.arrived.monotonic + latest.current_for
            if until > at.monotonic:
                if game.id in self._stale:
                    self._answering.add(game.id)
                return
            point = until
        self._answering.discard(game.id)
        since = self._stale.get(game.id)
        if since is not None and since <= point:
            return
        self._stale[game.id] = point
        if since is None and latest is not None:
            out.warnings.append(f"{game.id}: stale: its feed's newest read is out of date")

    def _recover(self, game_id: LeagueScopedId, shown: Held[BasketballGameState]) -> MonotonicTime | None:
        """When a game's live card went stale, or None once it's current: its feed answering again, and a state it
        sent since it went stale released."""
        since = self._stale.get(game_id)
        if since is None or game_id not in self._answering or shown.arrival.received.monotonic < since:
            return since
        del self._stale[game_id]
        self._answering.discard(game_id)
        return None

    def _enforce_staleness(self, game_id: LeagueScopedId, live: _Live, at: ClockReading) -> None:
        """A game whose live card isn't current: the card is marked stale, and comes down `stale_for` after."""
        since = self._stale.get(game_id)
        if self._woken_state(game_id, live):  # its state is from before the host's gap: stale since the gap
            since = self._stale[game_id] = self._woke if since is None else min(since, self._woke)
        if since is None or live.card is None:
            return
        if at.monotonic.since(since) >= self._policy.stale_for:
            self._queue.remove(live.card)
            live.card, live.shown = None, None
            return
        card = self._queue.card(live.card)
        if card is not None and card.stale_since is None:
            self._queue.ingest(replace(card, stale_since=at.project(since)), at)

    def _woken_state(self, game_id: LeagueScopedId, live: _Live) -> bool:
        """Whether a game's card still shows the state it showed when the host's gap ended."""
        if game_id not in self._woken:
            return False
        if live.shown is self._woken[game_id]:
            return True
        del self._woken[game_id]  # a state released since: current again, as its reads say
        return False

    # --- teardown ---------------------------------------------------------------------------------------------

    def _drop(self, out: _Pass) -> tuple[LeagueScopedId, ...]:
        """Drop what the games this pass left behind; return the games whose live card came down."""
        for game_id in set(self._pregames) - out.upcoming:
            self._queue.remove(self._pregames.pop(game_id))
        removed: list[LeagueScopedId] = []
        for game_id in set(self._live) - out.carding:
            live = self._live.pop(game_id)
            if live.card is not None:
                self._queue.remove(live.card)
                removed.append(game_id)
        for game_id in set(self._finals) - out.over:
            final = self._finals.pop(game_id)
            if final.key is not None:
                self._queue.remove(final.key)
        for game_id in set(self._stale) - out.carding:
            del self._stale[game_id]
        self._answering &= set(self._stale)
        for game_id in set(self._current_until) - out.carding:
            del self._current_until[game_id]
        return tuple(sorted(removed, key=str))

    # --- what the board's policy reads ------------------------------------------------------------------------

    def _policy_view(self, at: ClockReading) -> PolicyView:
        """What the board's policy may read after this pass: each game carded live, from its newest released state,
        with the break evidence released with it."""
        games: dict[LeagueScopedId, GameFacts] = {}
        for game_id, live in self._live.items():
            live.breaks.release(at)
            if live.card is None:
                continue
            assert live.shown is not None  # a staged card shows the newest released state
            row = self._feeds.slate_game(live.game)
            games[game_id] = GameFacts(
                game=live.game,
                leverage=_leverage(live.shown.value),
                fresh=game_id not in self._stale,
                records=self._matchup(live.game),
                cup_knockout=row is not None and row.cup in _KNOCKOUT,
                breaks=live.breaks.breaks,
            )
        return PolicyView(MappingProxyType(games), ())

    # --- the cards' parts -------------------------------------------------------------------------------------

    def _records(self, game: TeamGame) -> BySide[WinLossRecord | None]:
        """Each club's record, once the season is read."""
        if self._season is None:
            return BySide(None, None)
        return BySide(self._standings.record(game.away), self._standings.record(game.home))

    def _matchup(self, game: TeamGame) -> MatchupRecords | None:
        """Both clubs' records as the policy reads them, once the season is read."""
        records = self._records(game)
        if records.away is None or records.home is None:
            return None
        return MatchupRecords(away=TeamRecord.of(records.away), home=TeamRecord.of(records.home))

    @staticmethod
    def _lines_of(live: _Live) -> BySide[tuple[PlayerLine, ...]] | None:
        """Each team's players' lines, from the newest lines to have cleared the delay."""
        shown = live.shown_lines
        if shown is None:
            return None
        read = shown.value
        return BySide(
            tuple(roster.line for roster in read.side(HomeAway.AWAY)),
            tuple(roster.line for roster in read.side(HomeAway.HOME)),
        )

    def _priority(self, game: TeamGame) -> CardPriority:
        """The favorite band for a game with one of NBA's favorites in it, naming the better-ranked; else normal."""
        rank = self._favorites.of(game)
        if rank is None:
            return _NORMAL
        leader = game.away if self._favorites.rank(game.away) == rank else game.home
        return CardPriority(
            band=DisplayPriority.FAVORITE,
            score=_FAVORITE_SCORE,
            reasons=(f"favorite #{rank} {leader.abbreviation}",),
        )


def _leverage(state: BasketballGameState) -> Leverage:
    """A game's proxy leverage from its released state: at a break its period has run out; with no clock yet in a
    period under way it's yet to start."""
    if state.phase is BasketballPhase.FINAL:
        return basketball_proxy(period=None, clock_seconds=None, away_score=None, home_score=None)
    if state.clock is not None:
        clock = state.clock.tenths / 10
    elif state.phase is BasketballPhase.LIVE:
        clock = (12 if state.period <= 4 else 5) * 60.0
    else:
        clock = 0.0
    return basketball_proxy(
        period=state.period, clock_seconds=clock, away_score=state.away.score, home_score=state.home.score
    )
