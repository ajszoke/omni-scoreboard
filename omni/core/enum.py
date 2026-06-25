"""Typed enums and tolerant coercion for the Omni Scoreboard domain.

Cribbed in spirit from the Alpine ``enum.py`` approach (see
``docs/agent_context/ARCHITECTURE_TYPED_DOMAIN.md``):

- :class:`StrEnumMixin` — string-valued identifier enums where the value is
  canonical and JSON-safe; ``str(member)`` and ``to_json_value()`` return it.
- :class:`IntEnumMixin` — ordered severities/priorities where comparison is
  meaningful; they serialize to their lowercased member *name*, not the int.
- :func:`try_coerce_enum` — best-effort coercion for config/fixture/replay readers.

Strict construction paths (``League("mlb")``) fail fast. ``try_coerce_enum`` is the
only tolerant path, reserved for debugging/replay/migration at boundaries where
malformed historical input must not crash inspection tools.
"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Any, TypeVar

__all__ = [
    "EnumMixin",
    "StrEnumMixin",
    "IntEnumMixin",
    "try_coerce_enum",
    "Sport",
    "League",
    "PanelProfile",
    "GameStatus",
    "HomeAway",
    "DisplayPriority",
    "UpdateUrgency",
]

E = TypeVar("E", bound=Enum)


def try_coerce_enum(enum_cls: type[E], raw: Any) -> E | None:
    """Best-effort coerce ``raw`` into a member of ``enum_cls``; ``None`` on failure.

    Order: pass-through if already a member, construction by value
    (``enum_cls(raw)``), then lookup by upper-cased name (``enum_cls[raw.upper()]``)
    which is what lets name-serialized int enums round-trip. ``bool`` is rejected up
    front so ``True``/``False`` never silently coerce into ``0``/``1``-valued members.
    """
    if isinstance(raw, enum_cls):
        return raw
    if isinstance(raw, bool):
        return None
    try:
        return enum_cls(raw)
    except (ValueError, KeyError, TypeError):
        pass
    if isinstance(raw, str):
        try:
            return enum_cls[raw.upper()]
        except KeyError:
            return None
    return None


class EnumMixin:
    """Common interface for Omni typed enums (mixed into ``enum.Enum`` subclasses)."""

    if TYPE_CHECKING:
        # Provided at runtime by ``enum.Enum``; declared here for the type checker
        # only (guarded so they never become enum members) so the mixin methods can
        # reference ``self.value`` / ``self.name`` without per-call casts.
        name: str
        value: Any

    def to_json_value(self) -> str | int:
        # The canonical serialization for every string-valued enum: the value already
        # *is* its JSON identity, so those enums inherit this unchanged. Only
        # ``IntEnumMixin`` overrides it — an int member's identity is its name, never
        # the backing number — which is why this base must stay concrete, not abstract.
        value = self.value
        assert isinstance(value, (str, int))  # always true for Omni enums
        return value


class StrEnumMixin(EnumMixin):
    """String-valued enum: ``str(member)`` and ``to_json_value()`` return the value.

    Only ``__str__`` is overridden; ``to_json_value`` is inherited from ``EnumMixin``
    (the string value is already the canonical JSON form)."""

    def __str__(self) -> str:
        return str(self.value)


class IntEnumMixin(EnumMixin):
    """Ordered ``IntEnum`` that serializes to its lowercased member name.

    Note: f-string interpolation of an int-enum member formats the backing
    *integer* (``int.__format__``), so always serialize via ``to_json_value()`` or
    ``str()`` — never bare ``f"{member}"``.
    """

    def __str__(self) -> str:
        return self.name.lower()

    def to_json_value(self) -> str:
        return self.name.lower()


class Sport(StrEnumMixin, str, Enum):
    BASEBALL = "baseball"
    FOOTBALL = "football"
    BASKETBALL = "basketball"
    HOCKEY = "hockey"
    GOLF = "golf"


class League(StrEnumMixin, str, Enum):
    MLB = "mlb"
    NFL = "nfl"
    NBA = "nba"
    NHL = "nhl"
    PGA = "pga"
    NCAAF = "ncaaf"
    NCAAB = "ncaab"

    @property
    def sport(self) -> Sport:
        return _LEAGUE_SPORT[self]


# Defined after both enums; built once rather than per `League.sport` access. The
# `test_every_league_maps_to_a_sport` test guards completeness when leagues are added.
_LEAGUE_SPORT: dict[League, Sport] = {
    League.MLB: Sport.BASEBALL,
    League.NFL: Sport.FOOTBALL,
    League.NBA: Sport.BASKETBALL,
    League.NHL: Sport.HOCKEY,
    League.PGA: Sport.GOLF,
    League.NCAAF: Sport.FOOTBALL,
    League.NCAAB: Sport.BASKETBALL,
}


class PanelProfile(StrEnumMixin, str, Enum):
    SINGLE_64X32 = "single_64x32"
    STACK_64X64 = "stack_64x64"
    QUAD_128X64 = "quad_128x64"


class GameStatus(StrEnumMixin, str, Enum):
    SCHEDULED = "scheduled"
    PREGAME = "pregame"
    LIVE = "live"
    DELAYED = "delayed"
    SUSPENDED = "suspended"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


class HomeAway(StrEnumMixin, str, Enum):
    """Which side of a team contest — the typed result of a winner derivation."""

    AWAY = "away"
    HOME = "home"


class DisplayPriority(IntEnumMixin, IntEnum):
    BACKGROUND = 0
    NORMAL = 10
    FAVORITE = 20
    HIGH_LEVERAGE = 30
    ALERT = 40
    STICKY = 50


class UpdateUrgency(IntEnumMixin, IntEnum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    LIVE_CRITICAL = 3
