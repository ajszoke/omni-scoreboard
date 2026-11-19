"""BoardSession: the board's airtime, one card at a time, chosen by the card queue and held by its dwell.

The board is a surface (`Surface.BOARD`) like the play strip, under the same contract: a card airs as a
segment, the display's commit starts its dwell, and what happened is reported for the trace. What
differs is how the next one is chosen. The strip airs its segments in order; the board asks the card
queue what should air (`InterleavedCardQueue.peek`), and books the card in the queue once its first
frame is committed. So a card's rotation turn, a reminder's cooldown and a takeover are spent only on
airtime the viewer actually got.

A refresh publishes the board's input whole (`publish`): the queue's cards as one numbered view, and the
source's health with them. The board decides against the latest input only, so it never sees half a
refresh: a final without the one that ended earlier, or new cards beside old health. A card drawn from
one view is booked only if the latest still holds it as drawn; one that changed or left in the meantime
books nothing, and its newer version, if any, counts from its own first frame.

A card on air:
- comes down at once if it leaves the queue (its game moved on: factual invalidation), if it expires,
  or if its source goes offline. A live card its own final replaced is the game's last delay-safe frame:
  it keeps the board through its minimum, then hands off, to its final if that may air now;
- yields, once it has had its minimum, to a card with a stronger claim: a takeover over a reminder or
  rotation, a reminder over rotation;
- yields, once it has had its turn (its *slot*), to any other card the queue offers, or to nothing if
  the queue no longer offers it (a reminder whose beat has passed). A takeover or a reminder leaves at
  its turn even with nothing else waiting, and the board then shows its explicit frame;
- otherwise stays. A rotation card with nothing else waiting isn't taken down to show nothing.

A card's slot is the board's rotation slot, or a takeover's own `takeover_for`, held within the card's
minimum and maximum display. Takeovers back to back are bounded: once they have run the policy's cap,
the takeover up finishes its turn, and a base card (neither a takeover nor a reminder) is owed a full
turn of its own before the next one may start. A due reminder can't take that turn or cut it short;
only the card leaving, expiring or the source going offline can, and then the turn is still owed. With
no base card to give it to, the board says so (`BoardNote.NO_BASE_CARD`) and airs what it has.

A card the display can't draw comes down unseen (or at once, if it was already up) and is set aside
until it changes or a minute passes, so one bad payload never holds the board. A display that fails
outright spends nothing: the card stays cued, and its dwell starts at the first frame that lands.

**The first frame's landing** (`committed`) is when the display confirmed it, which for a display across a link
can be well after it was sent. The cue is judged again at that instant: a card whose first frame landed after its
latest start (the last moment it could start and still have its minimum before it expires), or, under policy 2,
after the grant it was cued under stopped holding (a guest whose host's lease no longer fits it), is an
*accidental display*: its pixels were up, but it's booked as nothing, comes down at once (`LANDED_LATE`,
`GRANT_LAPSED`), and the board chooses again. A display that never confirms what it shows (an older receiver,
credited only on its own say-so) can't give policy 2 its confirmed airtime: the board says so once
(`BoardNote.UNCONFIRMED_DISPLAY`), a policy-2 cue it put up is void (`UNCONFIRMED`), and from then on the board
rotates by policy 1's bands, booking in the queue on the display's unconfirmed word, as policy 1 does on such a
display; the director is told nothing, so it pays no debt, starts no cooldown and claims no first start.

When no card should be up, the board says so with an explicit frame: *idle* (nothing to show), *held*
(games are on, but nothing has cleared the TV delay yet) or *offline* (the source has gone quiet past
its limit or never answered, or a game's feed has stopped answering and its card has come down). A card
whose data is past its age airs marked *stale*.

A card's availability and expiry are UTC instants, which the queue pins to the monotonic clock when it takes
the card in (`CardWindow`). The board judges them on those pins, and runs dwell and slots on the monotonic
clock too, so a step of the wall clock moves none of them.

The session runs on the frame thread, but for `publish`, which the decision thread calls once a refresh is
done; the queue is thread-safe, and the input it publishes is replaced whole.

**Which policy decides.** Without settings the board selects as above (`omni-board-policy/1`). Given the user's
`PolicySettings`, a muted game's cards never air, and the turn is the settings' slot. Under `omni-board-policy/2`
the policy director (`omni.tower.director`) decides from the published `PolicyView` instead of the queue's
bands: base turns by debt, promotions through the broker, a favorite's break lent to guests, ordinary cards by
the ladder. The card contract above stays the board's: dwell from the commit, a minimum never cut by ordinary
priority, a card that leaves or expires coming down, a live card handing off to its final after its minimum, a
card that can't draw set aside, the explicit frames and stale dimming. What the policy decided is read with
`decisions`, for the trace.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any, Union

from omni.cards.base import ScoreboardCard
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import PanelProfile, StrEnumMixin
from omni.core.ids import SourceRef
from omni.domain.contest import Contest, TeamGame
from omni.policy.view import EMPTY_POLICY_VIEW, PolicyView
from omni.preferences.favorites import Favorites
from omni.queue.scheduler import CardView, InterleavedCardQueue, Pick, SelectionReason
from omni.tower.lane import ChangeKind, EndReason, LaneChange, earliest
from omni.tower.segment import Dwell, Provenance, Segment, SegmentKey, Surface

if TYPE_CHECKING:  # the policy's settings import the app, whose loop imports the board
    from omni.policy.settings import Mutes, PolicySettings
    from omni.tower.decisions import PolicyDecision
    from omni.tower.director import Air, DirectorPolicy, PolicyDirector

__all__ = [
    "BoardCue",
    "BoardFrame",
    "BoardHealth",
    "BoardInput",
    "BoardNote",
    "BoardPayload",
    "BoardPolicy",
    "BoardSession",
    "FrameKind",
    "SourceHealth",
    "UNDRAWN_RETRY",
    "muted",
]

_BOARD_SOURCE = SourceRef("board")
_NO_DWELL = Dwell(minimum=timedelta(0), maximum=timedelta(0))
UNDRAWN_RETRY = timedelta(minutes=1)  # how long a card the display couldn't draw is set aside, unless it changes


class FrameKind(StrEnumMixin, str, Enum):
    """An explicit frame, for when no card should be on the board."""

    IDLE = "idle"  # nothing to show
    HELD = "held"  # games are on, but nothing has cleared the TV delay yet
    OFFLINE = "offline"  # the source, or a game's feed, has gone quiet past its limit


@dataclass(frozen=True, slots=True)
class BoardFrame:
    """An explicit frame and why it's up.

    A held frame also says how the wait is going: from when the board began waiting until the first held
    card is due to clear the delay, if that's known.
    """

    kind: FrameKind
    reason: str
    started: datetime | None = None  # a held frame: when the board began waiting
    until: datetime | None = None  # a held frame: when the first held card is due to clear the delay

    def progress(self, now: datetime) -> float | None:
        """How far through its wait a held frame is at ``now``, from 0 to 1; None when that isn't known."""
        if self.started is None or self.until is None:
            return None
        if self.until <= self.started:
            return 1.0
        return min(max((now - self.started) / (self.until - self.started), 0.0), 1.0)


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
    held_until: datetime | None = None  # when the first of those games' cards is due to clear the delay
    unfed: int = 0  # games with nothing on the board because their feed has stopped answering


