"""BoardSession: the board's airtime, one card at a time, chosen by the card queue and held by its dwell.

The board is a surface (`Surface.BOARD`) like the play strip, under the same contract: a card airs as a
segment, the display's commit starts its dwell, and what happened is reported for the trace. What
differs is how the next one is chosen. The strip airs its segments in order; the board asks the card
queue what should air (`InterleavedCardQueue.peek`), and books the card in the queue once its first
frame is committed. So a card's rotation turn, a reminder's cooldown and a takeover are spent only on
airtime the viewer actually got.

A card on air:
- comes down at once if it leaves the queue (its game moved on: factual invalidation), if it expires,
  or if its source goes offline;
- yields, once it has had its minimum, to a card with a stronger claim: a takeover over a reminder or
  rotation, a reminder over rotation;
- yields, once it has had its turn (its *slot*), to any other card the queue offers, or to nothing if
  the queue no longer offers it (a reminder whose beat has passed);
- otherwise stays. A card with nothing else waiting isn't taken down to show nothing.

A card's slot is the board's rotation slot, or a takeover's own `takeover_for`, held within the card's
minimum and maximum display. Takeovers back to back are bounded: once they have run the policy's cap,
a rotation card airs before the next one may start.

When no card should be up, the board says so with an explicit frame: *idle* (nothing to show), *held*
(games are on, but nothing has cleared the TV delay yet) or *offline* (the source has gone quiet past
its limit or never answered, or a game's feed has stopped answering and its card has come down). A card
whose data is past its age airs marked *stale*.

Availability and expiry are UTC instants; dwell and slots run on the monotonic clock. The session runs
on the frame thread; the decision thread updates the queue (which is thread-safe) and the health.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Any, Union

from omni.cards.base import ScoreboardCard
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import PanelProfile, StrEnumMixin
from omni.core.ids import SourceRef
from omni.queue.scheduler import InterleavedCardQueue, Pick, SelectionReason
from omni.tower.lane import ChangeKind, EndReason, LaneChange, earliest
from omni.tower.segment import Dwell, Provenance, Segment, SegmentKey, Surface

__all__ = [
    "BoardCue",
    "BoardFrame",
    "BoardHealth",
    "BoardPayload",
    "BoardPolicy",
    "BoardSession",
    "FrameKind",
    "SourceHealth",
]

_BOARD_SOURCE = SourceRef("board")
_NO_DWELL = Dwell(minimum=timedelta(0), maximum=timedelta(0))


class FrameKind(StrEnumMixin, str, Enum):
    """An explicit frame, for when no card should be on the board."""

    IDLE = "idle"  # nothing to show
    HELD = "held"  # games are on, but nothing has cleared the TV delay yet
    OFFLINE = "offline"  # the source, or a game's feed, has gone quiet past its limit


@dataclass(frozen=True, slots=True)
class BoardFrame:
    """An explicit frame and why it's up."""

    kind: FrameKind
    reason: str


class SourceHealth(StrEnumMixin, str, Enum):
    """How far the board can trust its source."""

    FRESH = "fresh"
    STALE = "stale"  # its last answer is past its age: cards air marked stale
    OFFLINE = "offline"  # never answered, or silent past its limit: its cards come off the board


@dataclass(frozen=True, slots=True, kw_only=True)
class BoardHealth:
    """What the latest decision found: the source's health, and how many games wait out the TV delay."""

    source: SourceHealth = SourceHealth.FRESH
    reason: str = ""  # why the source is stale or offline
    held: int = 0  # games with nothing on the board yet because their card is inside the TV delay
    unfed: int = 0  # games with nothing on the board because their feed has stopped answering


@dataclass(frozen=True, slots=True, kw_only=True)
class BoardPolicy:
    """How the board shares its airtime."""

    slot: timedelta = timedelta(seconds=12)  # a rotation card's turn, held within its own min and max
    takeover_cap: timedelta = timedelta(seconds=30)  # takeover airtime back to back before a rotation card

    def __post_init__(self) -> None:
        if self.slot <= timedelta(0) or self.takeover_cap <= timedelta(0):
            raise ValueError("a board's slot and takeover cap must be positive")


BoardPayload = Union[ScoreboardCard[Any], BoardFrame]


@dataclass(frozen=True, slots=True)
class BoardCue:
    """What should be on the board now: one occurrence of a card or a frame."""

    segment: Segment[BoardPayload]
    occurrence: int  # counts what has gone up: the same card up again is a new occurrence
    reason: SelectionReason | None = None  # why the queue chose the card; None for a frame
    stale: bool = False  # its data is past its age: draw it marked stale

    @property
    def payload(self) -> BoardPayload:
        return self.segment.frames[0]


