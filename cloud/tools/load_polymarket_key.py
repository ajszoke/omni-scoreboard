"""Store the Polymarket key from a local ``.env`` in SSM, printing neither half.

    .venv/bin/python cloud/tools/load_polymarket_key.py [--env-file .env] [--profile omni] [--prefix PATH]

The values reach the AWS CLI on stdin, so they never appear in a process listing.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))  # so `omni` resolves when this script is run directly

from omni.core.env import read_env_file

_DEFAULT_PREFIX = "/omni/polymarket"
_FIELDS = {"POLYMARKET_KEY_ID": "key-id", "POLYMARKET_SECRET_KEY": "secret-key"}


def store_parameter(name: str, value: str, *, profile: str) -> subprocess.CompletedProcess[str]:
    """Store one encrypted parameter, passing its value on stdin."""
    command = ["aws", "ssm", "put-parameter", "--profile", profile, "--name", name]
    command += ["--type", "SecureString", "--overwrite", "--value", "file:///dev/stdin"]
    command += ["--query", "Version", "--output", "text"]
    return subprocess.run(command, input=value, capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=_ROOT / ".env")
    parser.add_argument("--profile", default="omni")
    parser.add_argument("--prefix", default=_DEFAULT_PREFIX)
    args = parser.parse_args()
    settings = read_env_file(args.env_file)
    missing = [name for name in _FIELDS if not settings.get(name)]
    if missing:
        print(f"not set in {args.env_file}: {', '.join(missing)}", file=sys.stderr)
        return 1
    for env_name, parameter in _FIELDS.items():
        name = f"{args.prefix}/{parameter}"
        result = store_parameter(name, settings[env_name], profile=args.profile)
        if result.returncode != 0:
            print(f"{name}: failed: {result.stderr.strip()}", file=sys.stderr)
            return result.returncode
        print(f"{name}: stored, version {result.stdout.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
