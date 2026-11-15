"""Machine-local settings from a git-ignored ``.env`` file, for secrets that never enter the repo."""

from __future__ import annotations

from pathlib import Path

__all__ = ["read_env_file"]


def read_env_file(path: Path) -> dict[str, str]:
    """The ``KEY=value`` pairs in ``path``; no file means no settings.

    Blank lines, ``#`` comments and an ``export`` prefix are skipped over, and a value may be quoted.
    """
    if not path.is_file():
        return {}
    settings: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip().removeprefix("export ").strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        settings[key.strip()] = value
    return settings
