"""Mine the NFL runner's log into our own telemetry, a digest per night, keeping none of its words.

``python -m omni.app.football`` prints its log to stdout, and a night's log can be kept as one file across several
nights and restarts. Its lines are our own format, but many carry NFLMeta's words: a strip entry's description, a
score line, a note's detail. `mine_runner_log` reads the lines by their shape alone, counts and times what they
say, and writes one digest per night (`build/telemetry/runner-log/<date>.json`, schema ``omni-mined/1``) with a
short Markdown summary beside it. A line it can't place is counted, never kept.

**The formats.** The runner's lines changed between the two nights it ran, and both are read:
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

**Runs and nights.** Each run starts with the runner's own line (``omni.app.football: PIT @ CIN (game 1),
quad_128x64, delay 0s``). Stamps carry no date: within a run, a stamp more than 12 hours behind the one before it
crossed midnight, and a smaller step back is a wall-clock jump. A run starts a new night when its game differs
from the run before, or its first stamp is earlier in the day than where the run before ended; nights are dated
one day apart from ``first_date``.

A strip entry's kind is read from its headline, the board's own words for it ("TIMEOUT CIN", "TV TIMEOUT", "XP
GOOD"): our `FootballEventKind`, the officials' timeout logged as "TV TIMEOUT" on both nights included.
"""

from __future__ import annotations

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
    "MEASURES",
    "FeedErrorKind",
    "LineKind",
    "LogFormat",
    "StripReason",
    "mine_log",
    "mine_runner_log",
    "summary",
]

_DAY = 86_400
_MIDNIGHT = 12 * 3_600  # a stamp this far behind the one before it is the next day's
SILENCE = timedelta(minutes=5)  # no line for this long, while the game should be talking, is a silence


class LogFormat(StrEnumMixin, str, Enum):
    """Which version of the runner wrote a run's lines."""

    NIGHT_ONE = "nfl runner 2026-11-15"
    NIGHT_TWO = "nfl runner 2026-11-16"


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
    UNPARSED = "unparsed"


class StripReason(StrEnumMixin, str, Enum):
    """Why the strip dropped or cleared an entry, as the runner worded it on either night."""

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


# Lines only the second night's runner wrote.
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
_STAMP = re.compile(r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})  (?P<body>.*)")
_START = re.compile(
    r"omni\.app\.football: (?P<away>[A-Z]{2,3}) @ (?P<home>[A-Z]{2,3}) \(game \d+\), (?P<profile>\S+),"
    r" delay (?P<delay>\d+)s"
)
_ANSWER = re.compile(
    r"[A-Z]{2,3} @ [A-Z]{2,3}(?:: (?P<early>pre|in|post)"
    r"| \d+-\d+ Q(?P<period>\d+) \d+:\d{2} \[(?P<phase>pre|in|post)\](?P<rest>.*))"
)
_STRIP = re.compile(r"  (?P<mark>[>=\-x~]) (?P<words>.*)")
_SUFFIX = re.compile(r"(?P<words>.*) \((?P<what>unseen|cleared|blanked): (?P<why>[^()]*)\)")
_HTTP = re.compile(r"HTTP Error (?P<status>\d{3})")
_FIXED = {
    "feed answering again": LineKind.FEED_BACK,
    "halftime: holding off the feed": LineKind.HOLD_START,
    "halftime hold over: polling again": LineKind.HOLD_END,
    "final: no more polling": LineKind.PARKED,
    "paint recovered": LineKind.PAINT_RECOVERED,
}
_PREFIXED = (
    ("display: starting ", LineKind.DISPLAY_START),
    ("display: couldn't start it ", LineKind.DISPLAY_START_FAILED),
    ("display: a frame write failed: ", LineKind.DISPLAY_WRITE_FAILED),
    ("display: no frame acknowledgements; ", LineKind.DISPLAY_UNCONFIRMED),
    ("feed stale: ", LineKind.FEED_STALE),
    ("feed: ", LineKind.FEED_ERROR),
    ("market: ", LineKind.MARKET_ERROR),
    ("paint failed: ", LineKind.PAINT_FAILED),
    ("strip: a caption failed to draw: ", LineKind.CAPTION_FAILED),
    ("step failed: ", LineKind.STEP_FAILED),
)
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


