"""The NBA live card's bottom panel: the play queue and the stat rotation, timed by the card's own turns on the board.

A live card's turn is 12 s in two 6 s halves, counted from the card's first frame on the board (the renderer's
`turn_half` reads the same count). The panel changes only on those half boundaries, so its plays line up with the
halves and the team rows keep alternating underneath. What it shows, in order:

- **The play queue.** A caption released through the delay waits its turn. One alone holds a whole 12 s turn, with no
  marker. While two or more wait, each shows for 6 s, numbered ``i/N``: ``i`` its place in the run, ``N`` the run's
  plays shown so far and still waiting, so ``N`` grows as plays arrive. The run lasts until nothing eligible waits.
  A timeout's caption and a ruling (a won challenge, a call taken back, a technical) hold a whole turn even in a run.
- **The rotation**, once no play waits: the line score, the two teams' box scores, then one player a team from each
  team's plus-minus order (the next rank each time, from the best down to `PanelPolicy.player_ranks`), a whole turn
  each, in that order, picking up where it left off whenever the card comes back. While a timeout lasts the line score
  sits out, so the stat panels rotate; without the players' lines (Free) the line score is the whole rotation. A
  stat panel gives way to a waiting play at its next half boundary, once it has had a half.

**Staging**, as the NFL caption's: a caption starts only when a frame carrying it is confirmed shown (`shown`), and
only before its latest start (60 s after its release for a routine play, 120 s for a timeout or a ruling); one no
confirmed frame carried by then is dropped, never shown late, and said so (`missed`). One shown is gone by its hard
end (90 s, 150 s), and at a break, which shows no caption. A caption the players' lines told waits for a slate read
that arrived no earlier than those lines, so the score beside it has caught up with it.

A turn the card spends off the board spends nothing: a slot starts only on a confirmed frame, and the card's next
turn on the board starts its halves afresh, its run anew.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from omni.cards.basketball import (
    BottomPanel,
    CaptionPanel,
    LineScorePanel,
    PlayerLinesPanel,
    QueuePlace,
    TeamBoxPanel,
)
from omni.core.clock import MonotonicTime
from omni.domain.basketball import BySide, PlayerLine
from omni.events.basketball import (
    BasketballCaption,
    CallReversed,
    ChallengeWon,
    TechnicalFoul,
    TimeoutTaken,
)

__all__ = ["BottomPanelQueue", "PanelPolicy", "PanelView", "QueuedCaption", "StatPanel", "whole_turn"]

_WHOLE_TURN = (TimeoutTaken, ChallengeWon, CallReversed, TechnicalFoul)


def whole_turn(caption: BasketballCaption) -> bool:
    """Whether a caption holds a whole turn even in a run, and has the longer bounds: a timeout, or a ruling."""
    return isinstance(caption, _WHOLE_TURN)


class StatPanel(Enum):
    """A stat panel of the rotation, in its order."""

    LINE_SCORE = "line score"
    TEAM_BOX = "team box"
    PLAYER_LINES = "player lines"

    @property
    def following(self) -> StatPanel:
        return _ORDER[(_ORDER.index(self) + 1) % len(_ORDER)]


_ORDER = (StatPanel.LINE_SCORE, StatPanel.TEAM_BOX, StatPanel.PLAYER_LINES)


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelPolicy:
    """How the panel is timed: a half of the card's turn, how deep the players' panel goes, and a caption's latest
    start and hard end after its release, routine or for a timeout or a ruling."""

    half: timedelta = timedelta(seconds=6)
    player_ranks: int = 5
    routine_start_within: timedelta = timedelta(seconds=60)
    routine_hard_end: timedelta = timedelta(seconds=90)
    major_start_within: timedelta = timedelta(seconds=120)
    major_hard_end: timedelta = timedelta(seconds=150)

    def bounds(self, caption: BasketballCaption) -> tuple[timedelta, timedelta]:
        """How long after its release a caption may first go up, and when it's gone."""
        if whole_turn(caption):
            return self.major_start_within, self.major_hard_end
        return self.routine_start_within, self.routine_hard_end


@dataclass(frozen=True, slots=True, kw_only=True)
class QueuedCaption:
    """A caption released through the delay, staged for the panel: its place among the game's captions, its bounds,
    and the arrival a slate read beside it must have reached (the players' lines that told it), when it has one."""

    order: int
    caption: BasketballCaption
    release: MonotonicTime
    latest_start: MonotonicTime
    hard_end: MonotonicTime
    after: MonotonicTime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelView:
    """What the card shows beside the panel this pass: whether play is on (a caption shows only then), whether a
    timeout is on, each team's players' lines (None without them), and the arrival of the slate read on the card."""

    live: bool
    timeout: bool
    lines: BySide[tuple[PlayerLine, ...]] | None
    shown: MonotonicTime | None


