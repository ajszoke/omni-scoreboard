"""Mine the NFL runner's log into our own telemetry, a digest per night, keeping none of its words.

``python -m omni.app.football`` prints its log to stdout, and a night's log can be kept as one file across several
nights and restarts. Its lines are our own format, but many carry NFLMeta's words: a strip entry's description, a
score line, a note's detail. `mine_runner_log` reads the lines by their shape alone, counts and times what they
say, and writes one digest per night (`build/telemetry/runner-log/<date>.json`, schema ``omni-mined/1``) with a
short Markdown summary beside it. A line it can't place is counted, never kept.

**The formats.** The runner's lines changed between the nights it ran, and each version is read:
- **2026-11-15** (`omni/app/football.py` and `football_board.py` as committed in 176fd69): ``HH:MM:SS`` and two
  spaces before each board line; the display process's lines (``display: starting ...``, ``display: lost the
  display process; restarting shortly``) unstamped. The strip logs what arrives (``  > words``), what it puts up
  (``  = words``) and what it drops (``  - words (unseen: why)``), with feed notes (``  ! kind: detail``), feed
  errors (``feed: ...``), a line per changed answer (``PIT @ CIN 0-7 Q2 1:52 [in] | ...``), the halftime hold
  (``halftime: holding off the feed``, ``halftime hold over: polling again``) and the park after the final
  (``final: no more polling``). Nothing says whether a frame was shown.
- **2026-11-16** (the same files at bfd123a, with `football_feed.py` and `frame_stream.py`): every board line
  stamped, display lines included; a put-up entry is logged when the display says it showed it, marked
  ``(unconfirmed)`` when the display process doesn't acknowledge, and ``(again, on the new display)`` after a
  restart. New lines: ``  x words (cleared: why)``, ``  ~ words (blanked: ...)``, ``feed stale: ...`` and ``feed
  answering again``, ``paint failed``, ``strip: a caption failed to draw``, ``step failed`` and the display's
  start, write and acknowledgement failures. Frames sent and their acknowledgements aren't logged one by one, so
  only the strip's shown entries tell confirmed from unconfirmed.
- **2026-11-19** (the same files at d76a7eb, with `omni/app/quota.py`, `versions.py` and `run_log.py`): each line
  goes in full to the run's ``runner.log``, quoting the feed as before, while stdout gets our own telemetry, which
  names a strip entry by its label (``routine play #57``: `omni.events.football.EntryLabel`) and a note by its
  kind. New lines: the month's NFLMeta usage (``quota: NFLMeta in 2026-11: 2,875 of 5,000 requests, ...``,
  unstamped, just before the opening line and again at exit), where the full log is (``omni.app.football: the
  full log is <path>, deleted by <date>``, unstamped), what the run decides with (``versions: ...`` and ``config
  <digest>: ...``), a jump of the wall clock (``clock: the wall clock jumped ...``) and the wake after a sleep
  (``woke: ...``).

**Reading a line.** `read_line` places a line by its shape, and keeps beside what the digest counts the same line
in our own words (`RunnerLine.ours`): the shape's fixed words, our kinds, the clubs, the score, the period and the
clock. Each run of a provider's words becomes ``[detail]`` (or a placeholder like it), each of NFLMeta's ids is
left for whoever writes the line to number (`NflMetaId`), and a line of no known shape becomes ``[line not
kept]``. A line in our own words reads the same when read again, so `omni.telemetry.runner_log_redact` writes a
log of our own from it, and checks one.

**Runs and nights.** Each run starts with the runner's own line (``omni.app.football: PIT @ CIN (game 1),
quad_128x64, delay 0s``); the month's usage line just before it is that run's. Stamps carry no date: within a run, a
stamp more than 12 hours behind the one before it crossed midnight, and a smaller step back is a wall-clock jump. A
run starts a new night when its game differs from the run before, or its first stamp is earlier in the day than
where the run before ended; nights are dated one day apart from ``first_date`` (`read_nights`).

A strip entry's kind is read from its headline, the board's own words for it ("TIMEOUT CIN", "TV TIMEOUT", "XP
GOOD"): our `FootballEventKind`, the officials' timeout logged as "TV TIMEOUT" on both nights included. An entry
named by its label ("major score #60") is read by the kind it names.
"""

from __future__ import annotations

import dataclasses
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from omni.core.enum import PanelProfile, StrEnumMixin
from omni.domain.football import FootballInterval
from omni.events.football import FootballEventKind
from omni.providers.nfl_teams import canonical_abbreviation
from omni.providers.nflmeta import FeedNoteKind, NflPhase
from omni.telemetry.digest import (
    SCHEMA,
    SPREAD_MEASURES,
    DigestKind,
    Spread,
    allowed_values,
    figure,
    write_digest,
)
from omni.telemetry.nflmeta_mine import Endpoint

__all__ = [
    "DETAIL",
    "MEASURES",
    "NOT_KEPT",
    "FeedErrorKind",
    "IdKind",
    "LineKind",
    "LogFormat",
    "NflMetaId",
    "Night",
    "RunnerLine",
    "StripReason",
    "mine_log",
    "mine_runner_log",
    "read_line",
    "read_nights",
    "summary",
]

_DAY = 86_400
_MIDNIGHT = 12 * 3_600  # a stamp this far behind the one before it is the next day's
SILENCE = timedelta(minutes=5)  # no line for this long, while the game should be talking, is a silence

# The placeholders a line in our own words puts where the log had something that isn't ours to keep.
DETAIL = "[detail]"  # a provider's words, or a failure's message, which may quote them
NOT_KEPT = "[line not kept]"  # a line of no known shape
_TEAM = "[team]"  # a club that isn't an NFL club
_GAME = "[game]"  # a game named by no id we know the shape of
_NOTE = "[note]"  # a note of a kind we don't know
_PROFILE = "[profile]"  # a panel that isn't one of ours
_COMMAND = "[command]"  # the display's command: this machine's, not the run's
_PATH = "[path]"  # where the full log is on this machine


