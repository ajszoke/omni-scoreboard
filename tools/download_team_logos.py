"""Regenerate the MLB cap-insignia logo tiles in ``assets/logos/mlb/``.

This is the provenance for the committed 20x20 tiles — not run at app time. Each
tile is a Wikipedia cap insignia, de-trademarked (the tiny (TM)/(R) glyphs are
dropped), thumbnailed to 20x20, and composited onto the club's flat background
colour. The renderer blits the finished tile; it never fetches or processes a logo.

Pipeline per team:

1. Fetch the source PNG from Wikimedia.
2. ``remove_small_symbols`` — threshold the alpha, keep only contours above an area
   floor, so the trademark glyphs (a handful of pixels) drop out while the insignia
   stays. Without this the (TM)/(R) marks survive as stray specks once shrunk to 20px.
3. Thumbnail to 20x20 (LANCZOS), centre on a flat ``BG_COLORS[abbr]`` background.
4. Save as ``assets/logos/mlb/<abbr>.png`` (lower-case, matching the team registry key).

Abbreviations are the registry's canonical StatsAPI codes (``AZ`` not ``ARI``, ``ATH``
not ``OAK`` after the Athletics' rebrand) so a generated tile resolves directly from
``MlbTeamRegistry`` — see ``omni/providers/mlb_teams.py``.

Caveat: Wikimedia now rejects arbitrary thumbnail widths (HTTP 400, "use thumbnail
sizes listed on https://w.wiki/GHai"). The widths baked into the URLs below were
valid when the tiles were first generated; re-running today may require swapping each
``<N>px-`` segment for a currently-allowed size. The committed tiles remain the
source of truth regardless.

Requires ``requests``, ``numpy``, ``opencv-python``, ``Pillow`` (not app deps; install
ad hoc to regenerate).
"""

from __future__ import annotations

import os
from io import BytesIO

import cv2
import numpy as np
import requests
from PIL import Image

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; omni-scoreboard logo fetch)"}
_SIZE = (20, 20)

# abbr -> Wikimedia cap-insignia URL. Keys are the registry's canonical codes.
LOGO_URLS: dict[str, str] = {
    "AZ": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ac/Arizona_Diamondbacks_logo_teal.svg/1024px-Arizona_Diamondbacks_logo_teal.svg.png",
    "ATL": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/7a/Atlanta_Braves_Insignia.svg/821px-Atlanta_Braves_Insignia.svg.png",
    "BAL": "https://upload.wikimedia.org/wikipedia/en/thumb/7/75/Baltimore_Orioles_cap.svg/253px-Baltimore_Orioles_cap.svg.png",
    "BOS": "https://upload.wikimedia.org/wikipedia/en/thumb/6/6d/RedSoxPrimary_HangingSocks.svg/2094px-RedSoxPrimary_HangingSocks.svg.png",
    "CHC": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/89/Chicago_Cubs_Cap_Insignia.svg/727px-Chicago_Cubs_Cap_Insignia.svg.png",
    "CWS": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/d6/Chicago_White_Sox_Insignia.svg/1024px-Chicago_White_Sox_Insignia.svg.png",
    "CIN": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/71/Cincinnati_Reds_Cap_Insignia.svg/838px-Cincinnati_Reds_Cap_Insignia.svg.png",
    "CLE": "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3f/Cleveland_Guardians_cap_logo.svg/768px-Cleveland_Guardians_cap_logo.svg.png",
    "COL": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/2d/Colorado_Rockies_Cap_Insignia.svg/768px-Colorado_Rockies_Cap_Insignia.svg.png",
    "DET": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/94/Detroit_Tigers_Insignia.svg/480px-Detroit_Tigers_Insignia.svg.png",
    "HOU": "https://upload.wikimedia.org/wikipedia/commons/thumb/f/f6/Houston_Astros_cap_logo.svg/1024px-Houston_Astros_cap_logo.svg.png",
    "KC": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/88/Kansas_City_Royals_Insignia.svg/480px-Kansas_City_Royals_Insignia.svg.png",
    "LAA": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/8b/Los_Angeles_Angeles_of_Anaheim.svg/766px-Los_Angeles_Angeles_of_Anaheim.svg.png",
    "LAD": "https://upload.wikimedia.org/wikipedia/commons/thumb/f/f6/LA_Dodgers.svg/768px-LA_Dodgers.svg.png",
    "MIA": "https://upload.wikimedia.org/wikipedia/en/thumb/c/c3/Miami_Marlins_cap_insignia.svg/800px-Miami_Marlins_cap_insignia.svg.png",
    "MIL": "https://upload.wikimedia.org/wikipedia/en/thumb/2/28/Milwaukee_Brewers_cap_insignia.svg/480px-Milwaukee_Brewers_cap_insignia.svg.png",
    "MIN": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/2f/Minnesota_Twins_Insignia.svg/250px-Minnesota_Twins_Insignia.svg.png",
    "NYM": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/98/New_York_Mets_Insignia.svg/768px-New_York_Mets_Insignia.svg.png",
    "NYY": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/70/NewYorkYankees_caplogo.svg/480px-NewYorkYankees_caplogo.svg.png",
    "ATH": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/7c/Oakland_A%27s_cap_logo.svg/1226px-Oakland_A%27s_cap_logo.svg.png",
    "PHI": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a3/Philadelphia_Phillies_Insignia.svg/768px-Philadelphia_Phillies_Insignia.svg.png",
    "PIT": "https://upload.wikimedia.org/wikipedia/commons/c/ce/PittsburghPiratesCapLogo.png",
    "SD": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/cb/San_Diego_Padres_%282020%29_cap_logo.svg/768px-San_Diego_Padres_%282020%29_cap_logo.svg.png",
    "SF": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/49/San_Francisco_Giants_Cap_Insignia.svg/741px-San_Francisco_Giants_Cap_Insignia.svg.png",
    "SEA": "https://upload.wikimedia.org/wikipedia/en/thumb/8/8a/Seattle_Mariners_Insignia.svg/480px-Seattle_Mariners_Insignia.svg.png",
    "STL": "https://upload.wikimedia.org/wikipedia/commons/thumb/3/39/St._Louis_Cardinals_insignia_logo.svg/718px-St._Louis_Cardinals_insignia_logo.svg.png",
    "TB": "https://upload.wikimedia.org/wikipedia/commons/thumb/5/52/Tampa_Bay_Rays_cap_logo.svg/1095px-Tampa_Bay_Rays_cap_logo.svg.png",
    "TEX": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e2/Texas_Rangers_Insignia.svg/1024px-Texas_Rangers_Insignia.svg.png",
    "TOR": "https://upload.wikimedia.org/wikipedia/en/thumb/c/cc/Toronto_Blue_Jay_Primary_Logo.svg/891px-Toronto_Blue_Jay_Primary_Logo.svg.png",
    "WSH": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e5/Washington_Nationals_Cap_Insig.svg/768px-Washington_Nationals_Cap_Insig.svg.png",
}

