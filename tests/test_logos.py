"""The logo-source vocabulary: the typed dimensions the asset pipeline works in."""

from __future__ import annotations

from omni.domain.logos import LogoKind, LogoSource, LogoSurface, LogoVariant


def test_logo_kind_members() -> None:
    assert {k.value for k in LogoKind} == {"cap", "primary"}


def test_logo_surface_members() -> None:
    # The two polarities official marks ship in — picking by background avoids recolour.
    assert {s.value for s in LogoSurface} == {"on_light", "on_dark"}


def test_logo_source_members_and_string_value() -> None:
    assert {s.value for s in LogoSource} == {"mlbstatic", "wikimedia"}
    assert LogoSource.WIKIMEDIA.value == "wikimedia"  # tagged into the cache manifest as a plain string


def test_logo_variant_members() -> None:
    # The two committed tiles per club; the renderer picks one per matchup to avoid a clash.
    assert {v.value for v in LogoVariant} == {"base", "alt"}
