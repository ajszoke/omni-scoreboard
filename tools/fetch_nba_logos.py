"""Fetch and cache NBA team logos from the NBA's own logo CDN.

The CDN serves each club's primary mark as SVG, keyed by the NBA's team id, in two surface variants:
``primary/L`` for light backgrounds and ``primary/D`` for dark ones. These are the clean marks the
league's own scoreboards show, without the wordmarks and lettered rings of the full logos. Boston is the
exception: the CDN's mark is the full leprechaun, which turns to noise at tile size, so Boston's two
variants are ESPN's scoreboard PNGs of the Celtics' shamrock roundel. Charlotte shows an alternate mark
the user picked by hand, one raster file for both of its tiles.

Each variant is cached under ``assets/raw/nba/<abbr>/`` as ``primary_on_light`` and
``primary_on_dark`` (a hand-picked mark as ``alternate.png``), with its source URL and sha256 recorded
in a manifest. A raster that isn't a PNG is converted to one as it is cached, since that is the raster
form the render step reads; the manifest then records the sha256 of both the bytes fetched and the PNG
kept. The manifest also pins the
color table the palette starts from, Wikipedia's ``Module:Sports color/basketball`` (CC BY-SA 4.0), at
the revision first read; a rerun keeps that pin. The render step (``render_nba_logo_variants``) then
runs offline, and the provenance of every committed tile can be audited. These are team trademarks
cached for a friends-and-family appliance, not redistributable clip art.

A one-off; uses the stdlib, plus Pillow to convert a hand-picked raster. Run from the repo root.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root, for `omni`

from PIL import Image  # noqa: E402

from omni.domain.logos import LogoSource, LogoSurface  # noqa: E402
from omni.providers.nba_teams import NBA_TEAMS  # noqa: E402

# A bare product token: the NBA's CDN stalls on a User-Agent that carries a comment.
_HEADERS = {"User-Agent": "omni-scoreboard-logo-cache/1.0"}
_RAW_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "raw", "nba")
_MANIFEST = os.path.join(_RAW_ROOT, "manifest.json")
_API = "https://en.wikipedia.org/w/api.php"
_COLOR_TABLE = "Module:Sports color/basketball"
_CDN = "https://cdn.nba.com/logos/nba/{team_id}/primary/{surface}/logo.svg"
_CDN_SURFACE = {LogoSurface.ON_LIGHT: "L", LogoSurface.ON_DARK: "D"}
_ESPN = {
    LogoSurface.ON_LIGHT: "https://a.espncdn.com/i/teamlogos/nba/500/scoreboard/{abbr}.png",
    LogoSurface.ON_DARK: "https://a.espncdn.com/i/teamlogos/nba/500-dark/scoreboard/{abbr}.png",
}
_FROM_ESPN = {"BOS"}  # the CDN's leprechaun is noise at tile size; ESPN's shamrock roundel reads
# abbreviation -> an alternate mark the user picked by hand, for both of the club's tiles.
_CURATED = {
    # The hornet in side profile, without the CDN mark's "CHARLOTTE HORNETS" bar.
    "CHA": "https://static.wikia.nocookie.net/sports-logos5931/images/9/97/Charlotte_hornets_2014-present_a.png"
    "/revision/latest?cb=20260523052842",
}
_ALTERNATE = "alternate.png"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# abbreviation -> the NBA's team id, which keys the CDN.
_NBA_TEAM_ID = {
    "ATL": 1610612737,
    "BOS": 1610612738,
    "CLE": 1610612739,
    "NOP": 1610612740,
    "CHI": 1610612741,
    "DAL": 1610612742,
    "DEN": 1610612743,
    "GSW": 1610612744,
    "HOU": 1610612745,
    "LAC": 1610612746,
    "LAL": 1610612747,
    "MIA": 1610612748,
    "MIL": 1610612749,
    "MIN": 1610612750,
    "BKN": 1610612751,
    "NYK": 1610612752,
    "ORL": 1610612753,
    "IND": 1610612754,
    "PHI": 1610612755,
    "PHX": 1610612756,
    "POR": 1610612757,
    "SAC": 1610612758,
    "SAS": 1610612759,
    "OKC": 1610612760,
    "TOR": 1610612761,
    "UTA": 1610612762,
    "MEM": 1610612763,
    "WAS": 1610612764,
    "DET": 1610612765,
    "CHA": 1610612766,
}
_PAUSE_S = 3.0  # a few seconds between files keeps a one-off fetch polite
_MAX_TRIES = 5
_MAX_WAIT_S = 60.0


def _fetch(url: str) -> bytes:
    """Read a URL, waiting out a throttle (429/503) as the server asks before trying again."""
    for attempt in range(_MAX_TRIES):
        try:
            return bytes(urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=20).read())
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 503) or attempt == _MAX_TRIES - 1:
                raise
            wait = min(float(exc.headers.get("Retry-After") or 10 * (attempt + 1)), _MAX_WAIT_S)
            print(f"  throttled ({exc.code}); waiting {wait:.0f}s")
            time.sleep(wait)
    raise AssertionError("unreachable")


def _color_table() -> dict[str, str]:
    """The palette's starting color table, pinned to the revision read."""
    query = urllib.parse.urlencode(
        {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "titles": _COLOR_TABLE,
            "prop": "revisions",
            "rvprop": "ids|content",
            "rvslots": "main",
        }
    )
    revision = json.loads(_fetch(f"{_API}?{query}"))["query"]["pages"][0]["revisions"][0]
    content = revision["slots"]["main"]["content"].encode()
    title = urllib.parse.quote(_COLOR_TABLE.replace(" ", "_"), safe=":/")
    return {
        "url": f"https://en.wikipedia.org/w/index.php?title={title}&oldid={revision['revid']}&action=raw",
        "sha256": hashlib.sha256(content).hexdigest(),
        "license": "CC BY-SA 4.0 (Wikipedia contributors); the logos themselves are team trademarks",
    }


