"""Basketball card payloads; the teams come from the card's contest.

Two payloads cover a game's life:

- `BasketballGameCardPayload` draws a game under way, at a break, and final. A final is the same frame as the live
  card (the marks, the scores, the line score) with the meter settled and the loser dimmed, so the game's card
  can turn into its final in place, as football's does; only its bottom panel changes, to each team's best
  player on the boards that show one. One payload keeps that one frame in one renderer, and its state's phase
  says which moment it is.
- `BasketballPregameCardPayload` draws a game not yet under way, scheduled or postponed. It has no score, no
  clock and no state, but its own numbers: the tip, the standings and the betting line.

Each card's `LayoutSupport` names the compromises its renderer makes on each board (`GAME_LAYOUT`, `FINAL_LAYOUT`,
`PREGAME_LAYOUT`).

A live card's 12 s turn has two 6 s halves: the first names each team (its code over its record), the second
shows what each has left (its timeouts over the bonus). Which half is up depends on how long the card has been
on the board, so it's never part of a payload: the renderer reads it from the render context.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from omni.cards.base import LayoutSupport, ScoreboardCard
from omni.core.enum import PanelProfile
from omni.domain.baseball import WinLossRecord, WinProbability
from omni.domain.basketball import (
    UNSTARTED,
    BasketballGameState,
    BasketballPhase,
    BySide,
    FavoriteLine,
    PlayerLine,
    ScoringRun,
    StandingRanks,
    TeamBox,
    best_player,
    by_plus_minus,
)
from omni.events.basketball import BasketballCaption

__all__ = [
    "FINAL_LAYOUT",
    "GAME_LAYOUT",
    "PREGAME_LAYOUT",
    "BasketballGameCard",
    "BasketballGameCardPayload",
    "BasketballPregameCard",
    "BasketballPregameCardPayload",
    "BestPlayers",
    "BottomPanel",
    "CaptionPanel",
    "LineScorePanel",
    "PlayerLinesPanel",
    "QueuePlace",
    "TeamBoxPanel",
]


@dataclass(frozen=True, slots=True)
class QueuePlace:
    """Where a caption stands among the plays waiting their turn: ``index`` of ``count``, from 1."""

    index: int
    count: int

    def __post_init__(self) -> None:
        if self.count < 2:
            raise ValueError("a caption alone has no place in a queue")
        if not 1 <= self.index <= self.count:
            raise ValueError("a place is 1..count")


@dataclass(frozen=True, slots=True)
class LineScorePanel:
    """The bottom panel's default: points by period."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CaptionPanel:
    """One caption, a play, a timeout or a ruling, and its place among the plays waiting when there's more than
    one; None when it's alone."""

    caption: BasketballCaption
    place: QueuePlace | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamBoxPanel:
    """Each team's box-score line."""

    box: BySide[TeamBox]

    @classmethod
    def of(cls, away: Sequence[PlayerLine], home: Sequence[PlayerLine]) -> TeamBoxPanel:
        """Each team's line, summed from its players'."""
        return cls(box=BySide(TeamBox.of(away), TeamBox.of(home)))


@dataclass(frozen=True, slots=True, kw_only=True)
class PlayerLinesPanel:
    """One player from each team at the same place in its plus-minus order (``rank`` 0 for the best), away and
    home; a side is None when it has no player that far down."""

    rank: int
    players: BySide[PlayerLine | None]

    def __post_init__(self) -> None:
        if self.rank < 0:
            raise ValueError("ranks count from 0")

    @classmethod
    def ranked(cls, rank: int, away: Sequence[PlayerLine], home: Sequence[PlayerLine]) -> PlayerLinesPanel:
        """Each team's player at ``rank`` in its plus-minus order, points breaking a tie."""

        def at(lines: Sequence[PlayerLine]) -> PlayerLine | None:
            order = by_plus_minus(lines)
            return order[rank] if 0 <= rank < len(order) else None

        return cls(rank=rank, players=BySide(at(away), at(home)))


BottomPanel = LineScorePanel | CaptionPanel | TeamBoxPanel | PlayerLinesPanel
"""What the panel under a game card's divider shows this turn."""


