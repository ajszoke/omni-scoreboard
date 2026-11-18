"""What a board's owner chose: the teams they follow, ranked within each league.

It sits above the leagues' club tables, which it checks each choice against, and below the queue and
the app, which read it.
"""

from __future__ import annotations

from omni.preferences.favorites import Favorites, parse_favorites

__all__ = ["Favorites", "parse_favorites"]
