"""Fetch and cache MLB team logo SVGs from MLB's static CDN.

MLBStatic serves edit-quality, team-id-keyed vector cap marks in two surface
variants — ``team-cap-on-light`` (a dark mark, for light backgrounds) and
``team-cap-on-dark`` (a light mark, for dark backgrounds) — plus a primary logo.
Picking the surface that matches the target background is what lets the renderer
recolour *nothing*: the official mark already comes in the polarity it needs.

This caches the raw SVGs under ``assets/raw/mlb/<team_id>/`` and records each
source URL + sha256 in a manifest, so the render step (``render_mlb_logo_variants``)
runs offline and the provenance of every committed tile is auditable. These are team
trademarks cached for a friends-and-family appliance, not redistributable clip art.

A one-off; uses only the stdlib. Run from the repo root.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root, for `omni`

from omni.domain.logos import LogoSource  # noqa: E402
from omni.providers.mlb_teams import _TEAM_ID_INFO  # noqa: E402

_HEADERS = {"User-Agent": "Mozilla/5.0 (omni-scoreboard logo cache)"}
_RAW_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "raw", "mlb")
_CDN = "https://www.mlbstatic.com/team-logos"
# local filename -> CDN path template (by team id)
_VARIANTS = {
    "cap_on_light.svg": "team-cap-on-light/{}.svg",
    "cap_on_dark.svg": "team-cap-on-dark/{}.svg",
    "primary.svg": "{}.svg",
}
# Per-team extra sources (full URLs) where the CDN variant is wrong for our use.
# HOU: every mlbstatic variant is the roundel; the bare star-H cap lives on Wikimedia.
# STL/BOS: alternate primary marks (the StL insignia, the hanging socks). The socks SVG
# uses Illustrator DOCTYPE entities the safe parser rejects, so both take Wikimedia's
# server-rendered PNG (which carries transparency) rather than the raw SVG.
_W = "https://upload.wikimedia.org/wikipedia"
_EXTRA_SOURCES: dict[int, dict[str, str]] = {
    117: {"cap_wikimedia.svg": f"{_W}/commons/f/f6/Houston_Astros_cap_logo.svg"},
    138: {
        "insignia.png": f"{_W}/commons/thumb/3/39/St._Louis_Cardinals_insignia_logo.svg/500px-St._Louis_Cardinals_insignia_logo.svg.png"
    },
    111: {"socks.png": f"{_W}/en/thumb/6/6d/RedSoxPrimary_HangingSocks.svg/500px-RedSoxPrimary_HangingSocks.svg.png"},
    121: {"insignia.png": f"{_W}/commons/thumb/9/98/New_York_Mets_Insignia.svg/500px-New_York_Mets_Insignia.svg.png"},
}


def _fetch(url: str) -> bytes:
    return bytes(urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=20).read())


def main() -> None:
    manifest: dict[str, dict[str, Any]] = {}
    for team_id, (abbr, _) in _TEAM_ID_INFO.items():
        out_dir = os.path.join(_RAW_ROOT, str(team_id))
        os.makedirs(out_dir, exist_ok=True)
        manifest[str(team_id)] = {"abbreviation": abbr, "sources": {}}
        sources = {fn: (f"{_CDN}/{tmpl.format(team_id)}", LogoSource.MLBSTATIC) for fn, tmpl in _VARIANTS.items()}
        sources.update({fn: (url, LogoSource.WIKIMEDIA) for fn, url in _EXTRA_SOURCES.get(team_id, {}).items()})
        for filename, (url, source) in sources.items():
            data = _fetch(url)
            with open(os.path.join(out_dir, filename), "wb") as handle:
                handle.write(data)
            manifest[str(team_id)]["sources"][filename] = {
                "url": url,
                "sha256": hashlib.sha256(data).hexdigest(),
                "source": source.value,
            }
        print(f"cached {abbr} ({team_id})")
    with open(os.path.join(_RAW_ROOT, "manifest.json"), "w") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    print(f"wrote manifest for {len(manifest)} teams")


if __name__ == "__main__":
    main()
