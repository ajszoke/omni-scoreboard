"""A run's log, with a provider's words kept apart from our own telemetry.

A log line can quote a provider: a play's description, an injury's text, a note from the feed. NFLMeta's
terms allow no standing copy of its data, so its words may land only in a folder stamped to expire
(`omni.providers.rights`). Each `LogLine` carries its telemetry (kinds, ids, reasons, timings, counts: ours
to keep) apart from the same line as it reads quoting the provider, so a line is routed by its parts and
never by searching its text.

`RunLog` prints each line's telemetry, and appends each line in full to ``runner.log`` in a run folder it
stamps to expire with its source (``build/logs/nflmeta/<date>-<away>-<home>/`` for the NFL runner).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from omni.providers.rights import DataSource, RetentionStamp, stamp_recording

__all__ = ["RUN_LOG_FILE", "LogLine", "RunLog", "print_telemetry"]

RUN_LOG_FILE = "runner.log"


@dataclass(frozen=True, slots=True)
class LogLine:
    """One line of a run's log. ``telemetry`` is ours alone, to print and keep. ``quoting`` is the same line
    with a provider's words in it, for a stamped run log only; None when the line quotes nobody."""

    telemetry: str
    quoting: str | None = None

    @property
    def full(self) -> str:
        """The line as a stamped run log keeps it."""
        return self.quoting if self.quoting is not None else self.telemetry


def print_telemetry(line: LogLine) -> None:
    """A log that prints a line's telemetry and drops what it quotes."""
    print(line.telemetry, flush=True)


def _stdout(text: str) -> None:
    print(text, flush=True)


class RunLog:
    """A run's log: each line's telemetry to ``out`` (stdout), and each line in full to the run folder's
    ``runner.log``, the folder stamped to expire with ``source``. Lines may come from several threads."""

    def __init__(
        self, folder: Path, source: DataSource, now: datetime, *, out: Callable[[str], None] | None = None
    ) -> None:
        self.stamp: RetentionStamp = stamp_recording(folder, source, now)
        self.path = folder / RUN_LOG_FILE
        self._out = out if out is not None else _stdout
        self._lock = threading.Lock()
        self._file = self.path.open("a", encoding="utf-8", buffering=1)

    def __call__(self, line: LogLine | str, *, at: datetime | None = None) -> None:
        """Log ``line``: a `LogLine`, or a line of our own telemetry. With ``at``, each part is prefixed with
        its local time of day."""
        entry = line if isinstance(line, LogLine) else LogLine(line)
        prefix = f"{at.astimezone():%H:%M:%S}  " if at is not None else ""
        with self._lock:
            self._out(prefix + entry.telemetry)
            self._file.write(prefix + entry.full + "\n")

    def close(self) -> None:
        with self._lock:
            self._file.close()