@dataclass(frozen=True, slots=True)
class BoardInput:
    """One refresh's whole word to the board, published at once: the cards it may air, as a numbered view,
    and how far their source can be trusted."""

    cards: CardView
    health: BoardHealth
    policy: PolicyView = EMPTY_POLICY_VIEW  # what the policy reads about each game, from the same refresh


class BoardNote(StrEnumMixin, str, Enum):
    """Something the board decided that isn't a card or a frame going up or down, for the trace."""

    NO_BASE_CARD = "no base card for the recovery turn"  # takeovers ran their cap, but only transients can air
    UNCONFIRMED_DISPLAY = (  # its frames are credited on its say-so: policy 2 can count none of it
        "the display doesn't confirm what it shows: policy 2 counts none of it, and the board rotates by policy 1"
    )


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
    expires: MonotonicTime | None = None  # a card: its expiry, as the queue pinned it; None if it never expires
    air: Air | None = None  # a card the policy director put up
    latest: MonotonicTime | None = None  # a card: the last moment its first frame may land; None if it never expires


class BoardSession:
    """The board surface's schedule: what's on air, why, and when it goes."""

    def __init__(
        self,
        queue: InterleavedCardQueue,
        profile: PanelProfile,
        *,
        policy: BoardPolicy = BoardPolicy(),
        settings: PolicySettings | None = None,
        favorites: Favorites = Favorites(),
        rules: DirectorPolicy | None = None,
    ):
        self._queue = queue
        self._profile = profile
        self._settings = settings
        self._director: PolicyDirector | None = None
        if settings is not None:
            from omni.tower.director import DirectorPolicy, PolicyDirector  # deferred: the settings import the app

            policy = replace(policy, slot=settings.slot)
            if PolicyDirector.decides(settings):
                rules = rules if rules is not None else DirectorPolicy()
                self._director = PolicyDirector(profile, settings, favorites=favorites, aired=queue.aired, policy=rules)
        self._policy = policy
        self._input = BoardInput(queue.published, BoardHealth())
        self._on_air: _OnAir | None = None
        self._occurrences = 0
        self._takeover_run = timedelta(0)  # takeover airtime since the last rotation card or frame went up
        self._handoff: str | None = None  # the card that takes the place of one just handed off: it may go first
        self._changes: list[LaneChange[BoardPayload]] = []
        self._notes: list[BoardNote] = []
        self._undrawn: dict[str, tuple[ScoreboardCard[Any], MonotonicTime]] = {}  # set aside: the card, and until
        self._unconfirmed = False  # the display has been credited on its own say-so: policy 2 stands aside

    @property
    def health(self) -> BoardHealth:
        """The source's health, as last published."""
        return self._input.health

    def publish(self, health: BoardHealth, policy: PolicyView = EMPTY_POLICY_VIEW) -> None:
        """A refresh is done: the queue's staged cards, the source's ``health`` and what the ``policy`` reads
        about each game become the board's input together, as one numbered view."""
        self._input = BoardInput(self._queue.publish(), health, policy)

    def cue(self, at: ClockReading) -> BoardCue:
        """What should be on the board at ``at``, once the rules have run: the card up, the next card, or
        an explicit frame."""
        board = self._input  # one input for the whole decision, whatever a refresh publishes meanwhile
        if self._director is not None and not self._unconfirmed:
            return self._cue_policy(self._director, board, at)
        health = board.health
        on = self._on_air
        if on is not None and on.pick is not None:
            self._review(on, board, at)
        on = self._on_air
        if on is None or on.pick is None:
            pick = None
            if health.source is not SourceHealth.OFFLINE:
                pick = self._choose(board.cards, at)
            if pick is not None:
                self._air_card(pick, board.cards, at)
            else:
                self._air_frame(self._explicit(health, at), at)
        return self._cue(health)

    def decisions(self) -> list[PolicyDecision]:
        """What the policy decided since the last ask, oldest first, for the trace; nothing under policy v1."""
        return self._director.decisions() if self._director is not None else []

    def committed(self, cue: BoardCue, at: ClockReading, *, confirmed: bool = True) -> bool:
        """The display showed ``cue`` at ``at``: by its own word, or, not ``confirmed``, credited on its say-so. A
        card's first commit starts its dwell and books it, if the latest published view still holds the card as
        drawn and it landed within its latest start and its grant; otherwise it counts for nothing (a landing too
        late is an accidental display), and the next cue takes up the change. Returns whether this commit put
        ``cue`` up."""
        on = self._on_air
        if on is None or cue.occurrence != on.occurrence or on.since is not None:
            return False  # no longer on air, or its dwell already runs
        if not confirmed:
            if not self._unconfirmed:
                self._notes.append(BoardNote.UNCONFIRMED_DISPLAY)
            self._unconfirmed = True
            if on.air is not None:  # the director's cue: it counts for nothing, and policy 1 rotates from here
                self._void(on, on.air, EndReason.UNCONFIRMED, at, aired=None)
                return False
        accident = self._accident(on, at)
        if accident is not None:
            self._void(on, on.air, accident, at, aired=timedelta(0))
            return False
        if on.pick is None:
            self._takeover_run = timedelta(0)  # a quiet board: takeovers before it weren't back to back
        elif on.air is not None:
            if not self._started(on.air, at):
                return False
        else:
            drawn = cue.payload
            assert not isinstance(drawn, BoardFrame)
            if not self._queue.book(Pick(drawn, on.pick.reason), at):
                return False  # it changed or left while it was drawn
        on.since = at.monotonic
        self._changes.append(LaneChange(ChangeKind.UP, on.segment, at))
        return True

    def _accident(self, on: _OnAir, at: ClockReading) -> EndReason | None:
        """Why a card's first frame, landing at ``at``, is an accidental display: too late to start, or its grant
        lapsed before it landed; None when it counts."""
        if on.pick is None:
            return None
        if on.latest is not None and at.monotonic > on.latest:
            return EndReason.LANDED_LATE
        if on.air is not None:
            assert self._director is not None
            if self._director.held(on.air, at) is not None:
                return EndReason.GRANT_LAPSED
        return None

    def _void(
        self, on: _OnAir, air: Air | None, reason: EndReason, at: ClockReading, *, aired: timedelta | None
    ) -> None:
        """A cue that landed but counts for nothing comes down at once, its turn unstarted, so nothing is credited.
        ``aired`` says whether its pixels were up (an accidental display) or not known to be."""
        if air is not None:
            assert self._director is not None
            self._director.down(air, at)
        kind = ChangeKind.DOWN if aired is not None else ChangeKind.DROPPED
        self._changes.append(LaneChange(kind, on.segment, at, reason=reason, aired=aired))
        self._on_air = None

    def failed(self, cue: BoardCue, at: ClockReading) -> None:
        """The display couldn't draw ``cue``'s card at ``at``: take it down, and set it aside until it changes
        or `UNDRAWN_RETRY` passes. A frame that fails is simply tried again."""
        on = self._on_air
        if on is None or cue.occurrence != on.occurrence or on.pick is None:
            return
        card = on.pick.card
        self._undrawn[card.dedupe_key.raw] = (card, at.monotonic + UNDRAWN_RETRY)
        self._queue.undrawn(card)
        self._down(EndReason.UNDRAWN, at)

    def next_deadline(self, at: ClockReading) -> MonotonicTime | None:
        """When the board will next change by itself, as of ``at``.

        None means only a change in the queue, the health or a commit can change it.
        """
        on = self._on_air
        if on is None:
            return at.monotonic  # nothing has been cued yet
        view = self._input.cards
        director = self._director.next_deadline(at) if self._director is not None else None
        upcoming = earliest(
            (self._queue.next_deadline(at, self._profile, view=view), director, *self._set_aside(view, at).values())
        )
        if on.pick is None or on.since is None:
            return upcoming  # a frame stays until a card can replace it; a card's dwell waits on its commit
        if on.air is not None:
            assert self._director is not None
            return earliest((upcoming, self._director.deadline(on.air, at), _ahead(on.expires, at)))
        deadlines = [upcoming, on.since + on.segment.dwell.minimum, on.since + on.slot, on.expires]
        return earliest(deadline for deadline in deadlines if deadline is None or deadline > at.monotonic)

    def changes(self) -> list[LaneChange[BoardPayload]]:
        """What happened since the last ask, oldest first."""
        changes, self._changes = self._changes, []
        return changes

    def notes(self) -> list[BoardNote]:
        """What the board noted since the last ask, oldest first."""
        notes, self._notes = self._notes, []
        return notes

    def _review(self, on: _OnAir, board: BoardInput, at: ClockReading) -> None:
        """Take the card on air down if its time is up, or keep it with its newest version."""
        assert on.pick is not None
        if board.health.source is SourceHealth.OFFLINE:
            self._down(EndReason.CLEARED, at)
            return
        if on.expires is not None and at.monotonic >= on.expires:
            self._down(EndReason.EXPIRED, at)  # whether or not a refresh has pruned it yet
            return
        key = on.segment.key.subject
        latest = board.cards.card(key)
        if latest is None:
            self._leave(on, board.cards.successor(key), at)
            return
        if latest is not on.segment.frames[0]:
            on.segment = _card_segment(latest, revision=on.segment.revision + 1)  # same dwell: a cosmetic edit
            on.pick = Pick(latest, on.pick.reason)
            window = board.cards.windows[key]
            on.expires, on.latest = window.expires, window.latest_start
        if on.since is None:
            return  # not drawn yet: no time has run
        up = at.monotonic.since(on.since)
        if up < on.segment.dwell.minimum:
            return  # ordinary priority never cuts a visible minimum
        best = self._peek(board.cards, at, recovery=self._owes_recovery(at))
        offered = best is not None and best.card.dedupe_key.raw == key
        if best is not None and not offered and best.reason.rank > on.pick.reason.rank:
            self._down(EndReason.NEXT_WAITING, at)  # a stronger claim, once this had its minimum
        elif up >= on.slot and (on.pick.reason is not SelectionReason.ROTATION or not offered):
            # Its turn is over, and something else is waiting, or the queue no longer offers it, or it's a
            # takeover or a reminder, which leaves at its turn even with nothing else waiting.
            self._down(EndReason.RAN_ITS_TIME, at)

    def _leave(self, on: _OnAir, successor: ScoreboardCard[Any] | None, at: ClockReading) -> None:
        """The card on air has left the queue. Replaced by its own final, it's the game's last delay-safe frame:
        it keeps the board through its minimum, then hands off, its final first if that may air. Otherwise
        it's no longer current, and comes down at once."""
        if successor is not None and on.since is not None and at.monotonic.since(on.since) < on.segment.dwell.minimum:
            return
        self._handoff = successor.dedupe_key.raw if successor is not None else None
        self._down(EndReason.INVALIDATED, at)

    def _choose(self, view: CardView, at: ClockReading) -> Pick | None:
        """The card to put up next, with nothing on air: a card handed off to first, if it may air."""
        owed = self._owes_recovery(at)
        pick = self._peek(view, at, recovery=owed, prefer=self._handoff)
        self._handoff = None
        if owed and pick is not None and pick.reason is not SelectionReason.ROTATION:
            self._notes.append(BoardNote.NO_BASE_CARD)  # a base card is owed its turn, but none can air
        return pick

    def _peek(self, view: CardView, at: ClockReading, *, recovery: bool, prefer: str | None = None) -> Pick | None:
        skip = frozenset(self._set_aside(view, at)) | self._muted(view)
        return self._queue.peek(at, self._profile, view=view, recovery=recovery, prefer=prefer, skip=skip)

    def _muted(self, view: CardView) -> frozenset[str]:
        """The cards of games the settings mute, which never air."""
        settings = self._settings
        if settings is None or not settings.mutes:
            return frozenset()
        return frozenset(key for key, card in view.cards.items() if muted(settings.mutes, card.contest))

    def _set_aside(self, view: CardView, at: ClockReading) -> dict[str, MonotonicTime]:
        """The cards set aside after failing to draw, with when each may be tried again; forgets the rest."""
        for key, (card, until) in list(self._undrawn.items()):
            if view.card(key) is not card or at.monotonic >= until:
                del self._undrawn[key]  # it changed, left the queue, or has waited long enough: try it again
        return {key: until for key, (_, until) in self._undrawn.items()}

    def _owes_recovery(self, at: ClockReading) -> bool:
        """Whether a base card is owed a full turn: the takeover airtime back to back, the one up included, has
        reached the cap, and no rotation card has had its full turn since."""
        run = self._takeover_run
        on = self._on_air
        if on is not None and on.pick is not None and on.since is not None:
            up = at.monotonic.since(on.since)
            if on.pick.reason is SelectionReason.TAKEOVER:
                run += up
            elif on.pick.reason is SelectionReason.ROTATION and up >= on.slot:
                run = timedelta(0)
        return run >= self._policy.takeover_cap

    def _air_card(self, pick: Pick, view: CardView, at: ClockReading) -> None:
        if self._on_air is not None:
            self._down(EndReason.NEXT_WAITING, at)  # a frame gives way to a card at once
        segment = _card_segment(pick.card, revision=0)
        dwell = segment.dwell
        turn = pick.card.attention.takeover_for.as_timedelta() if pick.reason is SelectionReason.TAKEOVER else None
        slot = min(max(turn if turn is not None else self._policy.slot, dwell.minimum), dwell.maximum)
        self._occurrences += 1
        window = view.windows[pick.card.dedupe_key.raw]
        self._on_air = _OnAir(
            segment=segment,
            occurrence=self._occurrences,
            pick=pick,
            slot=slot,
            expires=window.expires,
            latest=window.latest_start,
        )

    def _waiting_since(self, at: ClockReading) -> datetime:
        """When the held frame up began waiting; now, if none is up."""
        on = self._on_air
        if on is not None and on.pick is None:
            frame = on.segment.frames[0]
            assert isinstance(frame, BoardFrame)
            if frame.kind is FrameKind.HELD and frame.started is not None:
                return frame.started
        return at.utc

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
        if on.air is not None:
            assert self._director is not None
            self._director.down(on.air, at)
        elif aired is not None and on.pick is not None:
            if on.pick.reason is SelectionReason.TAKEOVER:
                self._takeover_run += aired
            elif on.pick.reason is SelectionReason.ROTATION and aired >= on.slot:
                self._takeover_run = timedelta(0)  # the board's own turn has run in full: takeovers may start again
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

    def _explicit(self, health: BoardHealth, at: ClockReading) -> BoardFrame:
        """The explicit frame for when no card should be up: offline, a game with no feed, games held in the TV
        delay, or idle."""
        if health.source is SourceHealth.OFFLINE:
            return BoardFrame(FrameKind.OFFLINE, health.reason)
        if health.unfed:
            return BoardFrame(FrameKind.OFFLINE, f"no feed for {_games(health.unfed)}")
        if health.held:
            return BoardFrame(
                FrameKind.HELD, f"{_games(health.held)} in the TV delay", self._waiting_since(at), health.held_until
            )
        return BoardFrame(FrameKind.IDLE, "nothing eligible")

    # --- policy v2: the director decides, the board keeps the card contract ----------------------------------

    def _cue_policy(self, director: PolicyDirector, board: BoardInput, at: ClockReading) -> BoardCue:
        health = board.health
        offline = health.source is SourceHealth.OFFLINE
        director.refresh(board.cards, board.policy, offline=offline, at=at)
        on = self._on_air
        if on is not None and on.air is not None:
            self._review_policy(director, on, on.air, board, at)
        on = self._on_air
        if on is None or on.pick is None:
            air = None
            if not offline:
                air = director.choose(board.cards, at, skip=frozenset(self._set_aside(board.cards, at)))
            if air is not None:
                self._air_policy(air, board.cards, at)
            else:
                self._air_frame(self._explicit(health, at), at)
        return self._cue(health)

    def _review_policy(
        self, director: PolicyDirector, on: _OnAir, air: Air, board: BoardInput, at: ClockReading
    ) -> None:
        """Take the card on air down if its time is up or the policy says it gives way; else keep it, drawn as the
        policy says, in its newest version."""
        if board.health.source is SourceHealth.OFFLINE:
            self._down(EndReason.CLEARED, at)
            return
        if on.expires is not None and at.monotonic >= on.expires:
            self._down(EndReason.EXPIRED, at)
            return
        key = on.segment.key.subject
        latest = board.cards.card(key)
        if latest is None:
            successor = board.cards.successor(key)
            if successor is not None and on.since is not None and at.monotonic < on.since + on.segment.dwell.minimum:
                return  # the game's last delay-safe frame keeps its minimum
            if successor is not None:
                director.handoff(air, successor, board.cards, at, skip=frozenset(self._set_aside(board.cards, at)))
            self._down(EndReason.INVALIDATED, at)
            return
        if latest is not air.card:
            director.revised(air, latest, at)
            on.pick = Pick(latest, air.reason)
            window = board.cards.windows[key]
            on.expires, on.latest = window.expires, window.latest_start
        if on.since is not None:
            reason = director.review(air, board.cards, at, skip=frozenset(self._set_aside(board.cards, at)))
            if reason is not None:
                self._down(reason, at)
                return
        if on.segment.frames[0] is not air.shown:
            on.segment = _card_segment(air.shown, revision=on.segment.revision + 1)  # same dwell: drawn anew

    def _air_policy(self, air: Air, view: CardView, at: ClockReading) -> None:
        if self._on_air is not None:
            self._down(EndReason.NEXT_WAITING, at)  # a frame gives way to a card at once
        self._occurrences += 1
        window = view.windows[air.card.dedupe_key.raw]
        self._on_air = _OnAir(
            segment=_card_segment(air.shown, revision=0),
            occurrence=self._occurrences,
            pick=Pick(air.card, air.reason),
            slot=air.turn.length,
            expires=window.expires,
            air=air,
            latest=window.latest_start,
        )

    def _started(self, air: Air, at: ClockReading) -> bool:
        """A director's card drew its first frame: it counts if the latest view still holds it as drawn and the
        policy confirms its start; a promotion the broker rejects comes down unseen, having spent nothing."""
        director = self._director
        assert director is not None
        board = self._input
        director.refresh(board.cards, board.policy, offline=board.health.source is SourceHealth.OFFLINE, at=at)
        if self._queue.published.card(air.card.dedupe_key.raw) is not air.card:
            return False  # it changed or left while it was drawn: the next cue takes up the change
        if not director.started(air, at):
            on = self._on_air
            assert on is not None
            self._changes.append(LaneChange(ChangeKind.DROPPED, on.segment, at, reason=EndReason.INVALIDATED))
            self._on_air = None
            return False
        self._queue.book(Pick(air.card, air.reason), at)
        return True


def muted(mutes: Mutes, contest: Contest) -> bool:
    """Whether the user's ``mutes`` silence ``contest``: its league, or either team of a team game."""
    if isinstance(contest, TeamGame):
        return mutes.mutes(contest)
    return contest.league in mutes.leagues


def _games(count: int) -> str:
    return "a game" if count == 1 else f"{count} games"


def _ahead(instant: MonotonicTime | None, at: ClockReading) -> MonotonicTime | None:
    return instant if instant is not None and instant > at.monotonic else None


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