@dataclass(slots=True)
class _OnAir:
    segment: Segment[BoardPayload]
    occurrence: int
    pick: Pick | None  # None for a frame
    slot: timedelta
    since: MonotonicTime | None = None  # when its first frame was committed


class BoardSession:
    """The board surface's schedule: what's on air, why, and when it goes."""

    def __init__(self, queue: InterleavedCardQueue, profile: PanelProfile, *, policy: BoardPolicy = BoardPolicy()):
        self._queue = queue
        self._profile = profile
        self._policy = policy
        self._health = BoardHealth()
        self._on_air: _OnAir | None = None
        self._occurrences = 0
        self._takeover_run = timedelta(0)  # takeover airtime since the last rotation card or frame went up
        self._changes: list[LaneChange[BoardPayload]] = []

    @property
    def health(self) -> BoardHealth:
        return self._health

    @health.setter
    def health(self, health: BoardHealth) -> None:
        self._health = health

    def cue(self, at: ClockReading) -> BoardCue:
        """What should be on the board at ``at``, once the rules have run: the card up, the next card, or
        an explicit frame."""
        health = self._health
        on = self._on_air
        if on is not None and on.pick is not None:
            self._review(on, health, at)
        on = self._on_air
        if on is None or on.pick is None:
            pick = None
            if health.source is not SourceHealth.OFFLINE:
                pick = self._queue.peek(at, self._profile, takeovers=self._takeovers_allowed(at))
            if pick is not None:
                self._air_card(pick, at)
            elif health.source is SourceHealth.OFFLINE:
                self._air_frame(BoardFrame(FrameKind.OFFLINE, health.reason), at)
            elif health.unfed:
                self._air_frame(BoardFrame(FrameKind.OFFLINE, f"no feed for {_games(health.unfed)}"), at)
            elif health.held:
                self._air_frame(BoardFrame(FrameKind.HELD, f"{_games(health.held)} in the TV delay"), at)
            else:
                self._air_frame(BoardFrame(FrameKind.IDLE, "nothing eligible"), at)
        return self._cue(health)

    def committed(self, cue: BoardCue, at: ClockReading) -> None:
        """The display drew ``cue`` at ``at``. A card's first commit starts its dwell and books it."""
        on = self._on_air
        if on is None or cue.occurrence != on.occurrence or on.since is not None:
            return  # no longer on air, or its dwell already runs
        on.since = at.monotonic
        if on.pick is not None:
            self._queue.book(on.pick, at)
        else:
            self._takeover_run = timedelta(0)  # a quiet board: takeovers before it weren't back to back
        self._changes.append(LaneChange(ChangeKind.UP, on.segment, at))

    def next_deadline(self, at: ClockReading) -> MonotonicTime | None:
        """When the board will next change by itself, as of ``at``.

        None means only a change in the queue, the health or a commit can change it.
        """
        on = self._on_air
        if on is None:
            return at.monotonic  # nothing has been cued yet
        upcoming = self._queue.next_deadline(at, self._profile)
        if on.pick is None or on.since is None:
            return upcoming  # a frame stays until a card can replace it; a card's dwell waits on its commit
        deadlines = [upcoming, on.since + on.segment.dwell.minimum, on.since + on.slot]
        expires = on.segment.expires_at
        if expires is not None:
            deadlines.append(at.monotonic + (expires - at.utc))
        return earliest(deadline for deadline in deadlines if deadline is None or deadline > at.monotonic)

    def changes(self) -> list[LaneChange[BoardPayload]]:
        """What happened since the last ask, oldest first."""
        changes, self._changes = self._changes, []
        return changes

    def _review(self, on: _OnAir, health: BoardHealth, at: ClockReading) -> None:
        """Take the card on air down if its time is up, or keep it with its newest version."""
        assert on.pick is not None
        if health.source is SourceHealth.OFFLINE:
            self._down(EndReason.CLEARED, at)
            return
        expires = on.segment.expires_at
        if expires is not None and at.utc >= expires:
            self._down(EndReason.EXPIRED, at)  # whether or not a refresh has pruned it yet
            return
        latest = self._queue.card(on.segment.key.subject)
        if latest is None:
            self._down(EndReason.INVALIDATED, at)
            return
        if latest is not on.segment.frames[0]:
            on.segment = _card_segment(latest, revision=on.segment.revision + 1)  # same dwell: a cosmetic edit
        if on.since is None:
            return  # not drawn yet: no time has run
        up = at.monotonic.since(on.since)
        if up < on.segment.dwell.minimum:
            return  # ordinary priority never cuts a visible minimum
        best = self._queue.peek(at, self._profile, takeovers=self._takeovers_allowed(at))
        if best is None:
            if up >= on.slot:
                self._down(EndReason.RAN_ITS_TIME, at)  # the queue no longer offers it, and its turn is over
        elif best.card.dedupe_key.raw == on.segment.key.subject:
            return  # nothing else wants the board
        elif best.reason.rank > on.pick.reason.rank:
            self._down(EndReason.NEXT_WAITING, at)  # a stronger claim, once this had its minimum
        elif up >= on.slot:
            self._down(EndReason.RAN_ITS_TIME, at)

    def _takeovers_allowed(self, at: ClockReading) -> bool:
        """Whether a takeover may start: the takeover airtime back to back, the one up included, is under the
        cap, or the board's own rotation turn has run since."""
        run = self._takeover_run
        on = self._on_air
        if on is not None and on.pick is not None and on.since is not None:
            up = at.monotonic.since(on.since)
            if on.pick.reason is SelectionReason.TAKEOVER:
                run += up
            elif on.pick.reason is SelectionReason.ROTATION and up >= on.slot:
                run = timedelta(0)
        return run < self._policy.takeover_cap

    def _air_card(self, pick: Pick, at: ClockReading) -> None:
        if self._on_air is not None:
            self._down(EndReason.NEXT_WAITING, at)  # a frame gives way to a card at once
        segment = _card_segment(pick.card, revision=0)
        dwell = segment.dwell
        turn = pick.card.attention.takeover_for.as_timedelta() if pick.reason is SelectionReason.TAKEOVER else None
        slot = min(max(turn if turn is not None else self._policy.slot, dwell.minimum), dwell.maximum)
        self._occurrences += 1
        self._on_air = _OnAir(segment=segment, occurrence=self._occurrences, pick=pick, slot=slot)

    def _air_frame(self, frame: BoardFrame, at: ClockReading) -> None:
        on = self._on_air
        if on is not None and on.pick is None:
            current = on.segment.frames[0]
            assert isinstance(current, BoardFrame)
            if current.kind is frame.kind:
                if current != frame:
                    on.segment = _frame_segment(frame, at, revision=on.segment.revision + 1)
                return
        if on is not None:
            self._down(EndReason.NEXT_WAITING if frame.kind is not FrameKind.OFFLINE else EndReason.CLEARED, at)
        self._occurrences += 1
        self._on_air = _OnAir(
            segment=_frame_segment(frame, at, revision=0), occurrence=self._occurrences, pick=None, slot=timedelta(0)
        )

    def _down(self, reason: EndReason, at: ClockReading) -> None:
        on = self._on_air
        assert on is not None
        aired = at.monotonic.since(on.since) if on.since is not None else None
        if aired is not None and on.pick is not None:
            if on.pick.reason is SelectionReason.TAKEOVER:
                self._takeover_run += aired
            elif on.pick.reason is SelectionReason.ROTATION:
                self._takeover_run = timedelta(0)  # the board's own turn has run: takeovers may start again
        kind = ChangeKind.DOWN if aired is not None else ChangeKind.DROPPED
        self._changes.append(LaneChange(kind, on.segment, at, reason=reason, aired=aired))
        self._on_air = None

    def _cue(self, health: BoardHealth) -> BoardCue:
        on = self._on_air
        assert on is not None
        if on.pick is None:
            return BoardCue(on.segment, on.occurrence)
        card = on.segment.frames[0]
        assert not isinstance(card, BoardFrame)
        stale = health.source is SourceHealth.STALE or card.stale_since is not None
        return BoardCue(on.segment, on.occurrence, reason=on.pick.reason, stale=stale)


def _games(count: int) -> str:
    return "a game" if count == 1 else f"{count} games"


def _card_segment(card: ScoreboardCard[Any], *, revision: int) -> Segment[BoardPayload]:
    """A card as a board segment: its dedupe key, its dwell, its availability and expiry."""
    timing = card.timing
    return Segment(
        key=SegmentKey(Surface.BOARD, card.dedupe_key.raw),
        frames=(card,),
        dwell=Dwell(minimum=timing.min_display.as_timedelta(), maximum=timing.max_display.as_timedelta()),
        provenance=Provenance(card.contest.id.source, received_at=timing.available_at),
        available_at=timing.available_at,
        expires_at=timing.expires_at,
        revision=revision,
        parent=str(card.contest.id),
    )


def _frame_segment(frame: BoardFrame, at: ClockReading, *, revision: int) -> Segment[BoardPayload]:
    """An explicit frame as a board segment: up until a card can replace it."""
    return Segment(
        key=SegmentKey(Surface.BOARD, f"frame:{frame.kind.value}"),
        frames=(frame,),
        dwell=_NO_DWELL,
        provenance=Provenance(_BOARD_SOURCE, received_at=at.utc),
        available_at=at.utc,
        revision=revision,
    )
