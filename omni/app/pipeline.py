"""LiveBaseballPipeline: turn live games + their states into TV-safe queued cards.

The middle of the orchestration spine. Each tick the loop hands it the current games
and a way to fetch a game's live state; for every LIVE game the pipeline observes the
state, holds it in a per-game `DelayedFeed` (so what surfaces is lag-old and never
spoils the broadcast), scores it, builds a card via `CardFactory`, and ingests it
into the queue. Cards (and feeds) for games no longer live are removed, so the queue
mirrors exactly the live slate. Per-game fetch failures are isolated and reported,
never fatal.

Baseball-only for now (it calls `score_live_baseball` / `live_baseball`), mirroring
the per-sport renderer split; a generic pipeline can dispatch to per-sport ones later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from omni.cards.base import CardId
from omni.cards.factory import CardFactory
from omni.core.enum import GameStatus
from omni.core.ids import LeagueScopedId
from omni.core.observation import Observation
from omni.domain.baseball import BaseballGameState
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.providers.base import ProviderError
from omni.queue.delay_policy import DelayPolicy
from omni.queue.delayed_feed import DelayedFeed
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue

__all__ = ["FeedFetcher", "PipelineResult", "LiveBaseballPipeline"]

# How the pipeline pulls one game's live feed as of `now` (wraps the provider's
# per-game fetch): the current state plus the play-by-play events from the same fetch.
FeedFetcher = Callable[[TeamGame, datetime], LiveBaseballFeed]


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineResult:
    """What one pipeline pass did (ids sorted for deterministic traces)."""

    ingested: tuple[CardId, ...]  # cards built/refreshed this pass
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

    def refresh(self, games: Iterable[TeamGame], *, now: datetime, fetch_feed: FeedFetcher) -> PipelineResult:
        """Observe live games, surface lag-safe cards into the queue, drop stale ones."""
        live = [game for game in games if game.status is GameStatus.LIVE]
        live_ids = {game.id for game in live}

        ingested: list[CardId] = []
        held: list[LeagueScopedId] = []
        skipped: list[str] = []

        for game in live:
            try:
                state = fetch_feed(game, now).state  # events ride along; consumed in B1(4b-ii)
            except ProviderError as exc:
                skipped.append(f"{game.id.raw}: {exc}")
                continue
            feed = self._feeds.setdefault(game.id, DelayedFeed(self._delay))
            feed.push(Observation(subject_id=game.id, source=game.id.source, observed_at=now, value=state))
            safe = feed.latest_eligible(now)
            if safe is None:
                held.append(game.id)  # still inside the TV delay — nothing safe to show yet
                continue
            priority = self._scorer.score_live_baseball(game, safe.value)
            card = self._factory.live_baseball(game, safe.value, now=now, priority=priority)
            self._queue.ingest(card)
            self._card_keys[game.id] = card.dedupe_key.raw
            ingested.append(card.id)

        removed = self._drop_absent(live_ids)
        return PipelineResult(
            ingested=tuple(ingested),
            held=tuple(sorted(held, key=str)),
            removed=tuple(removed),
            skipped=tuple(skipped),
        )

    def _drop_absent(self, live_ids: set[LeagueScopedId]) -> tuple[LeagueScopedId, ...]:
        """Remove cards + feeds for games that are no longer live; return the carded ones."""
        tracked = set(self._feeds) | set(self._card_keys)
        removed_cards: list[LeagueScopedId] = []
        for contest_id in tracked - live_ids:
            key = self._card_keys.pop(contest_id, None)
            if key is not None:
                self._queue.remove(key)
                removed_cards.append(contest_id)
            self._feeds.pop(contest_id, None)
        return tuple(sorted(removed_cards, key=str))