def _sources(abbreviation: str) -> dict[str, tuple[str, LogoSource]]:
    """Each cached file name -> where it comes from: one per surface, or the one hand-picked mark."""
    if abbreviation in _CURATED:
        return {_ALTERNATE: (_CURATED[abbreviation], LogoSource.CURATED)}
    if abbreviation in _FROM_ESPN:
        return {
            f"primary_{surface.value}.png": (_ESPN[surface].format(abbr=abbreviation.lower()), LogoSource.ESPN)
            for surface in LogoSurface
        }
    return {
        f"primary_{surface.value}.svg": (
            _CDN.format(team_id=_NBA_TEAM_ID[abbreviation], surface=_CDN_SURFACE[surface]),
            LogoSource.NBA_CDN,
        )
        for surface in LogoSurface
    }


def _as_png(data: bytes) -> bytes:
    """A raster in another format (a WebP served under a .png name, say) re-encoded as a PNG."""
    buffer = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGBA").save(buffer, format="PNG")
    return buffer.getvalue()


def main() -> None:
    previous: dict[str, Any] = {}
    if os.path.exists(_MANIFEST):
        with open(_MANIFEST) as handle:
            previous = json.load(handle)
    manifest: dict[str, Any] = {"_color_table": previous.get("_color_table") or _color_table()}
    for abbreviation in NBA_TEAMS:
        out_dir = os.path.join(_RAW_ROOT, abbreviation.lower())
        os.makedirs(out_dir, exist_ok=True)
        entries = {}
        for name, (url, source) in _sources(abbreviation).items():
            path = os.path.join(out_dir, name)
            cached = os.path.exists(path)
            entry: dict[str, str] = {"url": url, "source": source.value}
            if cached:  # a rerun after a throttle keeps what it already has
                with open(path, "rb") as handle:
                    data = handle.read()
                fetched = previous.get(abbreviation, {}).get("sources", {}).get(name, {}).get("fetched_sha256")
                if fetched:  # it was converted as it was cached
                    entry["fetched_sha256"] = fetched
            else:
                time.sleep(_PAUSE_S)
                data = _fetch(url)
                if name.endswith(".png") and not data.startswith(_PNG_SIGNATURE):
                    entry["fetched_sha256"] = hashlib.sha256(data).hexdigest()
                    data = _as_png(data)
                with open(path, "wb") as handle:
                    handle.write(data)
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            entries[name] = entry
            print(f"{'kept' if cached else 'cached'} {abbreviation} {name} ({len(data)} bytes)")
        manifest[abbreviation] = {"sources": entries}
    with open(_MANIFEST, "w") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    print(f"wrote manifest for {len(NBA_TEAMS)} teams")


if __name__ == "__main__":
    main()
