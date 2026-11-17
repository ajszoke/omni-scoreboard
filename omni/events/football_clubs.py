"""The league's play-by-play club codes, as its play text writes them, and the board's abbreviations for them.

Play text names five clubs by codes of their own: "to BLT 12" is Baltimore's 12, and "LA" is the Rams. A club read from play text
goes through `club_code`, and text a board shows goes through `canonical_play_text`. The team registry
(`omni.providers.nfl_teams`) takes these codes as aliases too, so this is their one table.
"""

from __future__ import annotations

import re

__all__ = ["PLAY_BY_PLAY_CODES", "canonical_play_text", "club_code"]

# The league's play-by-play codes that differ from the board's abbreviations.
PLAY_BY_PLAY_CODES: dict[str, str] = {"BLT": "BAL", "CLV": "CLE", "ARZ": "ARI", "HST": "HOU", "LA": "LAR"}
_CODE = re.compile(r"\b(?:" + "|".join(PLAY_BY_PLAY_CODES) + r")\b")  # a whole word, in capitals


def club_code(raw: str) -> str:
    """The board's abbreviation for a club code read from play text ("BLT" is "BAL"); any other code stays
    as written."""
    return PLAY_BY_PLAY_CODES.get(raw, raw)


def canonical_play_text(text: str) -> str:
    """Play text with its play-by-play club codes as the board's abbreviations: "to BLT 12" reads "to BAL
    12". Only a whole word in capitals is a code."""
    return _CODE.sub(lambda found: PLAY_BY_PLAY_CODES[found[0]], text)
