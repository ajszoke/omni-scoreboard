"""Priority scoring: turn a live game's state into an explainable `CardPriority`.

Per AGENTS.md, priority must not be an unexplained float — every score carries a
band and human-readable reason codes (ROADMAP's MLB signal table: favorite team,
close & late, high leverage, runners in scoring position, ...). Live baseball
only for now; other sports add their own scorers behind the same idea.
"""

from __future__ import annotations

from dataclasses import dataclass

from omni.cards.base import CardPriority
from omni.core.enum import DisplayPriority
from omni.domain.baseball import BaseballGameState
from omni.domain.contest import TeamGame

__all__ = ["PriorityScorer"]

# Signal weights (points added to the score). Named so priority isn't a magic number.
_W_FAVORITE = 30.0
_W_CLOSE_LATE = 25.0
_W_NINTH_OR_LATER = 10.0
_W_HIGH_LEVERAGE = 20.0
_W_BASES_LOADED = 8.0
_W_SCORING_POSITION = 5.0
_W_FULL_COUNT_TWO_OUT = 4.0

_LATE_INNING = 7  # 7th inning onward is "late"
_CLOSE_RUN_DIFF = 1  # within one run is "close"


@dataclass(frozen=True, slots=True)
class PriorityScorer:
    """Scores live games into a `CardPriority`.

    `favorites` are team abbreviations (e.g. ``frozenset({"COL", "LAD"})``); a
    typed favorite that also scopes by league can replace this once a second
    league exists.
    """

    favorites: frozenset[str] = frozenset()

    def score_live_baseball(self, game: TeamGame, state: BaseballGameState) -> CardPriority:
        band = DisplayPriority.NORMAL
        score = 0.0
        reasons: list[str] = []

        favorite_abbrs = [t.abbreviation for t in (game.away, game.home) if t.abbreviation in self.favorites]
        if favorite_abbrs:
            score += _W_FAVORITE
            band = max(band, DisplayPriority.FAVORITE)
            reasons.append(f"favorite team {'/'.join(favorite_abbrs)}")

        late = state.inning >= _LATE_INNING
        close = abs(state.away_score - state.home_score) <= _CLOSE_RUN_DIFF
        runners_on = state.bases.first or state.bases.second or state.bases.third

        if late and close:
            score += _W_CLOSE_LATE
            reasons.append("close & late")
            if state.inning >= 9:
                score += _W_NINTH_OR_LATER
                reasons.append("9th or later")

        if late and close and runners_on:
            # The game can swing on the next pitch.
            score += _W_HIGH_LEVERAGE
            band = max(band, DisplayPriority.HIGH_LEVERAGE)
            reasons.append("high leverage")
            if state.bases.first and state.bases.second and state.bases.third:
                score += _W_BASES_LOADED
                reasons.append("bases loaded")
        elif state.bases.second or state.bases.third:
            score += _W_SCORING_POSITION
            reasons.append("runner in scoring position")

        if state.count.outs == 2 and state.count.balls == 3 and state.count.strikes == 2:
            score += _W_FULL_COUNT_TWO_OUT
            reasons.append("full count, two outs")

        return CardPriority(band=band, score=score, reasons=tuple(reasons))
