"""A mined digest: our own measurements, written as JSON beside a short Markdown summary, and guarded.

A digest holds only what we measured (`omni.providers.rights.Telemetry.OWN_DECISIONS`): counts, spans in
seconds, our own enums' values, and a few names the caller vouches for (a recording folder's name, a date, the
two clubs' abbreviations). It never holds a provider's words. `check_digest` enforces that before anything is
written: every key must be a measure name the miner declared, and every string value one of the allowed
values, so a stray description, player or cursor fails loudly instead of landing on disk. Its error names
where the offending entry sits, never the entry itself.

A spread (`Spread`) sums up a list of spans as its count, minimum, quartiles, median and maximum, with
quartiles interpolated linearly between the nearest ranks.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from omni.core.enum import StrEnumMixin

__all__ = [
    "SCHEMA",
    "SPREAD_MEASURES",
    "DigestKind",
    "DigestLeak",
    "Spread",
    "allowed_values",
    "check_digest",
    "figure",
    "seconds",
    "write_digest",
]

SCHEMA = "omni-mined/1"
_PLACES = 3  # spans keep millisecond precision, as the recordings stamp arrivals


class DigestKind(StrEnumMixin, str, Enum):
    """What a digest was mined from."""

    NFLMETA_RECORDING = "nflmeta recording"
    RUNNER_LOG = "nfl runner log"


class DigestLeak(ValueError):
    """A digest holding something that isn't ours to keep: a key or a string off its allow-list."""


def seconds(span: float) -> float:
    """A span in seconds, rounded to the digest's precision."""
    return round(span, _PLACES)


def figure(value: Any) -> str:
    """A digest's value as a summary prints it: a number without trailing zeros, a missing one as a dash."""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{_PLACES}f}".rstrip("0").rstrip(".")
    return str(value)


@dataclass(frozen=True, slots=True)
class Spread:
    """A list of spans summed up: how many, the least, the quartiles and median, and the most."""

    count: int
    least: float | None = None
    p25: float | None = None
    median: float | None = None
    p75: float | None = None
    most: float | None = None

    @classmethod
    def of(cls, values: Iterable[float]) -> Spread:
        ordered = sorted(values)
        if not ordered:
            return cls(0)
        return cls(
            count=len(ordered),
            least=seconds(ordered[0]),
            p25=seconds(_quantile(ordered, 0.25)),
            median=seconds(_quantile(ordered, 0.5)),
            p75=seconds(_quantile(ordered, 0.75)),
            most=seconds(ordered[-1]),
        )

    def record(self) -> dict[str, int | float | None]:
        return {
            "count": self.count,
            "min": self.least,
            "p25": self.p25,
            "median": self.median,
            "p75": self.p75,
            "max": self.most,
        }


SPREAD_MEASURES = frozenset({"count", "min", "p25", "median", "p75", "max"})


def _quantile(ordered: Sequence[float], q: float) -> float:
    """The ``q`` quantile of sorted values, interpolated linearly between the two nearest ranks."""
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def allowed_values(*enums: type[Enum], names: Iterable[str] = ()) -> frozenset[str]:
    """The strings a digest may hold: the schema and digest kinds, the values of ``enums``, and ``names``."""
    values = {SCHEMA, *(kind.value for kind in DigestKind), *names}
    for enum in enums:
        values.update(str(member.value) for member in enum)
    return frozenset(values)


def check_digest(digest: Mapping[str, Any], *, measures: frozenset[str], values: frozenset[str]) -> None:
    """Raise `DigestLeak` unless every key of ``digest`` is in ``measures`` and every string in ``values``.

    Numbers, booleans and nulls pass; any other type is refused, so nothing unexpected is serialized.
    """
    _check(digest, "digest", measures, values)


def _check(value: Any, where: str, measures: frozenset[str], values: frozenset[str]) -> None:
    if isinstance(value, Mapping):
        for key, inner in value.items():
            if not isinstance(key, str) or key not in measures:
                raise DigestLeak(f"a key in {where} isn't a declared measure")
            _check(inner, f"{where}.{key}", measures, values)
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            _check(inner, f"{where}[{index}]", measures, values)
    elif isinstance(value, str):
        if value not in values:
            raise DigestLeak(f"the string at {where} isn't an allowed value")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise DigestLeak(f"the value at {where} isn't a number, a flag or an allowed string")


def write_digest(
    digest: Mapping[str, Any], path: Path, summary: str, *, measures: frozenset[str], values: frozenset[str]
) -> Path:
    """Check ``digest``, then write it to ``path`` as JSON and ``summary`` beside it as Markdown.

    The JSON goes last, through a temporary file, so a digest on disk always has its summary.
    """
    check_digest(digest, measures=measures, values=values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".md").write_text(summary, encoding="utf-8")
    partial = path.with_suffix(".json.partial")
    partial.write_text(json.dumps(digest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    partial.replace(path)
    return path