# abbr -> flat background colour the insignia is composited onto.
BG_COLORS: dict[str, tuple[int, int, int]] = {
    "AZ": (167, 25, 48),
    "ATL": (19, 39, 79),
    "BAL": (223, 70, 1),
    "BOS": (189, 48, 57),
    "CHC": (14, 51, 134),
    "CWS": (39, 37, 31),
    "CIN": (198, 1, 31),
    "CLE": (0, 43, 92),
    "COL": (51, 0, 111),
    "DET": (12, 35, 64),
    "HOU": (0, 45, 98),
    "KC": (0, 70, 135),
    "LAA": (186, 0, 33),
    "LAD": (0, 90, 156),
    "MIA": (0, 0, 0),
    "MIL": (19, 41, 75),
    "MIN": (0, 43, 92),
    "NYM": (0, 45, 114),
    "NYY": (12, 35, 64),
    "ATH": (0, 56, 49),
    "PHI": (232, 24, 40),
    "PIT": (39, 37, 31),
    "SD": (47, 36, 29),
    "SF": (0, 0, 0),
    "SEA": (12, 44, 86),
    "STL": (196, 30, 58),
    "TB": (9, 44, 92),
    "TEX": (192, 17, 31),
    "TOR": (19, 74, 142),
    "WSH": (171, 0, 3),
}

_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "logos", "mlb")


def remove_small_symbols(img: Image.Image, area_threshold: int = 100) -> Image.Image:
    """Drop tiny isolated shapes (the (TM)/(R) trademark glyphs) from an RGBA logo.

    Otherwise the marks survive as a few stray pixels once the logo is shrunk to 20px.
    Works on the alpha silhouette: keep only contours whose area clears the floor.
    """
    cv_image = np.array(img)
    gray = cv2.cvtColor(cv_image, cv2.COLOR_RGBA2GRAY)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    mask = np.zeros_like(gray)
    for cnt in contours:
        if cv2.contourArea(cnt) > area_threshold:
            cv2.drawContours(mask, [cnt], -1, 255, thickness=cv2.FILLED)
    alpha = cv2.bitwise_and(cv_image[:, :, 3], mask)
    cv_image[:, :, 3] = alpha
    return Image.fromarray(cv_image)


def _fetch(url: str) -> Image.Image:
    response = requests.get(url, headers=_HEADERS, timeout=30)
    response.raise_for_status()
    return Image.open(BytesIO(response.content)).convert("RGBA")


def build_tile(abbr: str) -> Image.Image:
    """Fetch, de-trademark, shrink, and composite one team's tile onto its background."""
    insignia = remove_small_symbols(_fetch(LOGO_URLS[abbr]))
    insignia.thumbnail(_SIZE, Image.Resampling.LANCZOS)
    tile = Image.new("RGBA", _SIZE, BG_COLORS[abbr])
    offset = ((_SIZE[0] - insignia.width) // 2, (_SIZE[1] - insignia.height) // 2)
    tile.paste(insignia, offset, mask=insignia)
    return tile


def main() -> None:
    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    for abbr in LOGO_URLS:
        try:
            tile = build_tile(abbr)
            tile.save(os.path.join(_OUTPUT_DIR, f"{abbr.lower()}.png"))
            print(f"saved {abbr.lower()}.png")
        except Exception as exc:  # noqa: BLE001 - a one-off tool; report and continue
            print(f"FAILED {abbr}: {exc}")


if __name__ == "__main__":
    main()
