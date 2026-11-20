"""Records and standing ranks counted from a season's finals: the season read, the order and its tiebreak, the NBA Cup
final, a final folded in as it happens, and a club with no games."""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import Any

import pytest

from omni.core.enum import HomeAway, League
from omni.core.ids import LeagueScopedId
from omni.domain.baseball import WinLossRecord
from omni.domain.basketball import StandingRanks
from omni.providers.balldontlie import BALLDONTLIE_SOURCE, BalldontlieProvider, BdlAnswer, FetchOutcome, RowProblem
from omni.providers.nba_standings import GameResult, NbaStandings
from omni.providers.nba_teams import NBA_TEAMS, NbaTeamRegistry
from tests.balldontlie_feed import RATE, ScriptedFetch, final_row, ok, scheduled_row, slate_row, team

REGISTRY = NbaTeamRegistry.default()
_IDS = {abbreviation: number for number, abbreviation in enumerate(sorted(NBA_TEAMS), start=1)}


def _club(abbreviation: str) -> dict[str, Any]:
    return team(_IDS[abbreviation], abbreviation)


def _final(game_id: int, away: str, home: str, winner: HomeAway, **changes: Any) -> dict[str, Any]:
    scores = (101, 99) if winner is HomeAway.AWAY else (99, 101)
    quarters = (25, 25, 25, 26) if winner is HomeAway.AWAY else (25, 25, 25, 24)
    return final_row(
        game_id=game_id,
        away=_club(away),
        home=_club(home),
        scores=scores,
        away_periods=quarters,
        home_periods=(25, 25, 25, 24) if winner is HomeAway.AWAY else (25, 25, 25, 26),
        **changes,
    )


AWAY, HOME = HomeAway.AWAY, HomeAway.HOME
SEASON = [
    _final(1, "UTA", "LAL", HOME),
    _final(2, "GSW", "LAL", HOME),
    _final(3, "UTA", "GSW", HOME),
    _final(4, "GSW", "UTA", HOME),
    _final(5, "SAC", "UTA", HOME),
    _final(6, "PHX", "GSW", HOME),
    _final(7, "PHX", "SAC", AWAY, ist_stage="West Group B"),  # a Cup group game counts
    _final(8, "NYK", "LAL", HOME, ist_stage="Championship"),  # the Cup final doesn't
    _final(9, "BOS", "NYK", AWAY, postseason=True),  # nor does the postseason
    slate_row(game_id=10, away=_club("MIA"), home=_club("ORL")),  # under way: no result yet
    scheduled_row(game_id=11, away=_club("CHI"), home=_club("DET")),
]


def _provider(*answers: BdlAnswer | Exception) -> tuple[BalldontlieProvider, ScriptedFetch]:
    fetch = ScriptedFetch(list(answers))
    return BalldontlieProvider(REGISTRY, fetch=fetch, clock=fetch.clock), fetch


def _standings() -> NbaStandings:
    provider, _ = _provider(ok(*SEASON[:6], cursor=6), ok(*SEASON[6:]))
    read = provider.read_season(2026)
    standings = NbaStandings(REGISTRY)
    standings.replace(read.results)
    return standings


def _record(standings: NbaStandings, abbreviation: str) -> str:
    return str(standings.record(REGISTRY.resolve(abbreviation)))


def _ranks(standings: NbaStandings, abbreviation: str) -> tuple[int, int]:
    ranks = standings.ranks(REGISTRY.resolve(abbreviation))
    return ranks.conference, ranks.division


def test_a_season_read_asks_for_the_regular_season_every_page() -> None:
    provider, fetch = _provider(ok(*SEASON[:6], cursor=6), ok(*SEASON[6:]))
    read = provider.read_season(2026)
    assert read.complete and len(read.exchanges) == 2
    assert fetch.asked[0] == ("/v1/games", (("seasons[]", "2026"), ("postseason", "false"), ("per_page", "100")))
    assert fetch.asked[1][1][-1] == ("cursor", "6")
    assert [result.game.raw for result in read.results] == ["1", "2", "3", "4", "5", "6", "7"]
    first = read.results[0]
    assert first == GameResult(
        game=LeagueScopedId(League.NBA, BALLDONTLIE_SOURCE, "1"),
        away=REGISTRY.resolve("UTA"),
        home=REGISTRY.resolve("LAL"),
        winner=HomeAway.HOME,
    )


def test_a_season_read_reports_the_rows_it_cant_read() -> None:
    provider, _ = _provider(ok(SEASON[0], slate_row(game_id=12, state="unknown")))
    read = provider.read_season(2026)
    assert len(read.results) == 1 and [each.problem for each in read.unreadable] == [RowProblem.UNKNOWN_STATUS]
    assert read.answered and not read.complete


def test_a_failed_season_read_counts_nothing() -> None:
    provider, _ = _provider(ok(*SEASON[:6], cursor=6), BdlAnswer(429, "", RATE))
    read = provider.read_season(2026)
    assert (read.outcome, read.results) == (FetchOutcome.RATE_LIMITED, ())


def test_a_season_read_a_page_at_a_time_asks_one_request_a_page_and_names_the_next() -> None:
    provider, fetch = _provider(ok(*SEASON[:6], cursor=6), ok(*SEASON[6:]))
    first = provider.read_season_page(2026)
    assert first.complete and first.cursor == "6" and len(first.exchanges) == 1
    assert [result.game.raw for result in first.results] == ["1", "2", "3", "4", "5", "6"]
    last = provider.read_season_page(2026, cursor=first.cursor)
    assert last.cursor is None and [result.game.raw for result in last.results] == ["7"]
    assert fetch.asked[1][1][-1] == ("cursor", "6")


