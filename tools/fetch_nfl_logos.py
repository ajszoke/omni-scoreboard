"""Fetch and cache NFL team logo SVGs, found through nflverse's team table.

nflverse's team table (CC BY 4.0) names each club's logo file on Wikipedia, through a PNG thumbnail
of the original SVG. Files move when a logo is updated, so the file's current address comes from
Wikipedia's API, by name, rather than from the table. The vector original renders crisply at any
size, so that is what gets cached:
``assets/raw/nfl/<abbr>/primary.svg``, with each source URL and sha256 recorded in a manifest. The
render step (``render_nfl_logo_variants``) then runs offline, and the provenance of every committed
tile can be audited. These are team trademarks cached for a friends-and-family appliance, not
redistributable clip art.

A one-off; uses only the stdlib. Run from the repo root.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root, for `omni`

from omni.domain.logos import LogoSource  # noqa: E402
from omni.providers.nfl_teams import NFL_TEAMS, canonical_abbreviation  # noqa: E402

_HEADERS = {"User-Agent": "omni-scoreboard-logo-cache/1.0 (personal LED scoreboard; one-off fetch)"}
_RAW_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "raw", "nfl")
_TEAM_TABLE = "https://github.com/nflverse/nflverse-data/releases/download/teams/teams_colors_logos.csv"
_THUMB = re.compile(r"^https://upload\.wikimedia\.org/wikipedia/[^/]+/thumb/[0-9a-f]/[0-9a-f]{2}/([^/]+\.svg)/[^/]+$")
_API = "https://en.wikipedia.org/w/api.php"
# Clubs whose logo file the table names no longer exists under that name, -> the current file.
_FILE_OVERRIDES = {
    "BUF": "Buffalo_Bills_logo_1974-present.svg",  # renamed on Commons
    "LAR": "LA_Rams_logo.svg",  # the 2020 "LA" with the horn
}
_PAUSE_S = 3.0  # Wikimedia throttles quick bursts; 32 files a few seconds apart is plenty
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


def _file_name(thumbnail: str) -> str:
    """The name of the SVG a Wikimedia thumbnail was rendered from."""
    match = _THUMB.match(thumbnail.strip())
    if match is None:
        raise ValueError(f"not a Wikimedia SVG thumbnail: {thumbnail}")
    return urllib.parse.unquote(match.group(1))


def _current_urls(names: list[str]) -> dict[str, str]:
    """Each file's current address, from one API query (Wikipedia resolves Commons files too)."""
    query = urllib.parse.urlencode(
        {
            "action": "query",
            "titles": "|".join(f"File:{name}" for name in names),
            "prop": "imageinfo",
            "iiprop": "url",
            "format": "json",
        }
    )
    answer = json.loads(_fetch(f"{_API}?{query}"))
    titles = {item["from"]: item["to"] for item in answer["query"].get("normalized", [])}
    urls = {
        page["title"]: page["imageinfo"][0]["url"] for page in answer["query"]["pages"].values() if "imageinfo" in page
    }
    found = {}
    for name in names:
        title = titles.get(f"File:{name}", f"File:{name}")
        if title in urls:
            found[name] = urls[title]
    return found


def main() -> None:
    table = _fetch(_TEAM_TABLE)
    rows = list(csv.DictReader(io.StringIO(table.decode())))
    links: dict[str, str] = {}
    for row in rows:
        try:
            abbreviation = canonical_abbreviation(row["team_abbr"])
        except KeyError:
            continue  # a relocated franchise's old name
        links.setdefault(abbreviation, row["team_logo_wikipedia"])
    missing = sorted(set(NFL_TEAMS) - set(links))
    if missing:
        raise SystemExit(f"no logo link for {', '.join(missing)}")
    names = {
        abbreviation: _FILE_OVERRIDES.get(abbreviation) or _file_name(link) for abbreviation, link in links.items()
    }
    urls = _current_urls(sorted(set(names.values())))
    unresolved = sorted(abbreviation for abbreviation, name in names.items() if name not in urls)
    if unresolved:
        raise SystemExit(f"Wikipedia has no current file for {', '.join(unresolved)}")
    manifest: dict[str, Any] = {
        "_team_table": {
            "url": _TEAM_TABLE,
            "sha256": hashlib.sha256(table).hexdigest(),
            "license": "CC BY 4.0 (nflverse); the logos themselves are team trademarks",
        }
    }
    for abbreviation in NFL_TEAMS:
        url = urls[names[abbreviation]]
        out_dir = os.path.join(_RAW_ROOT, abbreviation.lower())
        path = os.path.join(out_dir, "primary.svg")
        cached = os.path.exists(path)
        if cached:  # a rerun after a throttle keeps what it already has
            with open(path, "rb") as handle:
                data = handle.read()
        else:
            data = _fetch(url)
            os.makedirs(out_dir, exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
        manifest[abbreviation] = {
            "sources": {
                "primary.svg": {
                    "url": url,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "source": LogoSource.WIKIMEDIA.value,
                }
            }
        }
        print(f"{'kept' if cached else 'cached'} {abbreviation} ({len(data)} bytes)")
        if not cached:
            time.sleep(_PAUSE_S)
    with open(os.path.join(_RAW_ROOT, "manifest.json"), "w") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    print(f"wrote manifest for {len(NFL_TEAMS)} teams")


if __name__ == "__main__":
    main()