class LogFormat(StrEnumMixin, str, Enum):
    """Which version of the runner wrote a run's lines."""

    NIGHT_ONE = "nfl runner 2026-11-15"
    NIGHT_TWO = "nfl runner 2026-11-16"
    NIGHT_THREE = "nfl runner 2026-11-19"


class LineKind(StrEnumMixin, str, Enum):
    """What a log line says, by its shape."""

    START = "start"
    DISPLAY_START = "display start"
    DISPLAY_START_FAILED = "display start failed"
    DISPLAY_LOST = "display lost"
    DISPLAY_WRITE_FAILED = "display write failed"
    DISPLAY_UNCONFIRMED = "display unconfirmed"
    FEED_ERROR = "feed error"
    FEED_STALE = "feed stale"
    FEED_BACK = "feed back"
    NOTE = "note"
    HOLD_START = "hold start"
    HOLD_END = "hold end"
    PARKED = "parked"
    MARKET_ERROR = "market error"
    PAINT_FAILED = "paint failed"
    PAINT_RECOVERED = "paint recovered"
    CAPTION_FAILED = "caption failed"
    STEP_FAILED = "step failed"
    ARRIVED = "arrived"
    SHOWN = "shown"
    DROPPED = "dropped"
    CLEARED = "cleared"
    BLANKED = "blanked"
    ANSWER = "answer"
    QUOTA = "quota"  # the month's NFLMeta usage
    FULL_LOG = "full log"  # where the run's full log is, and when it's deleted
    VERSIONS = "versions"
    CONFIG = "config"
    CLOCK_JUMP = "clock jump"
    WOKE = "woke"
    UNPARSED = "unparsed"


class StripReason(StrEnumMixin, str, Enum):
    """Why the strip dropped or cleared an entry, as the runner worded it on any night."""

    BREAK_BEGAN = "the break began"
    DURING_BREAK = "during a break"
    RAN_ITS_TIME = "ran its time"
    NEXT_WAITING = "next waiting"
    NO_LONGER_CURRENT = "no longer current"
    CLOSING_OUT = "closing out"
    CLEARED = "cleared"
    TOO_LATE = "too late to start"
    EXPIRED = "expired"
    UNDRAWN = "failed to draw"
    MISSED_WINDOW = "missed its air window"
    SOURCE_STALE = "its source went stale"
    FIELD_MOVED_ON = "the field moved on without its play"
    MOVED_ON = "the field moved on"  # an entry dropped unseen for it, since 2026-11-19
    PAST_HARD_END = "past its hard end"
    ARRIVED_LATE = "arrived late"
    BACKLOG = "backlog"
    LANDED_LATE = "accidental: landed after its latest start"
    GRANT_LAPSED = "accidental: landed after its grant lapsed"
    UNCONFIRMED = "its display can't confirm it"
    INTERRUPTED = "interrupted"
    DROPPED = "dropped"
    OTHER = "other"

    @classmethod
    def _missing_(cls, value: object) -> StripReason:
        return cls.OTHER


class FeedErrorKind(StrEnumMixin, str, Enum):
    """What a ``feed:`` line's failure was."""

    FETCH_FAILED = "fetch failed"
    NOT_LISTED = "game not listed"
    UNREADABLE = "unreadable game"
    REFUSED = "refused by the quota"
    OTHER = "other"


class IdKind(StrEnumMixin, str, Enum):
    """Which of NFLMeta's ids a line names."""

    GAME = "game"
    PLAY = "play"  # a play-by-play row's number


@dataclass(frozen=True, slots=True)
class NflMetaId:
    """One of NFLMeta's ids as a line names it: its kind, and the id as written (or the ordinal a redacted log
    wrote in its place, ``G1`` or ``P001``)."""

    kind: IdKind
    raw: str


