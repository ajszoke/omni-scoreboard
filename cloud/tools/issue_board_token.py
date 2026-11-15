"""Issue a board's API token and write it to that board's env file, printing no secret.

    .venv/bin/python cloud/tools/issue_board_token.py BOARD_ID [--env-file .env] [--profile omni]

Only the token's hash reaches the table. Issuing again for the same board replaces its token, and
deleting the board's row revokes it. The file is kept private to its owner.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))  # so `cloud` resolves when this script is run directly

from cloud.market_service.auth import issue_credential

_STACK = "omni-market"
URL_VAR = "OMNI_CLOUD_URL"
TOKEN_VAR = "OMNI_CLOUD_TOKEN"


def updated_env_text(existing: str, settings: dict[str, str]) -> str:
    """``existing`` with ``settings`` set: replaced where they already appear, and added otherwise."""
    remaining = dict(settings)
    lines = []
    for line in existing.splitlines():
        key = line.split("=", 1)[0].removeprefix("export ").strip()
        lines.append(f"{key}={remaining.pop(key)}" if key in remaining else line)
    lines.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(lines) + "\n"


def write_private(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``, readable only by its owner from the first byte."""
    temporary = path.with_name(f"{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    os.replace(temporary, path)


def _aws(profile: str, *command: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["aws", *command, "--profile", profile, "--output", "text"], input=stdin, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SystemExit(f"aws {command[0]} {command[1]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _stack_output(profile: str, key: str) -> str:
    query = f"Stacks[0].Outputs[?OutputKey=='{key}'].OutputValue | [0]"
    return _aws(profile, "cloudformation", "describe-stacks", "--stack-name", _STACK, "--query", query)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board_id")
    parser.add_argument("--env-file", type=Path, default=_ROOT / ".env")
    parser.add_argument("--profile", default="omni")
    args = parser.parse_args()
    credential = issue_credential(args.board_id)
    item = {
        "pk": {"S": f"board#{credential.board_id}"},
        "sk": {"N": "0"},
        "token_sha256": {"S": credential.digest},
    }
    table = _stack_output(args.profile, "TableName")
    _aws(
        args.profile,
        "dynamodb",
        "put-item",
        "--table-name",
        table,
        "--item",
        "file:///dev/stdin",
        stdin=json.dumps(item),
    )
    existing = args.env_file.read_text() if args.env_file.exists() else ""
    settings = {URL_VAR: _stack_output(args.profile, "ApiUrl"), TOKEN_VAR: credential.token}
    write_private(args.env_file, updated_env_text(existing, settings))
    print(f"issued a token for {credential.board_id}; wrote {URL_VAR} and {TOKEN_VAR} to {args.env_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
