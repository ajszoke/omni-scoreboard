"""The display end's protocol: what a frame receiver reads and says, and when it blanks the panel on its own.

The board's panel is driven by a small receiver (`tools/board_frame_sink.py`) that reads frames from the app on its
stdin and answers on its stdout. This module is that conversation, kept apart from the hardware so it can be
tested: the wire, the replies, and the receiver's state (`Receiver`). The tool loads this file by its path, under
the board's system Python (3.9, the one with the LED bindings), not as part of the ``omni`` package: so it uses the
standard library only, and Python 3.9's language. That's why its dataclasses aren't slotted.

**The wire, app to receiver.** Each message starts with a 4-byte magic; numbers are little-endian:
- ``OMN3`` + session (32-bit) + frame id (32-bit) + width, height (16-bit) + the pixels, row by row, three bytes
  each: a frame for the app's output *session*;
- ``OMNH`` + session (32-bit): a heartbeat. The app sends one at least every `HEARTBEAT_EVERY` while it runs, even
  while its frame is unchanged;
- ``OMN2`` + frame id + width, height + pixels, and ``OMNF`` + width, height + pixels: an older app's frames, which
  carry no session (``OMNF``, no id either).

**The replies,** one a line (`Reply`):
- ``OMN2 READY`` then ``OMN3 READY <boot>`` once the receiver is up. ``<boot>`` is a fresh id for each start of the
  receiver, so the app can tell a restarted receiver from the one it had. An older app reads only the first.
- ``SHOWN <session> <id>`` once an ``OMN3`` frame is on the panel; ``ACK <id>`` for an ``OMN2`` frame.
- ``ALIVE <session>`` for each heartbeat of the newest session.
- ``BLANK <session>`` when the receiver blanked the panel itself (its watchdog).

**The receiver** keeps the newest session it has heard. A frame or a heartbeat from an older session is ignored,
since an app that has moved on to a new session no longer answers for it; a newer one takes over. Frames wait in a
single slot: one that arrives while another waits replaces it, so the panel only ever catches up to the newest
frame, and a frame replaced before it was shown is never acknowledged. Once it has heard a session (an ``OMN3``
frame or a heartbeat), it blanks the panel `WATCHDOG` after the last frame or heartbeat of the newest session, even
if the frame on the panel hasn't changed, and says so: an app that has stopped running stops sending heartbeats,
and its last frame must not stay up as if it were current. An older app's frames never arm the watchdog, since
that app sends nothing while its frame is unchanged.
"""

from __future__ import annotations

import enum
import struct
from dataclasses import dataclass
from typing import List, Optional, Protocol, Union

__all__ = [
    "FRAME_MAGIC",
    "FRAME_MAGIC_V3",
    "HEARTBEAT_EVERY",
    "HEARTBEAT_MAGIC",
    "LEGACY_FRAME_MAGIC",
    "READY_LINE",
    "WATCHDOG",
    "Frame",
    "Heartbeat",
    "OutOfSync",
    "Receiver",
    "Reply",
    "ReplyKind",
    "encode_heartbeat",
    "encode_session_frame",
    "parse_reply",
    "read_message",
]

FRAME_MAGIC_V3 = b"OMN3"  # then the session, the id, the width and the height: "<IIHH"
HEARTBEAT_MAGIC = b"OMNH"  # then the session: "<I"
FRAME_MAGIC = b"OMN2"  # then the id, the width and the height: "<IHH"
LEGACY_FRAME_MAGIC = b"OMNF"  # then the width and the height: "<HH"
READY_LINE = b"OMN2 READY\n"
HEARTBEAT_EVERY = 2.0  # seconds: the app's longest silence while it runs
WATCHDOG = 8.0  # seconds without a frame or a heartbeat of the newest session before the receiver blanks the panel


