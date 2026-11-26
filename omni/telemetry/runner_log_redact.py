"""A redacted run log: the NFL runner's log line for line, in our own words, with none of NFLMeta's text or ids.

A night's digest (`runner_log_mine`) keeps its counts and spreads, but a review sometimes needs the timeline itself,
line by line. NFLMeta's terms allow no standing copy of its data, so the runner's own log goes at its delete-by;
the same log in our own words can stay. `redact` writes each line from what `runner_log_mine.read_line` read in
it, in any of the runner's formats, one line out for each line in, in order, under the same stamp. It keeps:
- each line's kind and our own words: the fixed lines, the display's, the halftime hold's and the final's, and the
  versions, config and quota lines;
- a strip entry's mark (``>``, ``=``, ``-``, ``x``, ``~``) and our kind for the entry (`FootballEventKind`, read
  from the board's headline or its label), why it went, and markers such as ``(unconfirmed)``;
- an answer's clubs, score, period, clock, phase and break;
- a feed note's kind, a feed error's kind, endpoint and HTTP status.

It replaces NFLMeta's ids with ordinals numbered in the order first seen (games ``G1``, plays ``P001``), every
provider word (a description, a note's detail, an error's message, the field after an answer's ``|``) with a
placeholder of our own (``[detail]``), and a line of no known shape with ``[line not kept]``. A first line names
the schema, the nights, the formats read, and the promise:

    # omni-redacted-run-log/1 | night 2026-11-19 | formats: nfl runner 2026-11-19 | no NFLMeta text or ids
    20:15:00  NYJ @ NE 0-0 Q1 15:00 [in] | [detail]
    20:16:00    > routine timeout #P001
    20:16:31    ! matched play missing: routine play #P002: [detail]

A redacted log reads the same when redacted again. That is the check: `lines_to_redact` counts the lines a log
would change under redaction, and a log that carries no provider words or ids has none. Every redaction is
checked that way before it's handed back (`RedactionLeak`), and ``tools/redact_run_log.py --check`` runs it over
anything before it's bundled or shared. The expiry sweep's miner (`omni.telemetry.miners`) writes each night's
redacted log beside its digest (`write_redacted_nights`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from omni.telemetry.runner_log_mine import (
    IdKind,
    LineKind,
    LogFormat,
    Night,
    NflMetaId,
    RunnerLine,
    read_line,
    read_nights,
)

__all__ = [
    "REDACTED_SUFFIX",
    "SCHEMA",
    "Redaction",
    "RedactionLeak",
    "lines_to_redact",
    "redact",
    "redact_nights",
    "write_redacted_nights",
]

SCHEMA = "omni-redacted-run-log/1"
REDACTED_SUFFIX = ".redacted.log"  # a night's redacted log, named for its date, beside its digest
_PROMISE = "no NFLMeta text or ids"
_NO_FORMAT = "none"  # a log with no run in it
_DAY = r"\d{4}-\d{2}-\d{2}"
_HEADER = re.compile(
    rf"# {re.escape(SCHEMA)}(?: \| (?:night {_DAY}|nights {_DAY} to {_DAY}))? \| formats: (?P<formats>[^|]+)"
    rf" \| {re.escape(_PROMISE)}"
)
_FORMATS = frozenset({*(written.value for written in LogFormat), _NO_FORMAT})


class RedactionLeak(ValueError):
    """A redaction that would change if redacted again: some line still carries words or ids that aren't ours."""


@dataclass(frozen=True, slots=True)
class Redaction:
    """A log redacted: its header, then one line for each line of the log, and how many of them were kept as they
    were, had words or ids replaced, or weren't kept at all."""

    lines: tuple[str, ...]
    kept: int
    replaced: int
    not_kept: int


class _Ordinals:
    """NFLMeta's ids numbered within one log, each kind in the order first seen: ``G1``, ``P001``."""

    def __init__(self) -> None:
        self._seen: dict[IdKind, dict[str, int]] = {kind: {} for kind in IdKind}

    def __call__(self, named: NflMetaId) -> str:
        seen = self._seen[named.kind]
        number = seen.setdefault(named.raw, len(seen) + 1)
        return f"G{number}" if named.kind is IdKind.GAME else f"P{number:03d}"