@dataclass(frozen=True, slots=True, kw_only=True)
class _Line:
    """What one log line said, by its shape, without its words."""

    kind: LineKind
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


def _read_line(line: str) -> _Line:
    """One line of the runner's log, by its shape; an unknown shape is `LineKind.UNPARSED`."""
    stamped = _STAMP.fullmatch(line)
    stamp = None
    body = line
    if stamped is not None:
        stamp = int(stamped["h"]) * 3_600 + int(stamped["m"]) * 60 + int(stamped["s"])
        body = stamped["body"]
    return _body(body, stamp)


def _body(body: str, stamp: int | None) -> _Line:
    if (kind := _FIXED.get(body)) is not None:
        return _Line(kind=kind, stamp=stamp)
    if (start := _START.fullmatch(body)) is not None:
        return _start(start, stamp)
    if body.startswith("display: ") and body.endswith("; restarting shortly"):
        return _Line(kind=LineKind.DISPLAY_LOST, stamp=stamp)
    for prefix, kind in _PREFIXED:
        if body.startswith(prefix):
            return (
                _feed_error(body.removeprefix(prefix), stamp)
                if kind is LineKind.FEED_ERROR
                else _Line(kind=kind, stamp=stamp)
            )
    if body.startswith("  ! "):
        named = body.removeprefix("  ! ").split(": ", 1)[0]
        note = next((kind for kind in FeedNoteKind if kind.value == named), None)
        return _Line(kind=LineKind.NOTE, stamp=stamp, note=note)
    if (strip := _STRIP.fullmatch(body)) is not None:
        return _strip(_MARKS[strip["mark"]], strip["words"], stamp)
    if (answer := _ANSWER.fullmatch(body)) is not None:
        return _answer(answer, stamp)
    return _Line(kind=LineKind.UNPARSED, stamp=stamp)


def _start(start: re.Match[str], stamp: int | None) -> _Line:
    """The runner's opening line: the game's two clubs (when they're NFL clubs), the panel and the delay."""
    away: str | None
    home: str | None
    try:
        away, home = canonical_abbreviation(start["away"]), canonical_abbreviation(start["home"])
    except KeyError:
        away = home = None
    profile = next((profile for profile in PanelProfile if profile.value == start["profile"]), None)
    return _Line(kind=LineKind.START, stamp=stamp, away=away, home=home, profile=profile, delay=int(start["delay"]))


def _feed_error(error: str, stamp: int | None) -> _Line:
    """A ``feed:`` line by the shape of its error: the provider's and the quota's own phrasing."""
    status = _HTTP.search(error)
    endpoint = None
    if error.startswith("quota: "):
        kind = FeedErrorKind.REFUSED
    elif error.startswith("NFLMeta fetch failed for "):
        kind = FeedErrorKind.FETCH_FAILED
        endpoint = Endpoint.PLAY_CHANGES if "/play-changes" in error else Endpoint.LIVE_SCORES
    elif error.startswith("NFLMeta no longer lists game"):
        kind = FeedErrorKind.NOT_LISTED
    elif error.startswith("NFLMeta sent an unreadable game"):
        kind = FeedErrorKind.UNREADABLE
    else:
        kind = FeedErrorKind.OTHER
    return _Line(
        kind=LineKind.FEED_ERROR,
        stamp=stamp,
        error=kind,
        endpoint=endpoint,
        status=int(status["status"]) if status is not None else None,
    )


def _strip(kind: LineKind, words: str, stamp: int | None) -> _Line:
    """A strip line: the entry's kind from its headline, and the reason or marks after its words."""
    reason = None
    unconfirmed = again = False
    if kind is LineKind.SHOWN:
        unconfirmed = words.endswith(" (unconfirmed)")
        words = words.removesuffix(" (unconfirmed)")
        again = words.endswith(" (again, on the new display)")
        words = words.removesuffix(" (again, on the new display)")
    elif kind is not LineKind.ARRIVED:
        suffix = _SUFFIX.fullmatch(words)
        if suffix is not None:
            words = suffix["words"]
            reason = StripReason(suffix["why"]) if kind is not LineKind.BLANKED else None
        elif kind is not LineKind.BLANKED:
            reason = StripReason.OTHER
    return _Line(
        kind=kind, stamp=stamp, event=_headline_kind(words), reason=reason, unconfirmed=unconfirmed, again=again
    )