# Lines only the second night's runner and the ones after it wrote.
_NIGHT_TWO = frozenset(
    {
        LineKind.FEED_STALE,
        LineKind.FEED_BACK,
        LineKind.PAINT_FAILED,
        LineKind.PAINT_RECOVERED,
        LineKind.CAPTION_FAILED,
        LineKind.STEP_FAILED,
        LineKind.CLEARED,
        LineKind.BLANKED,
        LineKind.DISPLAY_UNCONFIRMED,
        LineKind.DISPLAY_START_FAILED,
        LineKind.DISPLAY_WRITE_FAILED,
    }
)
# Lines only the 2026-11-19 runner wrote.
_NIGHT_THREE = frozenset(
    {LineKind.QUOTA, LineKind.FULL_LOG, LineKind.VERSIONS, LineKind.CONFIG, LineKind.CLOCK_JUMP, LineKind.WOKE}
)
_STAMP = re.compile(r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})  (?P<body>.*)")
_CLUB = r"[A-Z]{2,3}|\[team\]"
_GAME_ID = r"\d+|G\d+"  # NFLMeta's id for a game, or the ordinal a redacted log names it by
_PLAY_ID = r"\d+|P\d+"  # and for a play-by-play row
_START = re.compile(
    rf"omni\.app\.football: (?P<away>{_CLUB}) @ (?P<home>{_CLUB}) \(game (?P<game>{_GAME_ID})\),"
    r" (?P<profile>\S+), delay (?P<delay>\d+)s"
)
_ANSWER = re.compile(
    rf"(?P<away>{_CLUB}) @ (?P<home>{_CLUB})(?:: (?P<early>pre|in|post)"
    r"| (?P<score>\d+-\d+) Q(?P<period>\d+) (?P<clock>\d+:\d{2}) \[(?P<phase>pre|in|post)\](?P<rest>.*))"
)
_STRIP = re.compile(r"  (?P<mark>[>=\-x~]) (?P<words>.*)")
_SUFFIX = re.compile(r"(?P<words>.*) \((?P<what>unseen|cleared|blanked): (?P<why>[^()]*)\)")
_HTTP = re.compile(r"HTTP Error (?P<status>\d{3})")
_KINDS = "|".join(sorted((re.escape(kind.value) for kind in FootballEventKind), key=len, reverse=True))
# An entry by our own label ("routine play #57", `EntryLabel`), or by its kind alone (a redacted log's "play").
_LABEL = re.compile(rf"(?:(?P<weight>routine|major) )?(?P<kind>{_KINDS})(?: #(?P<play>{_PLAY_ID}))?")
# A note's entry by its label, then what happens next.
_NOTE_LABEL = re.compile(rf"(?P<weight>routine|major) (?P<kind>{_KINDS})(?: #(?P<play>{_PLAY_ID}))?(?:: (?P<rest>.*))?")
_GAME_THEN = re.compile(rf"(?P<game>{_GAME_ID})(?P<rest>.*)")
_USAGE = re.compile(
    r"quota: NFLMeta in (?P<month>\d{4}-\d{2}): (?P<requests>[\d,]+) (?:of (?P<requests_limit>[\d,]+) )?requests"
    r"(?: \(no limit\))?, (?P<rows>[\d,]+) (?:of (?P<rows_limit>[\d,]+) )?rows(?: \(no limit\))?"
    r"(?:, (?P<per_minute>[\d,]+) a minute)?"
)
_FULL_LOG = re.compile(r"omni\.app\.football: the full log is (?P<path>.+), deleted by (?P<date>\d{4}-\d{2}-\d{2})")
_VERSIONS = re.compile(
    r"versions: policy \S+ \| config \S+ [0-9a-f]+ \| rules \S+ \| models .+ \| source \S+ \| migrations .+"
)
_CONFIG = re.compile(r"config [0-9a-f]+: \S+")
_CLOCK_JUMP = re.compile(
    r"clock: the wall clock jumped [+-]\d+(?:s|m|h\d{2}m) against the monotonic clock(?: \(the host slept\?\))?"
)
_FIXED = {
    "feed answering again": LineKind.FEED_BACK,
    "halftime: holding off the feed": LineKind.HOLD_START,
    "halftime hold over: polling again": LineKind.HOLD_END,
    "final: no more polling": LineKind.PARKED,
    "paint recovered": LineKind.PAINT_RECOVERED,
    "feed stale: no answer; dimming the card until it answers": LineKind.FEED_STALE,
    "display: no frame acknowledgements; showing frames unconfirmed (update board_frame_sink.py on the board)": (
        LineKind.DISPLAY_UNCONFIRMED
    ),
    "market: the ask failed": LineKind.MARKET_ERROR,
    "woke: an answer from after the sleep is up": LineKind.WOKE,
    "woke: dimming the card until an answer from after the sleep is up": LineKind.WOKE,
}
# Each line known by how it starts, and what follows the start in our own words.
_PREFIXED = (
    ("display: starting ", LineKind.DISPLAY_START, _COMMAND),
    ("display: couldn't start it ", LineKind.DISPLAY_START_FAILED, f"({DETAIL}); trying again shortly"),
    ("display: a frame write failed: ", LineKind.DISPLAY_WRITE_FAILED, DETAIL),
    ("display: no frame acknowledgements; ", LineKind.DISPLAY_UNCONFIRMED, DETAIL),
    ("feed stale: ", LineKind.FEED_STALE, DETAIL),
    ("market: ", LineKind.MARKET_ERROR, DETAIL),
    ("paint failed: ", LineKind.PAINT_FAILED, DETAIL),
    ("strip: a caption failed to draw: ", LineKind.CAPTION_FAILED, DETAIL),
    ("step failed: ", LineKind.STEP_FAILED, DETAIL),
)
_LOST = "lost the display process"
_DISPLAY_LOSSES = frozenset({_LOST, "the display process ended"})
_BLANKED_WHY = "the display went away"
_AN_ENTRY = "an entry"  # an entry the runner had no label for
_ASK_FAILED = "the ask failed"
_FETCH = "NFLMeta fetch failed for "
_NOT_LISTED = "NFLMeta no longer lists game"
_UNREADABLE = "NFLMeta sent an unreadable game"
_MARKS = {
    ">": LineKind.ARRIVED,
    "=": LineKind.SHOWN,
    "-": LineKind.DROPPED,
    "x": LineKind.CLEARED,
    "~": LineKind.BLANKED,
}
_CONVERSIONS = frozenset({"XP GOOD", "XP NO GOOD", "2-PT GOOD", "2-PT FAILS"})
_HEADLINES = (
    (re.compile(r"(?:TV|OFFICIAL) TIMEOUT"), FootballEventKind.OFFICIAL_TIMEOUT),
    (re.compile(r"TWO-MINUTE WARNING"), FootballEventKind.TWO_MINUTE_WARNING),
    (re.compile(r"TIMEOUT(?: [A-Z]{2,3})?"), FootballEventKind.TIMEOUT),
    (re.compile(r"INJURY(?: [A-Z]{2,3})?"), FootballEventKind.INJURY),
    (re.compile(r"(?:REVIEW|.+ CHALLENGE): (?:STANDS|REVERSED)"), FootballEventKind.REVIEW),
    (re.compile(r"[A-Z]{2,3} \+\d+"), FootballEventKind.SCORE),
)