@dataclass(slots=True)
class _Slot:
    """What the panel shows for one or two halves: a caption (with its place in a run) or a stat panel."""

    halves: int
    caption: QueuedCaption | None = None
    place: int | None = None
    stat: StatPanel | None = None
    rank: int = 0
    started: MonotonicTime | None = None
    occurrence: int | None = None

    def carried_by(self, bottom: BottomPanel) -> bool:
        """Whether a frame's bottom panel is this slot's."""
        if self.caption is not None:
            return isinstance(bottom, CaptionPanel) and bottom.caption == self.caption.caption
        if self.stat is StatPanel.PLAYER_LINES:
            return isinstance(bottom, PlayerLinesPanel) and bottom.rank == self.rank
        kind = LineScorePanel if self.stat is StatPanel.LINE_SCORE else TeamBoxPanel
        return isinstance(bottom, kind)


class BottomPanelQueue:
    """One game's bottom panel: its waiting captions, the slot up, the run and the rotation's place."""

    def __init__(self, policy: PanelPolicy = PanelPolicy()) -> None:
        self._policy = policy
        self._waiting: list[QueuedCaption] = []
        self._slot: _Slot | None = None
        self._occurrence: int | None = None  # the card's latest turn on the board
        self._since: MonotonicTime | None = None  # its first confirmed frame
        self._on_air = False  # the latest confirmed frame was the card's
        self._run = 0  # captions started in the current run
        self._next = StatPanel.LINE_SCORE
        self._rank = 0
        self._ready: MonotonicTime | None = None  # when a play was first seen waiting behind the stat panel up
        self._missed: list[QueuedCaption] = []

    @property
    def policy(self) -> PanelPolicy:
        return self._policy

    def offer(self, caption: QueuedCaption) -> None:
        """A caption cleared the delay: it waits its turn."""
        self._waiting.append(caption)

    def shown(self, occurrence: int, at: MonotonicTime, bottom: BottomPanel) -> None:
        """A frame of the card was confirmed shown at ``at``, in its turn ``occurrence``, carrying ``bottom``: a new turn
        starts its halves; the slot it carried starts, if it hadn't (a caption only before its latest start)."""
        self._on_air = True
        slot = self._slot
        if occurrence != self._occurrence:
            self._occurrence, self._since, self._run = occurrence, at, 0
            if slot is not None and slot.started is not None:
                slot.started, slot.place = None, None  # cut short by the card's leaving: it starts again with it
        if slot is None or slot.started is not None or not slot.carried_by(bottom):
            return
        caption = slot.caption
        if caption is not None and at >= caption.latest_start:
            return  # too late: dropped at the next pass
        slot.started, slot.occurrence, self._ready = at, occurrence, None
        if caption is not None:
            self._waiting = [each for each in self._waiting if each is not caption]
            self._run = slot.place if slot.place is not None else 0

    def off_air(self) -> None:
        """Another card's frame was confirmed: this card's turn on the board is over, and its run with it."""
        self._on_air, self._run = False, 0

    def bottom(self, now: MonotonicTime, view: PanelView) -> BottomPanel:
        """What the panel shows as of ``now``: the slot up, or once it's over the next one."""
        self._expire(now)
        slot = self._slot
        if slot is not None and slot.started is not None and not self._over(slot, now, view):
            return self._draw(slot, view)
        if slot is not None and slot.started is not None:
            self._finish(slot)
        self._slot = slot = self._propose(view)
        return self._draw(slot, view)

    def missed(self) -> list[QueuedCaption]:
        """The captions dropped unseen since the last ask: past their latest start before a frame carried them."""
        missed, self._missed = self._missed, []
        return missed

    def next_change(self, now: MonotonicTime, view: PanelView) -> MonotonicTime | None:
        """When the panel next changes by itself, as of ``now``: a waiting caption's latest start, and while the card
        is up, the slot's end and, for a play waiting behind a stat panel, the next half boundary."""
        instants = [caption.latest_start for caption in self._waiting]
        slot, since = self._slot, self._since
        if self._on_air and slot is not None and since is not None:
            start = slot.started if slot.started is not None and slot.occurrence == self._occurrence else now
            half = self._half(start)
            instants.append(self._boundary(half + slot.halves))
            if slot.stat is not None and self._ready is not None:
                instants.append(self._boundary(max(half, self._half(self._ready)) + 1))
        return min((instant for instant in instants if instant > now), default=None)

    def reset(self) -> None:
        """A host's gap: the waiting captions, the slot up and the card's turn are history. The rotation's place
        stays."""
        self._waiting, self._slot, self._occurrence, self._since, self._run = [], None, None, None, 0
        self._on_air, self._ready = False, None

    # --- choosing ---------------------------------------------------------------------------------------------

    def _expire(self, now: MonotonicTime) -> None:
        """Waiting captions past their latest start are dropped, and said so; so is a proposed caption no frame
        carried in time."""
        late = [caption for caption in self._waiting if now >= caption.latest_start]
        self._waiting = [caption for caption in self._waiting if now < caption.latest_start]
        self._missed += late
        slot = self._slot
        if slot is not None and slot.started is None and slot.caption is not None and slot.caption in late:
            self._slot = None

    def _over(self, slot: _Slot, now: MonotonicTime, view: PanelView) -> bool:
        """Whether the slot up is done: its turn on the board ended, its halves ran, a caption at a break or past its
        hard end, or a stat panel at the first half boundary after it has had a half and a play was seen waiting."""
        started = slot.started
        assert started is not None
        if slot.occurrence != self._occurrence or now >= self._boundary(self._half(started) + slot.halves):
            return True
        caption = slot.caption
        if caption is not None:
            return not view.live or now >= caption.hard_end
        if self._ready is None and self._eligible(view):
            self._ready = now
        return self._ready is not None and self._half(now) > max(self._half(started), self._half(self._ready))

    def _finish(self, slot: _Slot) -> None:
        """A slot that ran: a stat panel moves the rotation on."""
        if slot.stat is None:
            return
        self._next = slot.stat.following
        if slot.stat is StatPanel.PLAYER_LINES:
            self._rank = slot.rank + 1

    def _propose(self, view: PanelView) -> _Slot:
        """The next slot: the first eligible caption, in a run while two or more wait; otherwise the rotation's next
        stat panel."""
        eligible = self._eligible(view)
        if eligible:
            caption = eligible[0]
            running = self._run > 0 or len(eligible) >= 2
            halves = 2 if whole_turn(caption.caption) or not running else 1
            return _Slot(halves=halves, caption=caption, place=self._run + 1 if running else None)
        self._run = 0
        stat = self._next
        while stat not in self._cycle(view):
            stat = stat.following
        self._next = stat
        rank = 0
        if stat is StatPanel.PLAYER_LINES:
            assert view.lines is not None  # the players' panel is in the rotation only with the lines
            depth = min(self._policy.player_ranks, max(len(view.lines.away), len(view.lines.home)))
            rank = self._rank = self._rank if self._rank < depth else 0
        return _Slot(halves=2, stat=stat, rank=rank)

    def _eligible(self, view: PanelView) -> list[QueuedCaption]:
        """The waiting captions that may go up beside the card as it is: only while play is on, and one the players'
        lines told only beside a slate read no older than those lines."""
        if not view.live:
            return []
        return [
            caption
            for caption in self._waiting
            if caption.after is None or (view.shown is not None and view.shown >= caption.after)
        ]

    @staticmethod
    def _cycle(view: PanelView) -> Sequence[StatPanel]:
        """The stat panels the rotation runs through now."""
        if view.lines is None:
            return (StatPanel.LINE_SCORE,)
        if view.timeout:
            return (StatPanel.TEAM_BOX, StatPanel.PLAYER_LINES)
        return _ORDER

    def _draw(self, slot: _Slot, view: PanelView) -> BottomPanel:
        """The slot as the payload carries it: a caption with its place while others wait, or a stat panel."""
        caption = slot.caption
        if caption is not None:
            place = None
            if slot.place is not None:
                count = slot.place + len([each for each in self._eligible(view) if each is not caption])
                place = QueuePlace(slot.place, count) if count >= 2 else None
            return CaptionPanel(caption=caption.caption, place=place)
        if slot.stat is StatPanel.LINE_SCORE or view.lines is None:
            return LineScorePanel()
        away, home = view.lines.away, view.lines.home
        if slot.stat is StatPanel.TEAM_BOX:
            return TeamBoxPanel.of(away, home)
        return PlayerLinesPanel.ranked(slot.rank, away, home)

    # --- the card's halves ------------------------------------------------------------------------------------

    def _half(self, at: MonotonicTime) -> int:
        """Which half of the card's time on the board ``at`` falls in."""
        since = self._since
        assert since is not None
        return int(max(at.since(since), timedelta(0)) // self._policy.half)

    def _boundary(self, half: int) -> MonotonicTime:
        """When the card's ``half``-th half begins."""
        since = self._since
        assert since is not None
        return since + self._policy.half * half
