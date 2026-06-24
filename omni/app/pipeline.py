"""LiveBaseballPipeline: turn live games + their feeds into TV-safe queued cards.

The middle of the orchestration spine. Each tick the loop hands it the current games
and a way to fetch a game's live feed (state + play-by-play events); for every LIVE
game the pipeline runs two delay-safe paths:

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

Cards/feeds/streams for games no longer live are removed, so the queue mirrors the
live slate (a big play's own card lingers out its short window, then self-expires).
Per-game fetch failures are isolated and reported, never fatal.

Baseball-only for now (it calls `score_live_baseball` / `live_baseball` / `big_play` /
`no_hitter`), mirroring the per-sport renderer split; a generic pipeline can dispatch later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from omni.cards.base import CardId
from omni.cards.factory import CardFactory
from omni.core.enum import DisplayPriority, GameStatus
from omni.core.ids import LeagueScopedId
from omni.core.observation import Observation
from omni.domain.baseball import BaseballGameState, no_hitter_side
from omni.domain.contest import TeamGame
from omni.events.baseball import BaseballGameEvent, LiveBaseballFeed
from omni.providers.base import ProviderError
from omni.queue.delay_policy import DelayPolicy
from omni.queue.delayed_event_stream import DelayedEventStream
from omni.queue.delayed_feed import DelayedFeed
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue

__all__ = ["FeedFetcher", "PipelineResult", "LiveBaseballPipeline"]

# How the pipeline pulls one game's live feed as of `now` (wraps the provider's
# per-game fetch): the current state plus the play-by-play events from the same fetch.
FeedFetcher = Callable[[TeamGame, datetime], LiveBaseballFeed]

# An event needs at least this intrinsic band to flash as a big play; below it (a single,
# a walk) the play informs the live card but is not worth a takeover. HR/triple/DP/TP clear it.
_BIG_PLAY_MIN_BAND = DisplayPriority.HIGH_LEVERAGE

# A no-hitter only becomes news once the game is this deep — earlier hitless innings are routine.
_NO_HITTER_MIN_INNING = 6


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineResult:
    """What one pipeline pass did (ids sorted for deterministic traces)."""

    ingested: tuple[CardId, ...]  # live-state cards built/refreshed this pass
    big_plays: tuple[CardId, ...]  # big-play cards surfaced this pass (chronological)
    no_hitters: tuple[CardId, ...]  # active no-hitter cards surfaced/refreshed this pass
    held: tuple[LeagueScopedId, ...]  # live games still buffering — no TV-safe state yet
    removed: tuple[LeagueScopedId, ...]  # games no longer live whose card was dropped
    skipped: tuple[str, ...]  # per-game fetch failures (warnings)


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
        self._feeds: dict[LeagueScopedId, DelayedFeed[BaseballGameState]] = {}
        self._card_keys: dict[LeagueScopedId, str] = {}  # contest id -> its live card's dedupe key
        self._event_streams: dict[LeagueScopedId, DelayedEventStream[BaseballGameEvent]] = {}
        self._no_hitter_keys: dict[LeagueScopedId, str] = {}  # contest id -> its no-hitter card's key

    def refresh(self, games: Iterable[TeamGame], *, now: datetime, fetch_feed: FeedFetcher) -> PipelineResult:
        """Observe live games, surface lag-safe cards into the queue, drop stale ones."""
        live = [game for game in games if game.status is GameStatus.LIVE]
        live_ids = {game.id for game in live}

        ingested: list[CardId] = []
        big_plays: list[CardId] = []
        no_hitters: list[CardId] = []
        held: list[LeagueScopedId] = []
        skipped: list[str] = []

        for game in live:
            try:
                feed = fetch_feed(game, now)
            except ProviderError as exc:
                skipped.append(f"{game.id.raw}: {exc}")
                continue

            # Big-play path first, so it runs even when the state is still delay-held.
            big_plays.extend(self._surface_big_plays(game, feed.events, now=now))

            # Live-state path: hold in the delay feed, surface only the lag-old value.
            state_feed = self._feeds.setdefault(game.id, DelayedFeed(self._delay))
            state_feed.push(Observation(subject_id=game.id, source=game.id.source, observed_at=now, value=feed.state))
            safe = state_feed.latest_eligible(now)
            if safe is None:
                held.append(game.id)  # still inside the TV delay — nothing safe to show yet
                continue
            priority = self._scorer.score_live_baseball(game, safe.value)
            card = self._factory.live_baseball(game, safe.value, now=now, priority=priority)
            self._queue.ingest(card)
            self._card_keys[game.id] = card.dedupe_key.raw
            ingested.append(card.id)

            # No-hitter rides the same delay-safe state, so it is revealed/broken only as aired.
            no_hit = self._surface_no_hitter(game, safe.value, now=now)
            if no_hit is not None:
                no_hitters.append(no_hit)

        removed = self._drop_absent(live_ids)
        return PipelineResult(
            ingested=tuple(ingested),
            big_plays=tuple(big_plays),
            no_hitters=tuple(no_hitters),
            held=tuple(sorted(held, key=str)),
            removed=tuple(removed),
            skipped=tuple(skipped),
        )

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
        """Surface (or refresh) an active no-hitter card, or remove it once the bid is broken."""
        side = no_hitter_side(state, min_inning=_NO_HITTER_MIN_INNING)
        if side is None:
            # No (longer) a bid — drop the card if this game had one.
            stale = self._no_hitter_keys.pop(game.id, None)
            if stale is not None:
                self._queue.remove(stale)
            return None
        card = self._factory.no_hitter(game, pitching_side=side, through_inning=state.inning, now=now)
        self._queue.ingest(card)
        self._no_hitter_keys[game.id] = card.dedupe_key.raw
        return card.id

    def _drop_absent(self, live_ids: set[LeagueScopedId]) -> tuple[LeagueScopedId, ...]:
        """Drop cards/feeds/streams for games no longer live; return the carded ones.

        A big-play card is left to expire on its own short window (it may have fired as
        the game ended); the live and no-hitter cards are actively removed (a no-hitter
        ends with its game). Only live-card removals are reported in `removed`.
        """
        tracked = set(self._feeds) | set(self._card_keys) | set(self._event_streams) | set(self._no_hitter_keys)
        removed_cards: list[LeagueScopedId] = []
        for contest_id in tracked - live_ids:
            key = self._card_keys.pop(contest_id, None)
            if key is not None:
                self._queue.remove(key)
                removed_cards.append(contest_id)
            no_hitter_key = self._no_hitter_keys.pop(contest_id, None)
            if no_hitter_key is not None:
                self._queue.remove(no_hitter_key)
            self._feeds.pop(contest_id, None)
            self._event_streams.pop(contest_id, None)
        return tuple(sorted(removed_cards, key=str))