MEASURES = SPREAD_MEASURES | frozenset(
    {
        "schema",
        "kind",
        "date",
        "away",
        "home",
        "formats",
        "runs",
        "run_rows",
        "profile",
        "delay",
        "wall",
        "lines",
        "parsed",
        "unparsed",
        "frames",
        "logged",
        "shown_confirmed",
        "shown_unconfirmed",
        "unconfirmed_notes",
        "strip",
        "arrived",
        "shown",
        "shown_again",
        "dropped",
        "cleared",
        "blanked",
        "reason",
        "feed",
        "answer_lines",
        "answer_gap",
        "errors",
        "endpoint",
        "status",
        "refusals",
        "notes",
        "other_notes",
        "stale_spells",
        "stale",
        "market_errors",
        "display",
        "starts",
        "start_failures",
        "drops",
        "write_failures",
        "halftime",
        "holds",
        "start",
        "end",
        "length",
        "clock",
        "backward_steps",
        "backward",
        "silences",
        "silence",
        "final",
        "drain",
        "parked",
        "failures",
        "step",
        "paint",
        "paint_recovered",
        "caption",
        "quota",
        "requests",
        "rows",
        "requests_limit",
        "rows_limit",
        "per_minute",
    }
)
_ENUMS: tuple[type[Enum], ...] = (
    Endpoint,
    FeedErrorKind,
    FeedNoteKind,
    FootballEventKind,
    LogFormat,
    PanelProfile,
    StripReason,
)


# --- reading a line ----------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Usage:
    """The month's NFLMeta usage as a quota line gave it: requests and rows so far, against the plan's limits."""

    month: str
    requests: int
    rows: int
    requests_limit: int | None
    rows_limit: int | None
    per_minute: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class RunnerLine:
    """What one log line said, by its shape, without its words; and the line in our own words."""

    kind: LineKind
    # The line's body in our own words: text, and NFLMeta's ids left for the writer to number.
    ours: tuple[str | NflMetaId, ...]
    prefix: str = ""  # the stamp as written, with the two spaces after it, when the line is stamped
    stamp: int | None = None  # seconds since local midnight, when the line is stamped
    event: FootballEventKind | None = None
    reason: StripReason | None = None
    note: FeedNoteKind | None = None
    unconfirmed: bool = False
    again: bool = False
    phase: NflPhase | None = None
    interval: FootballInterval | None = None
    error: FeedErrorKind | None = None
    endpoint: Endpoint | None = None
    status: int | None = None
    away: str | None = None
    home: str | None = None
    profile: PanelProfile | None = None
    delay: int | None = None
    usage: _Usage | None = None


def read_line(line: str) -> RunnerLine:
    """One line of the runner's log, by its shape; an unknown shape is `LineKind.UNPARSED`."""
    stamped = _STAMP.fullmatch(line)
    if stamped is None:
        return _body(line)
    stamp = int(stamped["h"]) * 3_600 + int(stamped["m"]) * 60 + int(stamped["s"])
    prefix = line[: stamped.start("body")]
    return dataclasses.replace(_body(stamped["body"]), prefix=prefix, stamp=stamp)


def _body(body: str) -> RunnerLine:
    if (kind := _FIXED.get(body)) is not None:
        return RunnerLine(kind=kind, ours=(body,))
    if (start := _START.fullmatch(body)) is not None:
        return _start(start)
    if (written := _FULL_LOG.fullmatch(body)) is not None:
        return RunnerLine(
            kind=LineKind.FULL_LOG, ours=(f"omni.app.football: the full log is {_PATH}, deleted by {written['date']}",)
        )
    for pattern, kind in (
        (_VERSIONS, LineKind.VERSIONS),
        (_CONFIG, LineKind.CONFIG),
        (_CLOCK_JUMP, LineKind.CLOCK_JUMP),
    ):
        if pattern.fullmatch(body) is not None:
            return RunnerLine(kind=kind, ours=(body,))
    if (usage := _USAGE.fullmatch(body)) is not None:
        return RunnerLine(kind=LineKind.QUOTA, ours=(body,), usage=_usage(usage))
    if body.startswith("display: ") and body.endswith("; restarting shortly"):
        return _display_lost(body.removeprefix("display: ").removesuffix("; restarting shortly"))
    if body.startswith("feed: "):
        return _feed_error(body.removeprefix("feed: "))
    for prefix, kind, rest in _PREFIXED:
        if body.startswith(prefix):
            return RunnerLine(kind=kind, ours=(prefix + rest,))
    if body.startswith("  ! "):
        return _note(body.removeprefix("  ! "))
    if (strip := _STRIP.fullmatch(body)) is not None:
        return _strip(strip["mark"], strip["words"])
    if (answer := _ANSWER.fullmatch(body)) is not None:
        return _answer(answer)
    return RunnerLine(kind=LineKind.UNPARSED, ours=(NOT_KEPT,))


def _club(raw: str) -> str | None:
    """A club's canonical abbreviation, or None when it isn't an NFL club."""
    try:
        return canonical_abbreviation(raw)
    except KeyError:
        return None


def _clubs(away: str, home: str) -> str:
    """The two clubs as a line in our own words names them."""
    return f"{_club(away) or _TEAM} @ {_club(home) or _TEAM}"


def _start(start: re.Match[str]) -> RunnerLine:
    """The runner's opening line: the game's two clubs (when they're NFL clubs), the panel and the delay."""
    away, home = _club(start["away"]), _club(start["home"])
    if away is None or home is None:
        away = home = None
    profile = next((profile for profile in PanelProfile if profile.value == start["profile"]), None)
    delay = int(start["delay"])
    return RunnerLine(
        kind=LineKind.START,
        away=away,
        home=home,
        profile=profile,
        delay=delay,
        ours=(
            f"omni.app.football: {_clubs(start['away'], start['home'])} (game ",
            NflMetaId(IdKind.GAME, start["game"]),
            f"), {profile.value if profile is not None else _PROFILE}, delay {delay}s",
        ),
    )


def _usage(usage: re.Match[str]) -> _Usage:
    def count(written: str) -> int:
        return int(written.replace(",", ""))

    def limit(written: str | None) -> int | None:
        return count(written) if written is not None else None

    return _Usage(
        month=usage["month"],
        requests=count(usage["requests"]),
        rows=count(usage["rows"]),
        requests_limit=limit(usage["requests_limit"]),
        rows_limit=limit(usage["rows_limit"]),
        per_minute=limit(usage["per_minute"]),
    )