@dataclass(frozen=True)
class Frame:
    """One frame read from the app: its pixels, and, as its wire says, its id and its session."""

    width: int
    height: int
    pixels: bytes
    id: Optional[int] = None  # None for an ``OMNF`` frame
    session: Optional[int] = None  # None for an ``OMN2`` or ``OMNF`` frame


@dataclass(frozen=True)
class Heartbeat:
    """The app is running, in this session."""

    session: int


Message = Union[Frame, Heartbeat]


def encode_session_frame(session: int, frame_id: int, width: int, height: int, pixels: bytes) -> bytes:
    """An ``OMN3`` frame on the wire."""
    return FRAME_MAGIC_V3 + struct.pack("<IIHH", session, frame_id, width, height) + pixels


def encode_heartbeat(session: int) -> bytes:
    """A heartbeat on the wire."""
    return HEARTBEAT_MAGIC + struct.pack("<I", session)


class OutOfSync(ValueError):
    """The stream doesn't start a message where one should start: the receiver can't find its place again."""


class ByteStream(Protocol):
    def read(self, size: int) -> bytes: ...


def _read_exact(stream: ByteStream, size: int) -> Optional[bytes]:
    """Exactly ``size`` bytes, or None if the stream ends first."""
    chunks = []
    left = size
    while left:
        chunk = stream.read(left)
        if not chunk:
            return None
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def read_message(stream: ByteStream) -> Optional[Message]:
    """The next message on the stream; None once it ends, mid-message or not. Raises `OutOfSync` on a magic it
    doesn't know."""
    magic = _read_exact(stream, 4)
    if magic is None:
        return None
    if magic == HEARTBEAT_MAGIC:
        rest = _read_exact(stream, 4)
        return Heartbeat(struct.unpack("<I", rest)[0]) if rest is not None else None
    session: Optional[int] = None
    frame_id: Optional[int] = None
    if magic == FRAME_MAGIC_V3:
        header = _read_exact(stream, 12)
        if header is None:
            return None
        session, frame_id, width, height = struct.unpack("<IIHH", header)
    elif magic == FRAME_MAGIC:
        header = _read_exact(stream, 8)
        if header is None:
            return None
        frame_id, width, height = struct.unpack("<IHH", header)
    elif magic == LEGACY_FRAME_MAGIC:
        header = _read_exact(stream, 4)
        if header is None:
            return None
        width, height = struct.unpack("<HH", header)
    else:
        raise OutOfSync(f"no message starts {magic!r}")
    pixels = _read_exact(stream, width * height * 3)
    if pixels is None:
        return None
    return Frame(width, height, pixels, frame_id, session)


class ReplyKind(str, enum.Enum):
    """What a receiver's line says."""

    READY_V2 = "OMN2 READY"  # it reads ``OMN2`` frames and acknowledges them
    READY = "OMN3 READY"  # it reads sessions and heartbeats too: with its boot id
    SHOWN = "SHOWN"  # an ``OMN3`` frame is on the panel: its session and id
    ACK = "ACK"  # an ``OMN2`` frame is on the panel: its id
    ALIVE = "ALIVE"  # a heartbeat of this session was heard
    BLANK = "BLANK"  # the receiver blanked the panel itself, in this session


@dataclass(frozen=True)
class Reply:
    """One line from the receiver."""

    kind: ReplyKind
    session: Optional[int] = None
    frame: Optional[int] = None
    boot: Optional[str] = None

    def line(self) -> bytes:
        """The reply on the wire."""
        words = [self.kind.value]
        words += [str(value) for value in (self.boot, self.session, self.frame) if value is not None]
        return (" ".join(words) + "\n").encode("ascii")