@dataclass(frozen=True, slots=True, kw_only=True)
class BestPlayers:
    """Each team's best player at a final, by plus-minus (the higher points on a tie); None for a team with no
    player lines."""

    players: BySide[PlayerLine | None]

    @classmethod
    def of(cls, away: Sequence[PlayerLine], home: Sequence[PlayerLine]) -> BestPlayers:
        return cls(players=BySide(best_player(away), best_player(home)))


_NO_RECORDS: BySide[WinLossRecord | None] = BySide(None, None)


@dataclass(frozen=True, slots=True, kw_only=True)
class BasketballGameCardPayload:
    """A game under way, at a break, or final: its state, each club's record, the win meter, and the bottom panel.

    ``bottom`` is what the panel under the divider shows this turn; a caption only while play is on, and the line
    score at a final. ``run`` is a scoring run on while play is on. ``break_ends_at`` is when halftime is expected
    to end, for its countdown. ``best`` is each team's best player, at a final.
    """

    state: BasketballGameState
    records: BySide[WinLossRecord | None] = _NO_RECORDS  # as of tip-off; a final's include its result
    win_probability: WinProbability | None = None  # drives the meter while the game goes on; None hides it
    bottom: BottomPanel = LineScorePanel()
    run: ScoringRun | None = None
    break_ends_at: datetime | None = None
    best: BestPlayers | None = None

    def __post_init__(self) -> None:
        phase = self.state.phase
        live = phase is BasketballPhase.LIVE
        if isinstance(self.bottom, CaptionPanel) and not live:
            raise ValueError("a caption shows only while play is on")
        if phase is BasketballPhase.FINAL and not isinstance(self.bottom, LineScorePanel):
            raise ValueError("a final's bottom panel is its line score")
        if self.run is not None and not live:
            raise ValueError("a run is on only while play is on")
        if self.break_ends_at is not None:
            if self.break_ends_at.tzinfo is None:
                raise ValueError("break_ends_at must be timezone-aware")
            if phase is not BasketballPhase.HALFTIME:
                raise ValueError("only halftime counts down to its end")
        if self.best is not None and phase is not BasketballPhase.FINAL:
            raise ValueError("a game's best players are named at its final")


BasketballGameCard = ScoreboardCard[BasketballGameCardPayload]


_NO_RANKS: BySide[StandingRanks | None] = BySide(None, None)


@dataclass(frozen=True, slots=True, kw_only=True)
class BasketballPregameCardPayload:
    """A game not yet under way: when it tips, each club's record and standing, and the favorite's line.

    ``phase`` is scheduled or postponed. A scheduled game has its ``tip``; a postponed one has its new tip once
    one is set, and None until then.
    """

    phase: BasketballPhase
    tip: datetime | None
    records: BySide[WinLossRecord | None] = _NO_RECORDS
    ranks: BySide[StandingRanks | None] = _NO_RANKS
    line: FavoriteLine | None = None

    def __post_init__(self) -> None:
        if self.phase not in UNSTARTED:
            raise ValueError("a pregame card is for a game not yet under way")
        if self.tip is None and self.phase is BasketballPhase.SCHEDULED:
            raise ValueError("a scheduled game has its tip")
        if self.tip is not None and self.tip.tzinfo is None:
            raise ValueError("tip must be timezone-aware")


BasketballPregameCard = ScoreboardCard[BasketballPregameCardPayload]


_PROFILES = frozenset({PanelProfile.QUAD_128X64})
_QUAD_RECORD = "quad_128x64: from 100 points the record drops to 5x7, under a 6x10 code or timeouts"

GAME_LAYOUT = LayoutSupport(
    profiles=_PROFILES,
    compromise_notes=(_QUAD_RECORD,),
)
"""A live or break card's boards and compromises."""

FINAL_LAYOUT = LayoutSupport(
    profiles=_PROFILES,
    compromise_notes=(_QUAD_RECORD,),
)
"""A final's boards and compromises."""

PREGAME_LAYOUT = LayoutSupport(
    profiles=_PROFILES,
    compromise_notes=(
        "quad_128x64: the odds drop their spread when it would leave no blank column before the tip beside them",
    ),
)
"""A pregame or postponed card's boards and compromises."""