def _display_lost(why: str) -> RunnerLine:
    """The display process lost: why, when it's in the runner's own words."""
    if why not in _DISPLAY_LOSSES:
        why = f"{_LOST} ({DETAIL})" if why.startswith(f"{_LOST} (") else DETAIL
    return RunnerLine(kind=LineKind.DISPLAY_LOST, ours=(f"display: {why}; restarting shortly",))


def _feed_error(error: str) -> RunnerLine:
    """A ``feed:`` line by the shape of its error: the provider's and the quota's own phrasing. Its kind, the
    endpoint and the HTTP status are kept, and the game it names by its id; the rest is the provider's."""
    status = _HTTP.search(error)
    code = int(status["status"]) if status is not None else None
    tail = f": HTTP Error {code}: {DETAIL}" if code is not None else f": {DETAIL}"
    endpoint = None
    said: tuple[str | NflMetaId, ...]
    if error.startswith("quota: "):
        kind, said = FeedErrorKind.REFUSED, ("quota" + tail,)
    elif error.startswith(_FETCH):
        kind = FeedErrorKind.FETCH_FAILED
        endpoint = Endpoint.PLAY_CHANGES if "/play-changes" in error else Endpoint.LIVE_SCORES
        said = (f"{_FETCH}/{endpoint.value}{tail}",)
    elif error.startswith(_NOT_LISTED):
        kind, said = FeedErrorKind.NOT_LISTED, (f"{_NOT_LISTED} ", *_game_named(error.removeprefix(_NOT_LISTED), tail))
    elif error.startswith(_UNREADABLE):
        kind, said = FeedErrorKind.UNREADABLE, (f"{_UNREADABLE} ", *_game_named(error.removeprefix(_UNREADABLE), tail))
    else:
        kind, said = FeedErrorKind.OTHER, (_ASK_FAILED if error == _ASK_FAILED else tail.removeprefix(": "),)
    return RunnerLine(kind=LineKind.FEED_ERROR, error=kind, endpoint=endpoint, status=code, ours=("feed: ", *said))


def _game_named(text: str, tail: str) -> tuple[str | NflMetaId, ...]:
    """The game an error names by its id, then the error's tail when anything follows the id."""
    named = _GAME_THEN.fullmatch(text.removeprefix(" "))
    if named is None:
        return (_GAME, tail)
    return (NflMetaId(IdKind.GAME, named["game"]), tail if named["rest"] else "")


def _note(text: str) -> RunnerLine:
    """A feed note: its kind, the entry it's about by our own label, and a placeholder for anything else."""
    named, colon, tail = text.partition(": ")
    note = next((kind for kind in FeedNoteKind if kind.value == named), None)
    said: list[str | NflMetaId] = ["  ! ", note.value if note is not None else _NOTE]
    rest: str | None = tail
    if colon and (label := _NOTE_LABEL.fullmatch(tail)) is not None:
        said += [": ", *_label(label)]
        rest = label["rest"]
    if colon and rest:
        said.append(f": {DETAIL}")
    return RunnerLine(kind=LineKind.NOTE, note=note, ours=tuple(said))


def _label(label: re.Match[str]) -> tuple[str | NflMetaId, ...]:
    """An entry's label in our own words: its weight, its kind, and its play by the id."""
    words = (f"{label['weight']} " if label["weight"] else "") + FootballEventKind(label["kind"]).value
    if label["play"] is None:
        return (words,)
    return (f"{words} #", NflMetaId(IdKind.PLAY, label["play"]))


def _strip(mark: str, words: str) -> RunnerLine:
    """A strip line: the entry's kind from its headline or label, and the reason or marks after its words."""
    kind = _MARKS[mark]
    reason = None
    unconfirmed = again = False
    after = ""
    if kind is LineKind.SHOWN:
        unconfirmed = words.endswith(" (unconfirmed)")
        words = words.removesuffix(" (unconfirmed)")
        again = words.endswith(" (again, on the new display)")
        words = words.removesuffix(" (again, on the new display)")
        after = (" (again, on the new display)" if again else "") + (" (unconfirmed)" if unconfirmed else "")
    elif kind is not LineKind.ARRIVED:
        suffix = _SUFFIX.fullmatch(words)
        if suffix is not None:
            words, why = suffix["words"], suffix["why"]
            reason = StripReason(why) if kind is not LineKind.BLANKED else None
            own = StripReason(why) is not StripReason.OTHER or why == _BLANKED_WHY  # the runner's own words
            after = f" ({suffix['what']}: {why if own else DETAIL})"
        elif kind is not LineKind.BLANKED:
            reason = StripReason.OTHER
    event, entry = _entry(words)
    return RunnerLine(
        kind=kind,
        event=event,
        reason=reason,
        unconfirmed=unconfirmed,
        again=again,
        ours=(f"  {mark} ", *entry, after),
    )


def _entry(words: str) -> tuple[FootballEventKind, tuple[str | NflMetaId, ...]]:
    """A strip entry's kind and the entry in our own words: by its label when it has one, else by the kind its
    headline reads as."""
    if words == _AN_ENTRY:
        return FootballEventKind.PLAY, (_AN_ENTRY,)
    if (label := _LABEL.fullmatch(words)) is not None:
        return FootballEventKind(label["kind"]), _label(label)
    kind = _headline_kind(words)
    return kind, (kind.value,)


def _headline_kind(words: str) -> FootballEventKind:
    """A strip entry's kind, from its headline (the words before the first `` | ``)."""
    headline = words.split(" | ", 1)[0]
    if headline in _CONVERSIONS:
        return FootballEventKind.CONVERSION
    return next((kind for pattern, kind in _HEADLINES if pattern.fullmatch(headline)), FootballEventKind.PLAY)


