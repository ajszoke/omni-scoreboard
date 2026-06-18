"""Concrete pixel geometry for each supported panel profile."""

from __future__ import annotations

from dataclasses import dataclass

from omni.core.enum import PanelProfile

__all__ = ["PanelGeometry", "GEOMETRY", "geometry_for"]


@dataclass(frozen=True, slots=True)
class PanelGeometry:
    """The logical pixel dimensions of a panel profile."""

    profile: PanelProfile
    width: int
    height: int

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)


GEOMETRY: dict[PanelProfile, PanelGeometry] = {
    PanelProfile.SINGLE_64X32: PanelGeometry(PanelProfile.SINGLE_64X32, 64, 32),
    PanelProfile.STACK_64X64: PanelGeometry(PanelProfile.STACK_64X64, 64, 64),
    PanelProfile.QUAD_128X64: PanelGeometry(PanelProfile.QUAD_128X64, 128, 64),
}


def geometry_for(profile: PanelProfile) -> PanelGeometry:
    return GEOMETRY[profile]
