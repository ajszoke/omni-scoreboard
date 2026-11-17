"""What a game state's values rest on, field by field.

A parser that finds a field missing still has to show something, so it fills the gap: 0 outs, empty bases,
the top of the inning. Those stand-ins look like real values. The coverage beside them says which values the
source actually gave, so a reader that reasons from the state (leverage, say) can tell a default from a fact.
It changes nothing a panel draws.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

from omni.core.enum import StrEnumMixin

__all__ = ["CoverageMap", "FieldCoverage"]

F = TypeVar("F", bound=Enum)


class FieldCoverage(StrEnumMixin, str, Enum):
    """What one field's value rests on.

    Only a reported or inferred value is one to reason from (`known`). The other three say why not: the value
    beside a DEFAULTED field is a stand-in, and beside the last two it is None.
    """

    REPORTED = "reported"  # the source gave it; a reported 0 is a real zero
    INFERRED = "inferred"  # derived from data that vouches for it: timeouts from a play log read to its end
    DEFAULTED = "defaulted"  # the source left it out, and a stand-in shows in its place: 0 outs, empty bases
    UNAVAILABLE = "unavailable"  # the source left it out or can't give it, and nothing shows in its place
    NOT_APPLICABLE = "not_applicable"  # it means nothing in this state: no down after the final

    @property
    def known(self) -> bool:
        """Whether the value is one to reason from: reported, or inferred from data that vouches for it."""
        return self in _KNOWN


_KNOWN = frozenset({FieldCoverage.REPORTED, FieldCoverage.INFERRED})


@dataclass(frozen=True, slots=True)
class CoverageMap(Generic[F]):
    """What each field of one game state rests on.

    A field the map doesn't name was reported, so the empty map, a state's default, says every field was. Only
    the exceptions are kept, as a set of pairs, so two maps that say the same thing are equal and the state that
    carries one stays hashable.
    """

    exceptions: frozenset[tuple[F, FieldCoverage]] = frozenset()

    def __post_init__(self) -> None:
        fields = [field for field, _ in self.exceptions]
        if len(set(fields)) != len(fields):
            raise ValueError("a field rests on one thing at a time")
        if any(coverage is FieldCoverage.REPORTED for _, coverage in self.exceptions):
            raise ValueError("a reported field is left out of the exceptions")

    @classmethod
    def of(cls, coverage: Mapping[F, FieldCoverage]) -> CoverageMap[F]:
        """The map that says ``coverage``; a field it leaves out, or gives as REPORTED, was reported."""
        return cls(
            frozenset((field, level) for field, level in coverage.items() if level is not FieldCoverage.REPORTED)
        )

    def __getitem__(self, field: F) -> FieldCoverage:
        return next((level for named, level in self.exceptions if named == field), FieldCoverage.REPORTED)

    def updated(self, coverage: Mapping[F, FieldCoverage]) -> CoverageMap[F]:
        """This map, with ``coverage`` saying anew what those fields rest on."""
        return CoverageMap.of({**dict(self.exceptions), **coverage})