def parse_reply(line: bytes) -> Optional[Reply]:
    """A receiver's line as a reply; None for anything else it prints (a library's chatter, a malformed line)."""
    words = line.decode("ascii", "replace").split()
    numbers = [int(word) for word in words[1:] if word.isdigit()]
    if len(numbers) != len(words) - 1:
        numbers = []
    if words == ReplyKind.READY_V2.value.split():
        return Reply(ReplyKind.READY_V2)
    if len(words) == 3 and " ".join(words[:2]) == ReplyKind.READY.value:
        return Reply(ReplyKind.READY, boot=words[2])
    if len(words) == 3 and words[0] == ReplyKind.SHOWN.value and len(numbers) == 2:
        return Reply(ReplyKind.SHOWN, session=numbers[0], frame=numbers[1])
    if len(words) == 2 and words[0] == ReplyKind.ACK.value and numbers:
        return Reply(ReplyKind.ACK, frame=numbers[0])
    if len(words) == 2 and words[0] in (ReplyKind.ALIVE.value, ReplyKind.BLANK.value) and numbers:
        return Reply(ReplyKind(words[0]), session=numbers[0])
    return None


class Receiver:
    """One run of a receiver: the newest session it has heard, the frame waiting for the panel, what the panel shows,
    and its watchdog. Times are seconds on the receiver's own monotonic clock.

    The caller reads messages and hands each to `heard`, takes the frame waiting (`take`) to put on the panel and
    says when it's there (`swapped`), and asks `expired` now and then (at `deadline` at the latest) whether the
    watchdog has blanked the panel. Each returns the replies to write.
    """

    def __init__(self, boot: str, *, watchdog: float = WATCHDOG) -> None:
        self._boot = boot
        self._watchdog = watchdog
        self._session: Optional[int] = None  # the newest session heard; None until one is
        self._waiting: Optional[Frame] = None  # the frame to put on the panel next: at most one
        self._lit = False  # the panel shows a frame
        self._heard: Optional[float] = None  # when the newest session's last frame or heartbeat came

    def hello(self) -> List[Reply]:
        """What it says once it's up: ready, to an older app and to a new one."""
        return [Reply(ReplyKind.READY_V2), Reply(ReplyKind.READY, boot=self._boot)]

    def heard(self, message: Message, now: float) -> List[Reply]:
        """A message from the app, at ``now``: a frame waits for the panel (replacing one that was), a heartbeat is
        answered. Either from a session older than the newest heard is ignored."""
        session = message.session
        if session is not None:
            if self._session is not None and session < self._session:
                return []  # the app has moved on from that session
            if self._session is None or session > self._session:
                self._session = session
                if self._waiting is not None and self._waiting.session != session:
                    self._waiting = None  # an older session's frame never goes up
            self._heard = now
        if isinstance(message, Heartbeat):
            return [Reply(ReplyKind.ALIVE, session=message.session)]
        self._waiting = message
        return []

    @property
    def waiting(self) -> bool:
        """Whether a frame is waiting for the panel."""
        return self._waiting is not None

    def take(self) -> Optional[Frame]:
        """The frame to put on the panel now, if one is waiting."""
        frame, self._waiting = self._waiting, None
        return frame

    def swapped(self, frame: Frame) -> List[Reply]:
        """``frame`` is on the panel: acknowledge it, as its wire asks."""
        self._lit = True
        if frame.id is None:
            return []
        if frame.session is None:
            return [Reply(ReplyKind.ACK, frame=frame.id)]
        return [Reply(ReplyKind.SHOWN, session=frame.session, frame=frame.id)]

    def deadline(self) -> Optional[float]:
        """When the watchdog blanks the panel, if nothing more comes; None while it isn't armed or nothing is lit."""
        if self._heard is None or not self._lit:
            return None
        return self._heard + self._watchdog

    def expired(self, now: float) -> List[Reply]:
        """Whether the watchdog blanks the panel at ``now``: then the frame waiting goes too, and it says so. The
        caller blanks the panel when this says ``BLANK``."""
        deadline = self.deadline()
        if deadline is None or now < deadline:
            return []
        self._lit, self._waiting = False, None
        return [Reply(ReplyKind.BLANK, session=self._session)]
