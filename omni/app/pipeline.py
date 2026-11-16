"""LiveBaseballPipeline: turn the day's slate into TV-safe queued cards.

The middle of the orchestration spine. Each tick the loop hands it the current games
and a way to fetch a game's live feed; the pipeline cards each game for its phase:

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
    each once, the moment they clear the same TV delay (anchored to when the play
    happened). The score on a big-play card comes from the event itself, so a delayed
    flash never reveals a later, un-aired score.
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
- **final** — once a game ends, a `final` card with the box score. The result is the
  ultimate spoiler, so the reveal is held until the broadcast lag elapses from when we
  first saw the game final (StatsAPI cannot report FINAL before the last out, so first
  sight + lag never predates the broadcast's ending); the card is then ingested and
  lives out a finite post-game window. Through that embargo the game's **last delay-safe
  live frame is held on screen** (its live card is not torn down until the final reveals),
  so the board shows the last-aired state rather than going blank; the final names that
  live card as the one it replaces, so a board showing it hands off rather than cutting it.
  Finals air in the order their games ended: by the time of the play that ended the game (a
  walk-off) when the feed names one, otherwise by when the game was first seen final. A
  walk-off that ended the game keeps draining from the same delay-safe event stream, so it
  still flashes at its own release time — at or just before the final — even though the game
  is already over.

A card is dropped when its game leaves that phase: the pregame card yields when the game
goes live; the no-hitter card is removed when the game is no longer live; the live card is
removed too, except a just-ended game holds its last delay-safe live frame through the final
embargo, then swaps to the final (a big play's own card lingers out its short window, then
self-expires); the final card lingers its post-game window. So the queue mirrors the slate.
Per-game fetch failures are isolated and reported, never fatal. The pipeline's sources are reads of
what was last heard (the poll plan's answers, in the running app), so a refresh never waits on the
network.

Baseball-only for now (it calls `pregame` / `score_live_baseball` / `live_baseball` /
`big_play` / `no_hitter` / `final`), mirroring the per-sport renderer split; a generic
pipeline can dispatch later.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Callable, Iterable

from omni.cards.base import CardId, DedupeKey, EndBasis, GameEnd
from omni.cards.baseball import STATUS_CARD_STATUSES
from omni.cards.factory import CardFactory
from omni.core.enum import DisplayPriority, GameStatus
from omni.core.ids import LeagueScopedId
from omni.core.observation import Observation
from omni.domain.baseball import (
    BaseballGameState,
    GameLines,
    PitchingDecisions,
    PregameDetails,
    PregameSide,
    WinProbability,
    pitching_feat_progress,
)
from omni.domain.contest import TeamGame
from omni.events.baseball import BaseballGameEvent, LiveBaseballFeed
from omni.providers.base import ProviderError
from omni.queue.delay_policy import DelayPolicy
from omni.queue.delayed_event_stream import DelayedEventStream
from omni.queue.delayed_feed import DelayedFeed
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue

__all__ = [
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
    received: datetime


# How the pipeline reads one game's win probability as it stood at an instant (the arrival of the feed
# answer whose state is on the card): the newest reading that arrived no later than that, or None.
WinProbFetcher = Callable[[TeamGame, datetime], MeterReading | None]

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


class _MeterTrack:
    """One source's reading on the meter: the newest that arrived no later than the state on the card, held
    through a gap until it is too old to stand beside the score."""

    def __init__(self, delay: DelayPolicy) -> None:
        self._max_age = delay.broadcast_lag.as_timedelta() + _WIN_PROB_EXPIRY
        self._held: MeterReading | None = None

    def admit(self, reading: MeterReading, visible: datetime) -> None:
        """Take ``reading`` beside a state whose feed answer arrived at ``visible``, unless it arrived later."""
        if reading.received > visible:
            return  # it knows more than the score it would stand beside
        if self._held is None or reading.received >= self._held.received:
            self._held = reading

    def current(self, now: datetime) -> WinProbability | None:
        """The reading held, aged from its own arrival; None once it is too old."""
        if self._held is not None and now - self._held.received >= self._max_age:
            self._held = None
        return self._held.value if self._held is not None else None


class _MeterTracks:
    """One game's two meter sources, kept apart so a gap in one never mixes into the other's timeline."""

    def __init__(self, delay: DelayPolicy) -> None:
        self.primary = _MeterTrack(delay)
        self.fallback = _MeterTrack(delay)


def _read_win_probability(
    fetcher: WinProbFetcher | None, game: TeamGame, visible: datetime, warnings: list[str], *, label: str
) -> MeterReading | None:
    """Ask one source for its reading as of ``visible``; a failure is a warning, and just no reading this pass."""
    if fetcher is None:
        return None
    try:
        return fetcher(game, visible)
    except ProviderError as exc:
        warnings.append(f"{label} fetch failed: {exc}")
        return None


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
    stale: tuple[LeagueScopedId, ...] = ()  # live games whose feed has stopped answering
    releases: tuple[tuple[LeagueScopedId, datetime], ...] = ()  # when each held game's next card clears the delay
    ends: tuple[tuple[CardId, GameEnd], ...] = ()  # each final revealed this pass: when its game ended, and why


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
        self._factory = factory
        self._queue = queue
        self._pregame_keys: dict[LeagueScopedId, str] = {}  # contest id -> its pregame card's key
        self._status_keys: dict[LeagueScopedId, str] = {}  # contest id -> its (delay/suspension) status card's key
        self._feeds: dict[LeagueScopedId, DelayedFeed[BaseballGameState]] = {}
        # contest id -> its win-probability timelines; delayed in lockstep with the state feed so the
        # meter shows the reading from the same lag-old moment as the score (never a fresher one).
        self._meter_tracks: dict[LeagueScopedId, _MeterTracks] = {}
        self._card_keys: dict[LeagueScopedId, str] = {}  # contest id -> its live card's dedupe key
        self._event_streams: dict[LeagueScopedId, DelayedEventStream[BaseballGameEvent]] = {}
        self._no_hitter_keys: dict[LeagueScopedId, str] = {}  # contest id -> its no-hitter card's key
        # contest id -> when we first saw it final (the reveal's delay anchor; built from current state)
        self._final_pending: dict[LeagueScopedId, datetime] = {}
        self._final_keys: dict[LeagueScopedId, str] = {}  # contest id -> its (revealed) final card's key
        self._stale: dict[LeagueScopedId, datetime] = {}  # contest id -> when its feed stopped answering

    def refresh(
        self,
        games: Iterable[TeamGame],
        *,
        now: datetime,
        fetch_feed: FeedFetcher,
        fetch_win_probability: WinProbFetcher | None = None,
        fetch_fallback_win_probability: WinProbFetcher | None = None,
        fetch_pregame_details: PregameDetailsFetcher | None = None,
        fetch_game_lines: GameLinesFetcher | None = None,
    ) -> PipelineResult:
        """Observe the slate, surface each game's phase card into the queue, drop stale ones.

        `fetch_win_probability`, when given, supplies each live game's win probability as it stood when
        the state on the card arrived, so the meter never leads the shown score. Omitted (a test, or the
        meter off) the live card carries no meter. `fetch_fallback_win_probability`, when given, takes
        over once that source has gone quiet for longer than a reading may stand, or for a game it has
        no reading on at all. `fetch_pregame_details` and `fetch_game_lines`, when given, fill out the
        pregame card; a failure of either is non-fatal, and omitted the card stays at matchup + countdown.
        """
        live = [game for game in games if game.status is GameStatus.LIVE]
        upcoming = [game for game in games if game.status in _UPCOMING_STATUSES]
        irregular = [game for game in games if game.status in STATUS_CARD_STATUSES]
        final = [game for game in games if game.status is GameStatus.FINAL]
        live_ids = {game.id for game in live}
        upcoming_ids = {game.id for game in upcoming}
        irregular_ids = {game.id for game in irregular}
        final_ids = {game.id for game in final}

        pregames: list[CardId] = []
        statuses: list[CardId] = []
        ingested: list[CardId] = []
        big_plays: list[CardId] = []
        no_hitters: list[CardId] = []
        finals: list[CardId] = []
        ends: list[tuple[CardId, GameEnd]] = []
        held: list[LeagueScopedId] = []
        releases: list[tuple[LeagueScopedId, datetime]] = []
        skipped: list[str] = []
        warnings: list[str] = []

        # Upcoming games card without a live feed — there is no score to spoil, so no delay.
        for game in upcoming:
            details, detail_warnings = self._pregame_details_for(game, fetch_pregame_details)
            lines, line_warnings = self._game_lines_for(game, fetch_game_lines, now=now)
            warnings.extend(f"{game.id.raw}: {warning}" for warning in (*detail_warnings, *line_warnings))
            pregames.append(self._surface_pregame(game, details, lines, now=now))

        # Paused (delayed/suspended) games also card without a fetch — the banner reveals no score,
        # so a long rain delay never hammers the per-game feed.
        for game in irregular:
            statuses.append(self._surface_status(game, now=now))

        for game in live:
            try:
                feed = fetch_feed(game, now)
            except ProviderError as exc:
                skipped.append(f"{game.id.raw}: {exc}")
                self._go_stale(game.id, now)
                continue
            self._stale.pop(game.id, None)
            warnings.extend(f"{game.id.raw}: {warning}" for warning in feed.warnings)

            # Big-play path first, so it runs even when the state is still delay-held.
            big_plays.extend(self._surface_big_plays(game, feed.events, now=now))

            # Live-state path: hold in the delay feed, surface only the lag-old value.
            state_feed = self._feeds.setdefault(game.id, DelayedFeed(self._delay))
            received = feed.received_at if feed.received_at is not None else now
            state_feed.push(
                Observation(subject_id=game.id, source=game.id.source, observed_at=received, value=feed.state)
            )
            safe = state_feed.latest_eligible(now)
            if safe is None:
                held.append(game.id)  # still inside the TV delay — nothing safe to show yet
                release = state_feed.next_eligible(now)
                if release is not None:
                    releases.append((game.id, release))
                continue
            # The meter takes the newest reading that arrived no later than the state now on the card.
            win_prob, wp_warnings = self._safe_win_probability(
                game, fetch_win_probability, fetch_fallback_win_probability, visible=safe.observed_at, now=now
            )
            warnings.extend(f"{game.id.raw}: {warning}" for warning in wp_warnings)
            priority = self._scorer.score_live_baseball(game, safe.value)
            card = self._factory.live_baseball(game, safe.value, now=now, priority=priority, win_probability=win_prob)
            self._queue.ingest(card)
            self._card_keys[game.id] = card.dedupe_key.raw
            ingested.append(card.id)

            # No-hitter rides the same delay-safe state, so it is revealed/broken only as aired.
            no_hit = self._surface_no_hitter(game, safe.value, now=now)
            if no_hit is not None:
                no_hitters.append(no_hit)

        for game in final:
            if game.id in self._final_keys:
                continue  # already revealed — the card lives out its post-game window on its own
            try:
                feed = fetch_feed(game, now)
            except ProviderError as exc:
                skipped.append(f"{game.id.raw}: {exc}")
                continue
            warnings.extend(f"{game.id.raw}: {warning}" for warning in feed.warnings)
            # Drain any walk-off held from the live ticks: it clears the delay at its own play
            # time, which is at or before the final's first-sight anchor — so it flashes, then
            # the final confirms it. (Surfacing here, before the reveal, keeps the stream alive.)
            big_plays.extend(self._surface_big_plays(game, feed.events, now=now))
            revealed = self._surface_final(game, feed.state, feed.decisions, feed.events, now=now)
            if revealed is not None:
                finals.append(revealed[0])
                ends.append(revealed)
            else:
                held.append(game.id)  # ended, but the result is still inside the post-game delay
                releases.append((game.id, self._final_release(game.id, now)))

        removed = self._drop_absent(live_ids, final_ids)
        self._drop_stale_pregame(upcoming_ids)
        self._drop_stale_status(irregular_ids)
        self._drop_stale_finals(final_ids)
        return PipelineResult(
            pregames=tuple(pregames),
            statuses=tuple(statuses),
            ingested=tuple(ingested),
            big_plays=tuple(big_plays),
            no_hitters=tuple(no_hitters),
            finals=tuple(finals),
            held=tuple(sorted(held, key=str)),
            removed=tuple(removed),
            skipped=tuple(skipped),
            warnings=tuple(warnings),
            stale=tuple(sorted(self._stale, key=str)),
            releases=tuple(sorted(releases, key=lambda release: str(release[0]))),
            ends=tuple(ends),
        )

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
        self, game: TeamGame, details: PregameDetails | None, lines: GameLines | None, *, now: datetime
    ) -> CardId:
        """Surface (or refresh) the pregame card for an upcoming game, with whatever details are known."""
        away, home = (details.away, details.home) if details is not None else (None, None)
        if lines is not None:
            away = replace(away or PregameSide(), line=lines.away)
            home = replace(home or PregameSide(), line=lines.home)
        card = self._factory.pregame(game, now=now, away=away, home=home)
        self._queue.ingest(card)
        self._pregame_keys[game.id] = card.dedupe_key.raw
        return card.id

    def _surface_status(self, game: TeamGame, *, now: datetime) -> CardId:
        """Surface (or refresh) the status card for a paused (delayed/suspended) game."""
        card = self._factory.status(game, status=game.status, now=now)
        self._queue.ingest(card)
        self._status_keys[game.id] = card.dedupe_key.raw
        return card.id

    def _safe_win_probability(
        self,
        game: TeamGame,
        primary: WinProbFetcher | None,
        fallback: WinProbFetcher | None,
        *,
        visible: datetime,
        now: datetime,
    ) -> tuple[WinProbability | None, list[str]]:
        """The win probability for this game's live card, plus any read-failure warnings.

        ``visible`` is when the feed answer whose state the card shows arrived. Only a reading that
        arrived no later than that may stand beside it, so the meter never knows more than the score.
        A reading is dated by its own arrival, held through a gap in its source, and dropped once the
        TV delay plus `_WIN_PROB_EXPIRY` has passed since it arrived.

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
        primary_reading = tracks.primary.current(now)
        fallback_reading = tracks.fallback.current(now)
        return (primary_reading if primary_reading is not None else fallback_reading), warnings

    def _surface_big_plays(
        self, game: TeamGame, events: tuple[BaseballGameEvent, ...], *, now: datetime
    ) -> list[CardId]:
        """Flash notable events as big-play cards once they clear the TV delay (once each)."""
        stream = self._event_streams.get(game.id)
        if stream is None:
            # First sight of this game: prime the backlog so plays from before we tuned
            # in don't all flush at once — only events seen on later polls surface.
            stream = DelayedEventStream(self._delay)
            self._event_streams[game.id] = stream
            for event in events:
                stream.mark_seen(event.id.raw)
        else:
            for event in events:
                if event.importance.priority >= _BIG_PLAY_MIN_BAND:
                    stream.push(
                        Observation(
                            subject_id=game.id,
                            source=event.source,
                            observed_at=now,
                            value=event,
                            source_time=event.source_time,
                        ),
                        key=event.id.raw,
                    )
        surfaced: list[CardId] = []
        for obs in stream.release(now):
            card = self._factory.big_play(game, obs.value, now=now)
            self._queue.ingest(card)
            surfaced.append(card.id)
        return surfaced

    def _surface_no_hitter(self, game: TeamGame, state: BaseballGameState, *, now: datetime) -> CardId | None:
        """Surface (or refresh) an active no-hitter / perfect-game card, or remove it once broken.

        The depth shown is the pitching side's *finished* innings (not the raw current inning),
        and a confirmed clean sheet promotes the card to a perfect game — both off the same
        delay-safe state, so the feat is revealed and un-revealed only as the broadcast would show.
        """
        feat = pitching_feat_progress(state, min_completed_innings=_NO_HITTER_MIN_COMPLETED_INNINGS)
        if feat is None:
            # No (longer) a bid — drop the card if this game had one.
            stale = self._no_hitter_keys.pop(game.id, None)
            if stale is not None:
                self._queue.remove(stale)
            return None
        card = self._factory.no_hitter(
            game, pitching_side=feat.side, through_inning=feat.completed_innings, perfect=feat.perfect, now=now
        )
        self._queue.ingest(card)
        self._no_hitter_keys[game.id] = card.dedupe_key.raw
        return card.id

    def _surface_final(
        self,
        game: TeamGame,
        state: BaseballGameState,
        decisions: PitchingDecisions | None,
        events: tuple[BaseballGameEvent, ...],
        *,
        now: datetime,
    ) -> tuple[CardId, GameEnd] | None:
        """Reveal the final card once the result clears the post-game delay, or None while it is held.

        The reveal is anchored to when we *first* saw the game final: that instant can only be
        at or after the real last out, so anchor + lag never reveals the result before the
        broadcast reaches its ending. Until then the game simply shows nothing (its live card
        was dropped when it left the live set). The card is built from the *current* state and
        decisions at reveal — same final score, but the freshest W/L/S if the feed filled the
        decisions in a touch late.

        A game's plays come before its final: the reveal also waits for every big play still inside
        the delay, and the card airs only after the game's decisive play (a walk-off) has. It is
        available from the reveal instant. Finals air in the order their games ended: the card says
        when that was (the decisive play's time, or failing that when the game was first seen final),
        and on what basis; that is returned with its id for the trace.
        """
        seen_at = self._final_pending.setdefault(game.id, now)
        reveal = self._final_reveal(game.id, seen_at)
        if reveal > now:
            return None  # inside the post-game delay — revealing the result now would spoil the ending
        stream = self._event_streams.get(game.id)
        if stream is not None and stream.pending():
            return None  # a play from before the end is still inside the delay: it airs first
        walk_offs = [
            event
            for event in events
            if event.payload.scoring_impact is not None and event.payload.scoring_impact.walk_off
        ]
        live = self._card_keys.get(game.id)
        ended = _game_end(walk_offs, seen_at)
        card = self._factory.final(
            game,
            state,
            decisions=decisions,
            now=reveal,
            after=tuple(self._factory.big_play_key(game, event) for event in walk_offs),
            replaces=DedupeKey(live) if live is not None else None,
            ended=ended,
        )
        self._queue.ingest(card)
        self._final_keys[game.id] = card.dedupe_key.raw
        del self._final_pending[game.id]
        # By the reveal, any walk-off has cleared the same delay and fired, so the stream is drained.
        self._event_streams.pop(game.id, None)
        return card.id, ended

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
        for contest_id in set(self._final_pending) - final_ids:
            del self._final_pending[contest_id]
        for contest_id in set(self._final_keys) - final_ids:
            self._queue.remove(self._final_keys.pop(contest_id))

    def _drop_absent(self, live_ids: set[LeagueScopedId], final_ids: set[LeagueScopedId]) -> tuple[LeagueScopedId, ...]:
        """Drop per-game state for games that have moved on; return the carded ones.

        Live-scoped state (feed, live card, no-hitter card) is dropped the moment the game
        leaves the live set, with one exception: a *just-ended* game whose final reveal is still
        embargoed keeps its last delay-safe live frame (card + feed) on screen — so the board
        holds that frame instead of going blank through the post-game delay — until the final
        card reveals (then the next pass, no longer pending, tears it down). The no-hitter card,
        by contrast, is removed as soon as the game leaves the live set, embargo or not (an
        in-progress bid is over once the game ends). A big-play card is left to expire on its own
        short window; the event stream keeps draining through the post-final window (so a held
        walk-off still fires), so it is dropped only once the game leaves the slate entirely — or
        earlier, at the final reveal, by which point it has drained. Only live-card removals are reported.
        """
        live_scoped = set(self._feeds) | set(self._card_keys) | set(self._no_hitter_keys)
        removed_cards: list[LeagueScopedId] = []
        for contest_id in live_scoped - live_ids:
            if contest_id not in self._final_pending:  # an embargoed final holds its last frame; do not tear down
                key = self._card_keys.pop(contest_id, None)
                if key is not None:
                    self._queue.remove(key)
                    removed_cards.append(contest_id)
                self._feeds.pop(contest_id, None)
                self._meter_tracks.pop(contest_id, None)  # win-prob buffers ride the state feed's lifecycle
            no_hitter_key = self._no_hitter_keys.pop(contest_id, None)
            if no_hitter_key is not None:
                self._queue.remove(no_hitter_key)
        for contest_id in set(self._event_streams) - (live_ids | final_ids):
            self._event_streams.pop(contest_id, None)
        for contest_id in set(self._stale) - live_ids:
            del self._stale[contest_id]
        return tuple(sorted(removed_cards, key=str))

    def _go_stale(self, game_id: LeagueScopedId, now: datetime) -> None:
        """A live game's feed didn't answer: its cards stay up marked stale, then come down after `STALE_FOR`."""
        since = self._stale.setdefault(game_id, now)
        keys = [key for key in (self._card_keys.get(game_id), self._no_hitter_keys.get(game_id)) if key is not None]
        if now - since >= STALE_FOR:
            for key in keys:
                self._queue.remove(key)
            self._card_keys.pop(game_id, None)
            self._no_hitter_keys.pop(game_id, None)
            return
        for key in keys:
            card = self._queue.card(key)
            if card is not None and card.stale_since is None:
                self._queue.ingest(replace(card, stale_since=since))

    def _final_release(self, game_id: LeagueScopedId, now: datetime) -> datetime:
        """When a held final is next due to clear: its reveal, or then its last play still in the delay."""
        reveal = self._final_reveal(game_id, self._final_pending[game_id])
        stream = self._event_streams.get(game_id)
        plays = stream.next_eligible() if stream is not None else None
        return reveal if reveal > now or plays is None else plays

    def _final_reveal(self, game_id: LeagueScopedId, seen_at: datetime) -> datetime:
        """When a game first seen final at ``seen_at`` may show its result."""
        anchor = Observation(subject_id=game_id, source=game_id.source, observed_at=seen_at, value=None)
        return self._delay.eligible_at(anchor)

    def next_deadline(self, now: datetime) -> datetime | None:
        """When the pipeline would next surface or drop something by itself, as of ``now``: a state or a play
        clearing the delay, a final's reveal, or a stale game's cards coming down."""
        instants: list[datetime | None] = [feed.next_eligible(now) for feed in self._feeds.values()]
        instants += [stream.next_eligible() for stream in self._event_streams.values()]
        instants += [self._final_reveal(game_id, seen_at) for game_id, seen_at in self._final_pending.items()]
        instants += [since + STALE_FOR for since in self._stale.values()]
        return min((instant for instant in instants if instant is not None and instant > now), default=None)


def _game_end(decisive: list[BaseballGameEvent], seen_at: datetime) -> GameEnd:
    """When a game first seen final at ``seen_at`` ended: the time of the play that ended it, when the feed
    names one no later than that, or else ``seen_at`` itself."""
    times = [event.source_time for event in decisive if event.source_time <= seen_at]
    if times:
        return GameEnd(max(times), EndBasis.DECISIVE_PLAY)
    return GameEnd(seen_at, EndBasis.FIRST_SEEN)