def _headline_kind(words: str) -> FootballEventKind:
    """A strip entry's kind, from its headline (the words before the first `` | ``)."""
    headline = words.split(" | ", 1)[0]
    if headline in _CONVERSIONS:
        return FootballEventKind.CONVERSION
    return next((kind for pattern, kind in _HEADLINES if pattern.fullmatch(headline)), FootballEventKind.PLAY)


def _answer(answer: re.Match[str], stamp: int | None) -> _Line:
    """A changed answer's line: its phase, and its break, if it's in one. The score and the field go unread."""
    phase = NflPhase(answer["early"] or answer["phase"])
    rest = answer["rest"] or ""
    interval = next(
        (interval for interval in FootballInterval if re.match(rf" \| {interval.value}(?: |$)", rest)), None
    )
    return _Line(kind=LineKind.ANSWER, stamp=stamp, phase=phase, interval=interval)


# --- runs and nights ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Run:
    """One run of the runner: its opening line, and its lines, each stamp placed in seconds from the midnight
    before the run's first stamp, running on across a midnight it crosses."""

    start: _Line
    lines: list[tuple[_Line, int | None]] = field(default_factory=list)  # each line, and its time if stamped
    jumps: list[int] = field(default_factory=list)  # each step back of the wall clock, in seconds
    days: int = 0  # midnights crossed so far
    last: int | None = None  # the latest stamped line's time

    @property
    def times(self) -> list[int]:
        return [at for _, at in self.lines if at is not None]

    def add(self, line: _Line) -> None:
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
        later = any(
            line.kind in _NIGHT_TWO
            or (line.kind is LineKind.DISPLAY_START and at is not None)
            or (line.kind is LineKind.SHOWN and (line.unconfirmed or line.again))
            for line, at in self.lines
        )
        return LogFormat.NIGHT_TWO if later else LogFormat.NIGHT_ONE


@dataclass(slots=True)
class _Night:
    """The runs of one night, and the lines before the log's first run (only the first night has any)."""

    day: int  # days after the first night
    runs: list[_Run] = field(default_factory=list)
    orphans: list[_Line] = field(default_factory=list)


def _runs(lines: Iterable[str]) -> tuple[list[_Run], list[_Line]]:
    """The log's runs, each line's stamp placed on a clock that runs on across midnight; and the lines before the
    first run."""
    runs: list[_Run] = []
    orphans: list[_Line] = []
    for raw in lines:
        line = _read_line(raw)
        if line.kind is LineKind.START:
            runs.append(_Run(line))
        elif runs:
            runs[-1].add(line)
        else:
            orphans.append(line)
    return runs, orphans


def _nights(runs: Sequence[_Run], orphans: list[_Line]) -> list[_Night]:
    """Runs grouped by night: a run starts a new night when its game differs from the run before's, or it starts
    earlier in the day than the run before ended."""
    nights = [_Night(0, orphans=orphans)]
    previous: _Run | None = None
    for run in runs:
        if previous is not None and (_game(run) != _game(previous) or _earlier(run, previous)):
            nights.append(_Night(nights[-1].day + 1))
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


def _counted(lines: Iterable[_Line], *keys: str) -> list[dict[str, Any]]:
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


def _night_digest(night: _Night, first_date: date) -> dict[str, Any]:
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
        "formats": sorted({run.format.value for run in night.runs}),
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
    }


def mine_log(lines: Iterable[str], first_date: date) -> list[dict[str, Any]]:
    """A digest for each night in the runner's log ``lines``, the first night dated ``first_date``."""
    runs, orphans = _runs(lines)
    return [_night_digest(night, first_date) for night in _nights(runs, orphans)]


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
    strip, feed, frames, clock = digest["strip"], digest["feed"], digest["frames"], digest["clock"]
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
    ]
    return "\n".join(lines) + "\n"
