"""LiveBaseballPipeline: turn the day's slate into TV-safe queued cards.

The middle of the orchestration spine. Each tick the loop hands it the current games
and a way to fetch a game's live feed; the pipeline cards each game for its phase:

- **upcoming** (scheduled / pre-game) — a `pregame` card carrying the matchup and a
  first-pitch countdown. There is no score to spoil, so it needs no live feed and no
  delay; the renderer derives the live countdown from the render clock. When sources are
  given it also carries each club's record, probable starter and odds; those change slowly,
  so they are fetched at a relaxed pace, and a failure only leaves the card showing less.
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
  - **win probability** — the live card's per-team meter. A separate, cheaper fetch whose
    *current* reading is pushed into a per-game delay feed on the same ticks as the state, so
    the surfaced reading is from the same lag-old moment as the shown score — stale-safe, never
    leading it. A market's price comes first, since unlike a model of the game state it knows how
    good the clubs are. A reading is held through a gap, up to an expiry, and past that a fallback
    source takes over. A fetch failure is non-fatal; absent until a reading has cleared the lag.
- **final** — once a game ends, a `final` card with the box score. The result is the
  ultimate spoiler, so the reveal is held until the broadcast lag elapses from when we
  first saw the game final (StatsAPI cannot report FINAL before the last out, so first
  sight + lag never predates the broadcast's ending); the card is then ingested and
  lives out a finite post-game window. Through that embargo the game's **last delay-safe
  live frame is held on screen** (its live card is not torn down until the final reveals),
  so the board shows the last-aired state rather than going blank. A walk-off that ended
  the game keeps draining from the same delay-safe event stream, so it still flashes at
  its own release time — at or just before the final — even though the game is already over.

A card is dropped when its game leaves that phase: the pregame card yields when the game
goes live; the no-hitter card is removed when the game is no longer live; the live card is
removed too, except a just-ended game holds its last delay-safe live frame through the final
embargo, then swaps to the final (a big play's own card lingers out its short window, then
self-expires); the final card lingers its post-game window. So the queue mirrors the slate.
Per-game fetch failures are isolated and reported, never fatal.

Baseball-only for now (it calls `pregame` / `score_live_baseball` / `live_baseball` /
`big_play` / `no_hitter` / `final`), mirroring the per-sport renderer split; a generic
pipeline can dispatch later.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Callable, Iterable

from omni.cards.base import CardId
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
    "WinProbFetcher",
    "PregameDetailsFetcher",
    "GameLinesFetcher",
    "PipelineResult",
    "LiveBaseballPipeline",
]

# How the pipeline pulls one game's live feed as of `now` (wraps the provider's
# per-game fetch): the current state plus the play-by-play events from the same fetch.
FeedFetcher = Callable[[TeamGame, datetime], LiveBaseballFeed]

# How the pipeline pulls one game's current win probability as of `now` (a separate, cheaper call
# than the feed). Returns None when none is available yet (pregame / a payload without it). The
# *current* reading — the pipeline delays it itself so the meter never leads the shown score.
WinProbFetcher = Callable[[TeamGame, datetime], WinProbability | None]

# How the pipeline pulls an upcoming game's records + probable starters (one request per game).
PregameDetailsFetcher = Callable[[TeamGame], PregameDetails]

# How the pipeline pulls an upcoming game's odds as of `now`, or None when none are posted. Called
# every pass, so the source is expected to hold its own board and refresh it at its own pace.
GameLinesFetcher = Callable[[TeamGame, datetime], GameLines | None]

# How long a surfaced meter reading may stand with nothing newer behind it. Long enough to ride out
# a market going quiet for a few plays; past it, the reading is no longer a fair picture of the game.
_WIN_PROB_EXPIRY = timedelta(minutes=3)

# A club's record and starter barely move before first pitch, so a slow refresh is plenty; the budget
# spreads a full slate's first fetches over several passes rather than stalling one of them.
_PREGAME_DETAILS_REFRESH = timedelta(minutes=5)
_PREGAME_DETAILS_FETCHES_PER_PASS = 3

# Pre-first-pitch states: both carry a pregame card (a scheduled game later today and one
# in warmups are both "upcoming"). Mid-game oddities (DELAYED/SUSPENDED) are not pregame.
_UPCOMING_STATUSES = frozenset({GameStatus.SCHEDULED, GameStatus.PREGAME})

# An event needs at least this intrinsic band to flash as a big play; below it (a single,
# a walk) the play informs the live card but is not worth a takeover. HR/triple/DP/TP clear it.
_BIG_PLAY_MIN_BAND = DisplayPriority.HIGH_LEVERAGE

# A no-hitter only becomes news once the pitching side has *finished* this many hitless innings;
# earlier hitless innings are routine. Counted per side so it never triggers a half-inning early.
_NO_HITTER_MIN_COMPLETED_INNINGS = 6


class _MeterTrack:
    """One source's readings on their way to the meter: delayed like the score, held through a gap."""

    def __init__(self, delay: DelayPolicy) -> None:
        self._feed: DelayedFeed[WinProbability] = DelayedFeed(delay)
        self._held: WinProbability | None = None
        self._held_at: datetime | None = None

    def push(self, game: TeamGame, reading: WinProbability, now: datetime) -> None:
        self._feed.push(Observation(subject_id=game.id, source=game.id.source, observed_at=now, value=reading))

    def current(self, now: datetime) -> WinProbability | None:
        """The newest reading clear of the delay, or the last one until it expires."""
        eligible = self._feed.latest_eligible(now)
        if eligible is not None:
            self._held, self._held_at = eligible.value, now
        elif self._held_at is not None and now - self._held_at >= _WIN_PROB_EXPIRY:
            self._held, self._held_at = None, None
        return self._held