def _answer(answer: re.Match[str]) -> RunnerLine:
    """A changed answer's line: its phase, and its break, if it's in one. In our own words it keeps the clubs, the
    score, the period, the clock, the phase and the break; the field after them is the feed's."""
    phase = NflPhase(answer["early"] or answer["phase"])
    clubs = _clubs(answer["away"], answer["home"])
    if answer["early"] is not None:
        return RunnerLine(kind=LineKind.ANSWER, phase=phase, ours=(f"{clubs}: {phase.value}",))
    rest = answer["rest"]
    interval = next(
        (interval for interval in FootballInterval if re.match(rf" \| {interval.value}(?: |$)", rest)), None
    )
    said = f"{clubs} {answer['score']} Q{answer['period']} {answer['clock']} [{phase.value}]"
    if interval is not None:
        said += f" | {interval.value}"
        rest = rest.removeprefix(f" | {interval.value}")
    if rest:
        said += f" | {DETAIL}"
    return RunnerLine(kind=LineKind.ANSWER, phase=phase, interval=interval, ours=(said,))


# --- runs and nights ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Run:
    """One run of the runner: its opening line, and its lines, each stamp placed in seconds from the midnight
    before the run's first stamp, running on across a midnight it crosses."""

    start: RunnerLine
    lines: list[tuple[RunnerLine, int | None]] = field(default_factory=list)  # each line, and its time if stamped
    jumps: list[int] = field(default_factory=list)  # each step back of the wall clock, in seconds
    days: int = 0  # midnights crossed so far
    last: int | None = None  # the latest stamped line's time

    @property
    def times(self) -> list[int]:
        return [at for _, at in self.lines if at is not None]

    def add(self, line: RunnerLine) -> None:
        """Place ``line`` on the run's clock: a stamp far behind the last crossed midnight, a small step back is a
        jump of the wall clock."""
        at = None
        if line.stamp is not None:
            at = line.stamp + self.days * _DAY
            if self.last is not None and at < self.last - _MIDNIGHT:
                self.days += 1
                at += _DAY
            elif self.last is not None and at < self.last:
                self.jumps.append(self.last - at)
            self.last = at
        self.lines.append((line, at))

    @property
    def format(self) -> LogFormat:
        if any(line.kind in _NIGHT_THREE for line, _ in self.lines):
            return LogFormat.NIGHT_THREE
        later = any(
            line.kind in _NIGHT_TWO
            or (line.kind is LineKind.DISPLAY_START and at is not None)
            or (line.kind is LineKind.SHOWN and (line.unconfirmed or line.again))
            for line, at in self.lines
        )
        return LogFormat.NIGHT_TWO if later else LogFormat.NIGHT_ONE


@dataclass(slots=True)
class Night:
    """The runs of one night, and the lines before the log's first run (only the first night has any)."""

    day: int  # days after the first night
    runs: list[_Run] = field(default_factory=list)
    orphans: list[RunnerLine] = field(default_factory=list)

    @property
    def size(self) -> int:
        """How many of the log's lines are this night's: a night's lines are one stretch of the log."""
        return len(self.orphans) + sum(1 + len(run.lines) for run in self.runs)

    @property
    def formats(self) -> tuple[LogFormat, ...]:
        """The formats its runs were written in, in order."""
        return tuple(sorted({run.format for run in self.runs}, key=lambda written: written.value))


def _runs(lines: Iterable[str]) -> tuple[list[_Run], list[RunnerLine]]:
    """The log's runs, each line's stamp placed on a clock that runs on across midnight; and the lines before the
    first run. The month's usage, logged just before a run's opening line, is that run's."""
    runs: list[_Run] = []
    orphans: list[RunnerLine] = []
    for raw in lines:
        line = read_line(raw)
        if line.kind is LineKind.START:
            run = _Run(line)
            if runs and runs[-1].lines and runs[-1].lines[-1][0].kind is LineKind.QUOTA:
                run.add(runs[-1].lines.pop()[0])
            elif not runs and orphans and orphans[-1].kind is LineKind.QUOTA:
                run.add(orphans.pop())
            runs.append(run)
        elif runs:
            runs[-1].add(line)
        else:
            orphans.append(line)
    return runs, orphans


def read_nights(lines: Iterable[str]) -> list[Night]:
    """The log's runs grouped by night: a run starts a new night when its game differs from the run before's, or it
    starts earlier in the day than the run before ended."""
    runs, orphans = _runs(lines)
    nights = [Night(0, orphans=orphans)]
    previous: _Run | None = None
    for run in runs:
        if previous is not None and (_game(run) != _game(previous) or _earlier(run, previous)):
            nights.append(Night(nights[-1].day + 1))
        nights[-1].runs.append(run)
        previous = run
    return [night for night in nights if night.runs or night.orphans]


def _game(run: _Run) -> tuple[str | None, str | None]:
    return run.start.away, run.start.home


def _earlier(run: _Run, before: _Run) -> bool:
    """Whether ``run`` starts earlier in the day than ``before`` ended: a later night's."""
    first, last = run.times, before.times
    return bool(first and last and first[0] % _DAY < last[-1] % _DAY)


# --- measures ----------------------------------------------------------------------------------------------------


def _counted(lines: Iterable[RunnerLine], *keys: str) -> list[dict[str, Any]]:
    """Lines counted by the given fields' values, one record each, in a stable order."""
    counts: Counter[tuple[Any, ...]] = Counter(tuple(getattr(line, key) for key in keys) for line in lines)
    rows = [
        {
            **{key: value.value if isinstance(value, Enum) else value for key, value in zip(_names(keys), values)},
            "count": count,
        }
        for values, count in counts.items()
    ]
    return sorted(rows, key=lambda row: tuple(str(row[key]) for key in _names(keys)))


def _names(keys: Sequence[str]) -> list[str]:
    return ["kind" if key in ("event", "error", "note") else key for key in keys]


