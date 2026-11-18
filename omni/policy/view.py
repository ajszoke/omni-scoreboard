"""What the board's policy reads at each decision, published whole with the cards: one released generation.

Each refresh, the league pipelines describe every game they card from **released** state only (what has cleared
the viewer's delay), never from a raw receipt: its leverage, whether its state is current, its records and
stakes, the break evidence and the newest action that have cleared the delay, and the cards they offer for
promotion. The loop publishes this beside the queue's `CardView` (`omni.tower.board.BoardInput`), so a decision
never reads half a refresh.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from omni.cards.base import DedupeKey
from omni.core.ids import LeagueScopedId
from omni.domain.breaks import BreakEvidence
from omni.domain.contest import TeamGame
from omni.events.break_detection import ActionEvidence
from omni.policy.leverage import Leverage
from omni.policy.pool import MatchupRecords
from omni.policy.promotion import PromotionCandidate

__all__ = ["EMPTY_POLICY_VIEW", "GameFacts", "PolicyView"]


@dataclass(frozen=True, slots=True, kw_only=True)
class GameFacts:
    """One game, as the policy may read it: released state only."""

    game: TeamGame
    leverage: Leverage  # from the released state: the labeled proxy until the league's model passes validation
    fresh: bool  # its released state is current (its card isn't stale)
    records: MatchupRecords | None = None  # both teams' records as they stood at the start; None when unknown
    cup_knockout: bool = False  # an NBA Cup knockout game, which the schedule's stage can't say
    breaks: tuple[BreakEvidence, ...] = ()  # released break evidence, the newest revision of each break
    action: ActionEvidence | None = None  # the newest released action

    def __post_init__(self) -> None:
        for evidence in self.breaks:
            if evidence.id.contest != self.game.id:
                raise ValueError(f"break evidence for {evidence.id.contest} can't describe {self.game.id}")
        if self.action is not None and self.action.contest != self.game.id:
            raise ValueError(f"action in {self.action.contest} can't describe {self.game.id}")


@dataclass(frozen=True, slots=True)
class PolicyView:
    """Every carded game's facts, by its id, and the cards offered for promotion (each by its dedupe key in the
    same refresh's `CardView`), as one value."""

    games: Mapping[LeagueScopedId, GameFacts] = field(default_factory=lambda: MappingProxyType({}))
    offers: tuple[PromotionCandidate[DedupeKey], ...] = ()

    def __post_init__(self) -> None:
        for contest, facts in self.games.items():
            if facts.game.id != contest:
                raise ValueError(f"{facts.game.id}'s facts are filed under {contest}")

    def facts(self, contest: LeagueScopedId) -> GameFacts | None:
        return self.games.get(contest)


EMPTY_POLICY_VIEW = PolicyView()