class _MeterTracks:
    """One game's two meter sources, kept apart so a gap in one never mixes into the other's timeline."""

    def __init__(self, delay: DelayPolicy) -> None:
        self.primary = _MeterTrack(delay)
        self.fallback = _MeterTrack(delay)


def _read_win_probability(
    fetcher: WinProbFetcher | None, game: TeamGame, now: datetime, warnings: list[str], *, label: str
) -> WinProbability | None:
    """Ask one source for a reading; a failure is a warning, and just no reading this pass."""
    if fetcher is None:
        return None
    try:
        return fetcher(game, now)
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
        # contest id -> its last pregame details (None until a fetch succeeds) and when they were last tried
        self._pregame_details: dict[LeagueScopedId, tuple[PregameDetails | None, datetime]] = {}
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

        `fetch_win_probability`, when given, supplies each live game's current win probability;
        the pipeline buffers it through the same TV delay as the state so the meter never leads
        the shown score. Omitted (a test, or the meter disabled) the live card carries no meter.
        `fetch_fallback_win_probability`, when given, takes over once that source has gone quiet for
        longer than a reading may stand, or for a game it has no reading on at all.
        `fetch_pregame_details` and `fetch_game_lines`, when given, fill out the pregame card; a
        failure of either is non-fatal, and omitted the card stays at matchup + countdown.
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
        held: list[LeagueScopedId] = []
        skipped: list[str] = []
        warnings: list[str] = []

        # Upcoming games card without a live feed — there is no score to spoil, so no delay.
        fetch_budget = _PREGAME_DETAILS_FETCHES_PER_PASS
        for game in upcoming:
            details, fetched, game_warnings = self._pregame_details_for(
                game, fetch_pregame_details, may_fetch=fetch_budget > 0, now=now
            )
            fetch_budget -= fetched
            lines, line_warnings = self._game_lines_for(game, fetch_game_lines, now=now)
            warnings.extend(f"{game.id.raw}: {warning}" for warning in (*game_warnings, *line_warnings))
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
                continue
            warnings.extend(f"{game.id.raw}: {warning}" for warning in feed.warnings)

            # Big-play path first, so it runs even when the state is still delay-held.
            big_plays.extend(self._surface_big_plays(game, feed.events, now=now))

            # Live-state path: hold in the delay feed, surface only the lag-old value.
            state_feed = self._feeds.setdefault(game.id, DelayedFeed(self._delay))
            state_feed.push(Observation(subject_id=game.id, source=game.id.source, observed_at=now, value=feed.state))
            safe = state_feed.latest_eligible(now)
            if safe is None:
                held.append(game.id)  # still inside the TV delay — nothing safe to show yet
                continue
            # Win probability rides its own delay buffer, pushed on the same ticks as the state, so
            # the surfaced reading is from the same lag-old moment as the score — never a fresher one.
            win_prob, wp_warnings = self._safe_win_probability(
                game, fetch_win_probability, fetch_fallback_win_probability, now=now
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
            revealed = self._surface_final(game, feed.state, feed.decisions, now=now)
            if revealed is not None:
                finals.append(revealed)
            else:
                held.append(game.id)  # ended, but the result is still inside the post-game delay

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
        )

    def _pregame_details_for(
        self, game: TeamGame, fetcher: PregameDetailsFetcher | None, *, may_fetch: bool, now: datetime
    ) -> tuple[PregameDetails | None, int, list[str]]:
        """A game's pregame details, refreshed if due: the details, fetches spent (0 or 1), and any warning.

        A failed fetch keeps the last good details and still waits out the refresh, so a source that
        is down is not retried on every pass.
        """
        cached, tried_at = self._pregame_details.get(game.id, (None, None))
        due = tried_at is None or now - tried_at >= _PREGAME_DETAILS_REFRESH
        if fetcher is None or not due or not may_fetch:
            return cached, 0, []
        warnings: list[str] = []
        try:
            cached = fetcher(game)
        except ProviderError as exc:
            warnings.append(f"pregame details fetch failed: {exc}")
        self._pregame_details[game.id] = (cached, now)
        return cached, 1, warnings

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
        self, game: TeamGame, primary: WinProbFetcher | None, fallback: WinProbFetcher | None, *, now: datetime
    ) -> tuple[WinProbability | None, list[str]]:
        """The TV-safe win probability for this game's live card, plus any fetch-failure warnings.

        The primary source's *current* reading goes through a delay feed, so what surfaces is from the
        same ``now - lag`` moment as the state the card shows: a touch stale, but never leading the
        score. A surfaced reading is held through a gap in the source until it expires.

        While the primary has nothing, the fallback is read and delayed the same way, so it is ready
        the moment the held reading expires; while the primary answers, the fallback isn't touched.
        With neither (the meter disabled) this is a no-op. Fetch failures are non-fatal: the live card
        still shows, and each is surfaced as a warning.
        """
        tracks = self._meter_tracks.setdefault(game.id, _MeterTracks(self._delay))
        warnings: list[str] = []
        current = _read_win_probability(primary, game, now, warnings, label="win-probability")
        if current is not None:
            tracks.primary.push(game, current, now)
        else:
            backup = _read_win_probability(fallback, game, now, warnings, label="fallback win-probability")
            if backup is not None:
                tracks.fallback.push(game, backup, now)
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
        self, game: TeamGame, state: BaseballGameState, decisions: PitchingDecisions | None, *, now: datetime
    ) -> CardId | None:
        """Reveal the final card once the result clears the post-game delay, or None while it is held.

        The reveal is anchored to when we *first* saw the game final: that instant can only be
        at or after the real last out, so anchor + lag never reveals the result before the
        broadcast reaches its ending. Until then the game simply shows nothing (its live card
        was dropped when it left the live set). The card is built from the *current* state and
        decisions at reveal — same final score, but the freshest W/L/S if the feed filled the
        decisions in a touch late.
        """
        seen_at = self._final_pending.setdefault(game.id, now)
        anchor = Observation(subject_id=game.id, source=game.id.source, observed_at=seen_at, value=state)
        if self._delay.eligible_at(anchor) > now:
            return None  # inside the post-game delay — revealing the result now would spoil the ending
        card = self._factory.final(game, state, decisions=decisions, now=now)
        self._queue.ingest(card)
        self._final_keys[game.id] = card.dedupe_key.raw
        del self._final_pending[game.id]
        # By the reveal, any walk-off has cleared the same delay and fired, so the stream is drained.
        self._event_streams.pop(game.id, None)
        return card.id

    def _drop_stale_pregame(self, upcoming_ids: set[LeagueScopedId]) -> None:
        """Drop the pregame card for any game no longer upcoming — it went live, ended, or left the slate."""
        for contest_id in set(self._pregame_keys) - upcoming_ids:
            self._queue.remove(self._pregame_keys.pop(contest_id))
            self._pregame_details.pop(contest_id, None)

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
        return tuple(sorted(removed_cards, key=str))