def _spans(run: _Run, opens: LineKind, closes: LineKind) -> list[tuple[int, int | None]]:
    """Each stretch from an ``opens`` line to the next ``closes`` line in a run: its start, and its end if any."""
    spans: list[tuple[int, int | None]] = []
    for line, at in run.lines:
        if at is None:
            continue
        if line.kind is opens and (not spans or spans[-1][1] is not None):
            spans.append((at, None))
        elif line.kind is closes and spans and spans[-1][1] is None:
            spans[-1] = (spans[-1][0], at)
    return spans


def _silences(run: _Run) -> list[float]:
    """Gaps between stamped lines longer than `SILENCE` while the game should be talking: not before kickoff, not in
    the halftime hold, and not after the final (its drain is measured on its own)."""
    gaps: list[float] = []
    quiet = True  # nothing's due before the first answer of a game under way
    previous: int | None = None
    for line, at in run.lines:
        if at is None:
            continue
        if previous is not None and not quiet and at - previous > SILENCE.total_seconds():
            gaps.append(float(at - previous))
        previous = at
        if line.kind is LineKind.ANSWER:
            quiet = line.phase is not NflPhase.IN
        elif line.kind in (LineKind.HOLD_START, LineKind.PARKED):
            quiet = True
        elif line.kind is LineKind.HOLD_END:
            quiet = False
    return gaps


def _drain(run: _Run) -> float | None:
    """How long after the run's first answer from the final it parked, if it did."""
    post = next((at for line, at in run.lines if line.kind is LineKind.ANSWER and line.phase is NflPhase.POST), None)
    if post is None:
        return None
    parked = next(
        (at for line, at in run.lines if line.kind is LineKind.PARKED and at is not None and at >= post), None
    )
    return float(parked - post) if parked is not None else None


def _quota(night: Night) -> dict[str, Any]:
    """The month's NFLMeta usage over the night, from its quota lines: how far the month's requests and rows rose
    from the first to the last (when both are in one month), and the plan's limits as the last gave them."""
    usages = [line.usage for line in night.orphans if line.usage is not None]
    usages += [line.usage for run in night.runs for line, _ in run.lines if line.usage is not None]
    if not usages:
        return {
            "lines": 0,
            "requests": None,
            "rows": None,
            "requests_limit": None,
            "rows_limit": None,
            "per_minute": None,
        }
    first, last = usages[0], usages[-1]
    rose = len(usages) > 1 and first.month == last.month
    return {
        "lines": len(usages),
        "requests": last.requests - first.requests if rose else None,
        "rows": last.rows - first.rows if rose else None,
        "requests_limit": last.requests_limit,
        "rows_limit": last.rows_limit,
        "per_minute": last.per_minute,
    }


def _night_digest(night: Night, first_date: date) -> dict[str, Any]:
    day = (first_date + timedelta(days=night.day)).isoformat()
    lines = [line for run in night.runs for line, _ in run.lines] + night.orphans
    games = {_game(run) for run in night.runs}
    away, home = next(iter(games)) if len(games) == 1 else (None, None)
    of = Counter(line.kind for line in lines)
    shown = [line for line in lines if line.kind is LineKind.SHOWN]
    errors = [line for line in lines if line.kind is LineKind.FEED_ERROR]
    holds = [span for run in night.runs for span in _spans(run, LineKind.HOLD_START, LineKind.HOLD_END)]
    stale = [span for run in night.runs for span in _spans(run, LineKind.FEED_STALE, LineKind.FEED_BACK)]
    answer_gaps = []
    for run in night.runs:
        answers = [at for line, at in run.lines if line.kind is LineKind.ANSWER and at is not None]
        answer_gaps += [float(later - earlier) for earlier, later in zip(answers, answers[1:]) if later >= earlier]
    drains = [drain for run in night.runs if (drain := _drain(run)) is not None]
    return {
        "schema": SCHEMA,
        "kind": DigestKind.RUNNER_LOG.value,
        "date": day,
        "away": away,
        "home": home,
        "formats": [written.value for written in night.formats],
        "runs": len(night.runs),
        "run_rows": [
            {
                "away": run.start.away,
                "home": run.start.home,
                "profile": run.start.profile.value if run.start.profile is not None else None,
                "delay": run.start.delay,
                "wall": float(run.times[-1] - run.times[0]) if len(run.times) > 1 else None,
                "lines": len(run.lines) + 1,
            }
            for run in night.runs
        ],
        "lines": {"parsed": len(lines) + len(night.runs) - of[LineKind.UNPARSED], "unparsed": of[LineKind.UNPARSED]},
        "frames": {
            "logged": False,
            "shown_confirmed": sum(not line.unconfirmed for line in shown),
            "shown_unconfirmed": sum(line.unconfirmed for line in shown),
            "unconfirmed_notes": of[LineKind.DISPLAY_UNCONFIRMED],
        },
        "strip": {
            "arrived": _counted((line for line in lines if line.kind is LineKind.ARRIVED), "event"),
            "shown": _counted((line for line in shown if not line.again), "event"),
            "shown_again": _counted((line for line in shown if line.again), "event"),
            "dropped": _counted((line for line in lines if line.kind is LineKind.DROPPED), "event", "reason"),
            "cleared": _counted((line for line in lines if line.kind is LineKind.CLEARED), "event", "reason"),
            "blanked": _counted((line for line in lines if line.kind is LineKind.BLANKED), "event"),
        },
        "feed": {
            "answer_lines": of[LineKind.ANSWER],
            "answer_gap": Spread.of(answer_gaps).record(),
            "errors": _counted(errors, "error", "endpoint", "status"),
            "refusals": sum(line.error is FeedErrorKind.REFUSED for line in errors),
            "notes": _counted((line for line in lines if line.kind is LineKind.NOTE and line.note), "note"),
            "other_notes": sum(line.kind is LineKind.NOTE and line.note is None for line in lines),
            "stale_spells": len(stale),
            "stale": Spread.of(float(end - begin) for begin, end in stale if end is not None).record(),
            "market_errors": of[LineKind.MARKET_ERROR],
        },
        "display": {
            "starts": of[LineKind.DISPLAY_START],
            "start_failures": of[LineKind.DISPLAY_START_FAILED],
            "drops": of[LineKind.DISPLAY_LOST],
            "write_failures": of[LineKind.DISPLAY_WRITE_FAILED],
        },
        "halftime": {
            "holds": [
                {
                    "start": begin,
                    "end": end,
                    "length": float(end - begin) if end is not None else None,
                }
                for begin, end in holds
            ]
        },
        "clock": {
            "backward_steps": sum(len(run.jumps) for run in night.runs),
            "backward": Spread.of(float(jump) for run in night.runs for jump in run.jumps).record(),
            "silences": sum(len(_silences(run)) for run in night.runs),
            "silence": Spread.of(gap for run in night.runs for gap in _silences(run)).record(),
        },
        "final": {
            "parked": of[LineKind.PARKED],
            "drain": Spread.of(drains).record(),
        },
        "failures": {
            "step": of[LineKind.STEP_FAILED],
            "paint": of[LineKind.PAINT_FAILED],
            "paint_recovered": of[LineKind.PAINT_RECOVERED],
            "caption": of[LineKind.CAPTION_FAILED],
        },
        "quota": _quota(night),
    }


