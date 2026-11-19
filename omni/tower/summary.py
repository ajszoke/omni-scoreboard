"""The unseen major's result: a promotion that ran out, kept as a still of its story for its game's ordered final
(`BOARD_POLICY.md` §4, §6).

A promoted event must start within its lifetime, and is gone 120 s after it became eligible. When a decisive event
runs out unseen (its moment never came: a favorite was in action), its story isn't lost with it. Its game's final
airs in end order, and its first presentation is a `ResultSummary`: a still of the event's own card, as it was
offered for that revision, with the final's confirmed score and a safe generic label in place of the play's text.

- **What transfers:** only a verified terminal consequence: a decisive event that its game's final names as the play
  that decided it, or a confirmed rare feat. Ordinary action, whose state is obsolete once the game moved on, is
  dropped.
- **Identity:** the summary carries its event's promotion key and revision, and draws under its own presentation id
  (the event card's id, with ``:result``), while the final keeps its own key and its place in end order. Once the
  event's story has aired, by promotion, in its game's turn or as this summary, it never airs again.
- **Freshness:** it may lead its final until the earlier of the final's own expiry and five minutes after the
  verified final became eligible (`SummaryPolicy`). Past that, or when it can't be drawn on the panel, the final
  airs plain and says the play that decided it is gone.
- **The drawing** reuses an existing renderer statically: an MLB big play's still, the event's headline over the
  label and the final's score. A payload with no static still yet (football's per-event card) can't be drawn, so its
  final airs plain.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from omni.cards.base import CardId, ScoreboardCard
from omni.cards.baseball import BigPlayCardPayload, FinalCardPayload
from omni.core.clock import MonotonicTime
from omni.policy.interrupts import InterruptClass
from omni.policy.promotion import PromotionKey

__all__ = ["SUMMARY_LABEL", "ResultSummary", "SummaryPolicy", "terminal"]

SUMMARY_LABEL = "FINAL"  # in place of the play's own text, which may describe a state the final has moved past


@dataclass(frozen=True, slots=True, kw_only=True)
class SummaryPolicy:
    """How long an unseen event's story may lead its final: until this long after the verified final became
    eligible (or the final's own expiry, if that's sooner)."""

    fresh_for: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if self.fresh_for <= timedelta(0):
            raise ValueError("a summary's freshness must be positive")


def terminal(event: InterruptClass) -> bool:
    """Whether an event's story may outlive its promotion: a decisive event, or a confirmed rare feat."""
    return event is InterruptClass.DECISIVE_LATE_WINNER or event.feat


@dataclass(frozen=True, slots=True, kw_only=True)
class ResultSummary:
    """An unseen event's story, kept for its game's ordered final: its promotion's key and revision, its class, the
    event's own card as offered for that revision, and how long it's remembered without a final to lead."""

    key: PromotionKey
    revision: int
    event: InterruptClass
    card: ScoreboardCard[Any]  # the event's card, as it was offered at ``revision``
    until: MonotonicTime  # forgotten after this, if no final has taken it up

    def __post_init__(self) -> None:
        if not terminal(self.event):
            raise ValueError("only a decisive event or a confirmed rare feat outlives its promotion")

    @property
    def id(self) -> CardId:
        """Its own presentation identity: the event card's id, as a result."""
        return CardId(f"{self.card.id.raw}:result")

    def leads(self, final: ScoreboardCard[Any]) -> bool:
        """Whether ``final`` names the event as the play that decided it."""
        return self.card.dedupe_key in final.after

    def drawn(self, final: ScoreboardCard[Any]) -> ScoreboardCard[Any] | None:
        """``final``'s first presentation drawn as this summary: the event's still, with the final's confirmed score
        and the generic label, under the final's key and timing so the board keeps treating it as the final. None
        when the event has no static still yet (only an MLB big play has one), or the final isn't a baseball final."""
        payload, result = self.card.payload, final.payload
        if not isinstance(payload, BigPlayCardPayload) or not isinstance(result, FinalCardPayload):
            return None
        return replace(
            final,
            id=self.id,
            kind=self.card.kind,
            layout_support=self.card.layout_support,
            payload=replace(
                payload, description=SUMMARY_LABEL, away_score=result.away_score, home_score=result.home_score
            ),
            source_event_ids=self.card.source_event_ids,
        )
