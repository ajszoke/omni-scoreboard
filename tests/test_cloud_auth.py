"""Board tokens: what gets a request in, and what never leaves the board."""

from __future__ import annotations

import pytest

from cloud.market_service.auth import authenticated_board, issue_credential, token_digest


def test_an_issued_token_proves_its_board_and_only_its_hash_needs_storing() -> None:
    credential = issue_credential("den-quad")
    assert credential.token.startswith("den-quad.")
    secret = credential.token.split(".", 1)[1]
    assert credential.digest == token_digest(secret) and secret not in credential.digest
    stored = {"den-quad": credential.digest}
    assert authenticated_board(f"Bearer {credential.token}", stored.get) == "den-quad"


def test_two_issues_never_share_a_token() -> None:
    assert issue_credential("a").token != issue_credential("a").token


def test_a_credentials_repr_keeps_the_token_out_of_any_log() -> None:
    credential = issue_credential("den")
    assert credential.token not in repr(credential)
    assert credential.digest not in repr(credential)


@pytest.mark.parametrize("board_id", ["", "UPPER", "has.dot", "has space", "x" * 33])
def test_a_board_id_that_couldnt_be_read_back_from_a_token_is_refused(board_id: str) -> None:
    with pytest.raises(ValueError):
        issue_credential(board_id)


def test_a_wrong_secret_or_a_revoked_board_is_turned_away() -> None:
    credential = issue_credential("den")
    assert authenticated_board("Bearer den.wrong", {"den": credential.digest}.get) is None
    revoked: dict[str, str] = {}
    assert authenticated_board(f"Bearer {credential.token}", revoked.get) is None


def test_another_boards_secret_doesnt_work_under_this_boards_name() -> None:
    den, reno = issue_credential("den"), issue_credential("reno")
    forged = f"Bearer reno.{den.token.split('.', 1)[1]}"
    assert authenticated_board(forged, {"den": den.digest, "reno": reno.digest}.get) is None


@pytest.mark.parametrize(
    "header",
    [None, "", "Basic abc", "Bearer ", "Bearer nosecret", "Bearer den.", "Bearer .secret", "Bearer BAD ID.secret"],
)
def test_a_malformed_header_never_reaches_the_table(header: str | None) -> None:
    def lookup(_board: str) -> str | None:
        raise AssertionError("should not read the table")

    assert authenticated_board(header, lookup) is None


def test_the_scheme_is_case_insensitive() -> None:
    credential = issue_credential("den")
    assert authenticated_board(f"bearer {credential.token}", {"den": credential.digest}.get) == "den"