def mine_log(lines: Iterable[str], first_date: date) -> list[dict[str, Any]]:
    """A digest for each night in the runner's log ``lines``, the first night dated ``first_date``."""
    return [_night_digest(night, first_date) for night in read_nights(lines)]


def allowed(digest: Mapping[str, Any]) -> frozenset[str]:
    """The strings a night's digest may hold: our enums' values, its date and its game's two clubs."""
    clubs = {name for row in digest["run_rows"] for name in (row["away"], row["home"]) if name is not None}
    return allowed_values(*_ENUMS, names=(digest["date"], *clubs))


def mine_runner_log(log: Path, out_dir: Path, first_date: date) -> list[Path]:
    """Mine the runner's log at ``log`` into ``out_dir/<date>.json`` (and ``.md``) per night: the digests' paths.

    A night already mined isn't mined again: its digest stays as it is.
    """
    paths: list[Path] = []
    for digest in mine_log(log.read_text(encoding="utf-8", errors="replace").splitlines(), first_date):
        target = out_dir / f"{digest['date']}.json"
        if not target.exists():
            write_digest(digest, target, summary(digest), measures=MEASURES, values=allowed(digest))
        paths.append(target)
    return paths


# --- the summary -------------------------------------------------------------------------------------------------


def _spread(record: Mapping[str, Any]) -> str:
    return f"{figure(record['p25'])} / {figure(record['median'])} / {figure(record['p75'])} (n={record['count']})"


def _tally(rows: Sequence[Mapping[str, Any]]) -> str:
    """Counted rows as a phrase: ``kind (its other fields): count``, each."""
    if not rows:
        return "none"
    phrases = []
    for row in rows:
        extra = [figure(value) for key, value in row.items() if key not in ("kind", "count") and value is not None]
        phrases.append(row["kind"] + (f" ({', '.join(extra)})" if extra else "") + f": {row['count']}")
    return ", ".join(phrases)


def summary(digest: Mapping[str, Any]) -> str:
    """A short Markdown summary of a night's digest, for a person; it holds nothing the digest doesn't."""
    strip, feed, frames, clock, quota = (digest[part] for part in ("strip", "feed", "frames", "clock", "quota"))
    holds = digest["halftime"]["holds"]
    game = f"{figure(digest['away'])} @ {figure(digest['home'])}"
    lines = [
        f"# NFL runner log, {digest['date']}",
        "",
        f"{game}: {digest['runs']} runs ({', '.join(digest['formats'])}); {digest['lines']['parsed']} lines read,"
        f" {digest['lines']['unparsed']} not. Times are seconds (p25 / median / p75).",
        "",
        "## Strip",
        "",
        f"- Arrived: {_tally(strip['arrived'])}.",
        f"- Shown: {_tally(strip['shown'])} ({frames['shown_confirmed']} confirmed by the display,"
        f" {frames['shown_unconfirmed']} unconfirmed); again after a restart: {_tally(strip['shown_again'])}.",
        f"- Dropped unseen: {_tally(strip['dropped'])}.",
        f"- Cleared early: {_tally(strip['cleared'])}. Blanked: {_tally(strip['blanked'])}.",
        "",
        "## Feed and display",
        "",
        f"- Answers logged: {feed['answer_lines']}, {_spread(feed['answer_gap'])} apart.",
        f"- Errors: {_tally(feed['errors'])}; refusals {feed['refusals']}; market errors {feed['market_errors']}.",
        f"- Notes: {_tally(feed['notes'])}; other {feed['other_notes']}.",
        f"- Stale spells: {feed['stale_spells']}, {_spread(feed['stale'])}.",
        f"- Display: {digest['display']['starts']} starts, {digest['display']['drops']} drops,"
        f" {digest['display']['start_failures']} failed starts, {digest['display']['write_failures']} failed writes.",
        f"- Halftime holds: {', '.join(figure(hold['length']) for hold in holds) or 'none'}.",
        f"- Final: parked {digest['final']['parked']} times; drain {_spread(digest['final']['drain'])}.",
        f"- Clock: {clock['backward_steps']} steps back, {clock['silences']} silences ({_spread(clock['silence'])}).",
        f"- Quota: {quota['lines']} NFLMeta usage lines; the month rose {figure(quota['requests'])} requests and"
        f" {figure(quota['rows'])} rows (limits {figure(quota['requests_limit'])} requests,"
        f" {figure(quota['rows_limit'])} rows, {figure(quota['per_minute'])} a minute).",
    ]
    return "\n".join(lines) + "\n"
