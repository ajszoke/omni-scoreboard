"""Typed vocabulary for the team-logo source pipeline.

The committed logo tiles are generated (offline, by `tools/`) from official vector
art. These enums name the provenance dimensions that art is fetched and composited
in, so the cache manifest and the render treatments are typed rather than stringly
described. The runtime consumes the finished tiles (`LogoAsset` / `LogoStore`); this
is the typed record of *where each tile came from* and *how the source is polarised*.
"""

from __future__ import annotations

from enum import Enum

from omni.core.enum import StrEnumMixin

__all__ = ["LogoKind", "LogoSurface", "LogoSource", "LogoVariant"]


class LogoKind(StrEnumMixin, str, Enum):
    """Which mark: the simplified cap insignia, or the full primary logo."""

    CAP = "cap"
    PRIMARY = "primary"


class LogoSurface(StrEnumMixin, str, Enum):
    """The polarity an official mark is drawn for — picking the one that suits the
    target background is what lets the renderer recolour nothing."""

    ON_LIGHT = "on_light"  # a dark mark, for light backgrounds
    ON_DARK = "on_dark"  # a light mark, for dark backgrounds


class LogoSource(StrEnumMixin, str, Enum):
    """Where the source art was fetched from."""

    MLBSTATIC = "mlbstatic"  # MLB's static CDN (edit-quality, team-id keyed)
    WIKIMEDIA = "wikimedia"  # a club mark the CDN lacks (HOU's bare cap, the BOS socks)


class LogoVariant(StrEnumMixin, str, Enum):
    """Which committed tile a club shows in a given matchup.

    Every club has both; the base is its primary colour and the alt a distinct second
    one. A club shows its alt only to break a clash — when its base background reads as
    the same colour as the opponent's on a small panel (see `renderers.logo_clash`)."""

    BASE = "base"  # the primary tile, `<abbr>.png`
    ALT = "alt"  # the distinct second tile, `<abbr>-alt.png`