def test_a_season_page_leaves_out_the_finals_filed_on_or_after_a_date() -> None:
    later = _final(13, "UTA", "LAL", AWAY, date="2026-11-19")
    provider, _ = _provider(ok(SEASON[0], later), ok(SEASON[0], later))
    assert [each.game.raw for each in provider.read_season_page(2026, before=date(2026, 11, 19)).results] == ["1"]
    assert [each.game.raw for each in provider.read_season_page(2026, before=date(2026, 11, 20)).results] == ["1", "13"]


def test_a_failed_season_page_counts_nothing_and_says_why() -> None:
    provider, _ = _provider(BdlAnswer(401, "", {}))
    page = provider.read_season_page(2026)
    assert (page.outcome, page.results, page.cursor, page.detail) == (FetchOutcome.UNAUTHORIZED, (), None, "HTTP 401")


def test_records_count_the_regular_season_finals() -> None:
    standings = _standings()
    records = {abbreviation: _record(standings, abbreviation) for abbreviation in ("LAL", "GSW", "UTA", "PHX", "SAC")}
    assert records == {"LAL": "2-0", "GSW": "2-2", "UTA": "2-2", "PHX": "1-1", "SAC": "0-2"}
    assert (_record(standings, "NYK"), _record(standings, "BOS")) == ("0-0", "0-0")  # the Cup final, the postseason


def test_ranks_go_by_win_percentage_then_wins_then_losses_then_the_abbreviation() -> None:
    standings = _standings()
    west = sorted(
        (abbreviation for abbreviation, club in NBA_TEAMS.items() if club[2] == "West"),
        key=lambda abbreviation: _ranks(standings, abbreviation)[0],
    )
    # LAL 2-0; GSW and UTA 2-2, tied all the way to the abbreviation; PHX 1-1, a .500 with fewer wins; the clubs
    # with no games, by abbreviation; SAC 0-2, a .000 with more losses.
    assert west == "LAL GSW UTA PHX DAL DEN HOU LAC MEM MIN NOP OKC POR SAS SAC".split()
    assert [_ranks(standings, each)[1] for each in ("LAL", "GSW", "PHX", "LAC", "SAC")] == [1, 2, 3, 4, 5]
    assert [_ranks(standings, each)[1] for each in ("UTA", "DEN", "MIN", "OKC", "POR")] == [1, 2, 3, 4, 5]
    assert standings.ranks(REGISTRY.resolve("UTA")) == StandingRanks(conference=3, division=1)


def test_a_club_with_no_games_is_0_0_and_ranks_among_its_equals() -> None:
    standings = NbaStandings(REGISTRY)
    assert standings.record(REGISTRY.resolve("CHA")) == WinLossRecord(wins=0, losses=0)
    assert _ranks(standings, "ATL") == (1, 1) and _ranks(standings, "WAS") == (15, 5)
    assert _ranks(_standings(), "ATL") == (1, 1)


def test_a_final_folds_in_as_it_happens_without_a_reread() -> None:
    standings = _standings()
    provider, _ = _provider(ok(_final(20, "LAL", "PHX", AWAY)))
    (game,) = provider.poll_slate(date(2026, 11, 18)).games
    assert game.result is not None
    assert standings.fold(game.result) and _record(standings, "LAL") == "3-0" and _record(standings, "PHX") == "1-2"
    assert not standings.fold(game.result) and _record(standings, "LAL") == "3-0"
    standings.replace(_standings_results())  # a full read made before it went final: it stays counted
    assert _record(standings, "LAL") == "3-0"
    standings.replace((*_standings_results(), game.result))  # the next day's read has it: counted once
    assert _record(standings, "LAL") == "3-0"
    assert not standings.fold(game.result)
    standings.replace(_standings_results())  # once a read has had it, it's the read's to count
    assert _record(standings, "LAL") == "2-0"


def _standings_results() -> tuple[GameResult, ...]:
    provider, _ = _provider(ok(*SEASON))
    return provider.read_season(2026).results


def test_a_game_counts_once_it_is_a_regular_season_final() -> None:
    provider, _ = _provider(ok(*SEASON))
    games = provider.poll_slate(date(2026, 11, 18)).games
    counted = {game.game.id.raw for game in games if game.result is not None}
    assert counted == {"1", "2", "3", "4", "5", "6", "7"}


def test_a_result_is_between_two_nba_clubs() -> None:
    lal, uta = REGISTRY.resolve("LAL"), REGISTRY.resolve("UTA")
    game = LeagueScopedId(League.NBA, BALLDONTLIE_SOURCE, "1")
    with pytest.raises(ValueError, match="two clubs"):
        GameResult(game=game, away=lal, home=lal, winner=HomeAway.HOME)
    stranger = dataclasses.replace(lal, abbreviation="SEA")
    standings = NbaStandings(REGISTRY)
    with pytest.raises(ValueError, match="SEA"):
        standings.fold(GameResult(game=game, away=stranger, home=uta, winner=HomeAway.HOME))
    with pytest.raises(ValueError, match="SEA"):
        standings.replace([GameResult(game=game, away=uta, home=stranger, winner=HomeAway.HOME)])
    with pytest.raises(ValueError, match="SEA"):
        standings.record(stranger)


def test_ranking_needs_each_clubs_conference_and_division() -> None:
    clubs = {abbreviation: REGISTRY.resolve(abbreviation) for abbreviation in NBA_TEAMS}
    clubs["UTA"] = dataclasses.replace(clubs["UTA"], division=None)
    with pytest.raises(ValueError, match="UTA"):
        NbaStandings(NbaTeamRegistry(clubs))
