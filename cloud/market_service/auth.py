"""Per-board bearer tokens: boards hold a token, and the table holds only its hash.

A token names its board, so a request can be checked with one read, and deleting that board's row
revokes it at once. Tokens are long random strings, so a plain hash is enough to keep them safe at rest.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["BoardCredential", "authenticated_board", "issue_credential", "token_digest"]

_BOARD_ID = re.compile(r"^[a-z0-9-]{1,32}$")
_BEARER = "bearer "
_SEPARATOR = "."


@dataclass(frozen=True, slots=True)
class BoardCredential:
    """A freshly issued token and the hash to store for it; the token stays out of ``repr``."""

    board_id: str
    token: str
    digest: str

    def __repr__(self) -> str:
        return f"BoardCredential(board_id={self.board_id!r})"


def token_digest(secret: str) -> str:
    """What the table stores in place of a token's secret part."""
    return hashlib.sha256(secret.encode()).hexdigest()


def issue_credential(board_id: str) -> BoardCredential:
    """A new token for a board. Raises `ValueError` for a name that couldn't be sent back in a token."""
    if not _BOARD_ID.match(board_id):
        raise ValueError("a board id is 1-32 lowercase letters, digits or dashes")
    secret = secrets.token_urlsafe(32)
    return BoardCredential(board_id=board_id, token=f"{board_id}{_SEPARATOR}{secret}", digest=token_digest(secret))


def authenticated_board(authorization: str | None, lookup: Callable[[str], str | None]) -> str | None:
    """The board an ``Authorization`` header proves itself to be, or None.

    ``lookup`` gives a board's stored hash. A malformed header is turned away without reading the table.
    """
    if authorization is None or not authorization.lower().startswith(_BEARER):
        return None
    board_id, separator, secret = authorization[len(_BEARER) :].strip().partition(_SEPARATOR)
    if not separator or not secret or not _BOARD_ID.match(board_id):
        return None
    stored = lookup(board_id)
    if stored is None or not hmac.compare_digest(stored, token_digest(secret)):
        return None
    return board_id