def _redacted(lines: Iterable[str]) -> list[tuple[RunnerLine, str]]:
    """Each line as read, and the line in our own words under its stamp, its ids numbered within ``lines``."""
    ordinals = _Ordinals()
    written = []
    for raw in lines:
        line = read_line(raw)
        words = "".join(part if isinstance(part, str) else ordinals(part) for part in line.ours)
        written.append((line, line.prefix + words))
    return written


def _changed(lines: Sequence[str]) -> int:
    """How many of ``lines`` (no header) would change under redaction."""
    return sum(raw != words for raw, (_, words) in zip(lines, _redacted(lines)))


def _header(nights: Sequence[Night], first_date: date | None) -> str:
    """The redacted log's first line: the schema, the nights' dates when the first is known, the formats read, and
    the promise."""
    dated = ""
    if first_date is not None and nights:
        first, last = (first_date + timedelta(days=night.day) for night in (nights[0], nights[-1]))
        dated = (
            f" | night {first.isoformat()}" if first == last else f" | nights {first.isoformat()} to {last.isoformat()}"
        )
    formats = sorted({written.value for night in nights for written in night.formats}) or [_NO_FORMAT]
    return f"# {SCHEMA}{dated} | formats: {', '.join(formats)} | {_PROMISE}"


def _redaction(lines: Sequence[str], nights: Sequence[Night], first_date: date | None) -> Redaction:
    redacted = _redacted(lines)
    body = [words for _, words in redacted]
    if _changed(body):
        raise RedactionLeak("a redacted line would change if redacted again")
    not_kept = sum(line.kind is LineKind.UNPARSED for line, _ in redacted)
    kept = sum(line.kind is not LineKind.UNPARSED and raw == words for raw, (line, words) in zip(lines, redacted))
    return Redaction(
        lines=(_header(nights, first_date), *body),
        kept=kept,
        replaced=len(lines) - kept - not_kept,
        not_kept=not_kept,
    )


def redact(lines: Sequence[str], *, first_date: date | None = None) -> Redaction:
    """The runner's log ``lines`` redacted as one log, its first night dated ``first_date`` when it's known."""
    return _redaction(lines, read_nights(lines), first_date)


def redact_nights(lines: Sequence[str], first_date: date) -> list[tuple[date, Redaction]]:
    """Each night of the runner's log ``lines`` redacted as a log of its own, dated from ``first_date``. A night's
    lines are one stretch of the log, so together the nights' lines are the log's."""
    nights: list[tuple[date, Redaction]] = []
    start = 0
    for night in read_nights(lines):
        day = first_date + timedelta(days=night.day)
        nights.append((day, _redaction(lines[start : start + night.size], [night], first_date)))
        start += night.size
    return nights


def lines_to_redact(lines: Sequence[str]) -> int:
    """How many of a log's lines would change under redaction: none for a log that carries no NFLMeta text or id.

    A redacted log's header counts as one line to redact unless it's in the shape `redact` writes it.
    """
    if not lines or not lines[0].startswith(f"# {SCHEMA.partition('/')[0]}/"):
        return _changed(lines)
    header = _HEADER.fullmatch(lines[0])
    sound = header is not None and set(header["formats"].split(", ")) <= _FORMATS
    return (0 if sound else 1) + _changed(lines[1:])


def write_redacted_nights(log: Path, out_dir: Path, first_date: date) -> list[Path]:
    """Write each night of the runner's log at ``log`` redacted, to ``out_dir/<date>.redacted.log``: the paths.

    A night already written isn't written again: its redacted log stays as it is.
    """
    paths: list[Path] = []
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    for day, redaction in redact_nights(lines, first_date):
        target = out_dir / f"{day.isoformat()}{REDACTED_SUFFIX}"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".partial")
            partial.write_text("\n".join(redaction.lines) + "\n", encoding="utf-8")
            partial.replace(target)
        paths.append(target)
    return paths
