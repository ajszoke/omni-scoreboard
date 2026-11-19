"""BALLDONTLIE's player lines: paging, each row's side, the feed's order, lines the feed stops sending, short names,
and rows that can't be read."""

from __future__ import annotations

import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

import bdfparser
import pytest

from omni.core.clock import MonotonicTime
from omni.core.enum import HomeAway, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.basketball import GameClock, PlayerLine
from omni.domain.basketball_reads import LinesRead, PlayerKey
from omni.providers.balldontlie import (
    BALLDONTLIE_SOURCE,
    BalldontlieProvider,
    BdlAnswer,
    FetchOutcome,
    GameLines,
    LinesPoll,
    RowProblem,
    Unreadable,
    short_name,
)
from omni.providers.nba_teams import NbaTeamRegistry
from tests.balldontlie_feed import AWAY_ID, GAME, HOME_ID, RATE, ScriptedFetch, ok, slate_row, stat_row, team

DAY = date(2026, 11, 18)
REPO = Path(__file__).resolve().parents[1]
GAME_ID = LeagueScopedId(League.NBA, BALLDONTLIE_SOURCE, str(GAME))
OTHER = 21718137
OTHER_ID = LeagueScopedId(League.NBA, BALLDONTLIE_SOURCE, str(OTHER))


def _provider(*answers: BdlAnswer | Exception, slate: tuple[Any, ...] | None = None) -> BalldontlieProvider:
    """A provider that has read the slate (so it knows each game's sides), then answers ``answers``."""
    rows = slate if slate is not None else (slate_row(),)
    fetch = ScriptedFetch([ok(*rows), *answers])
    provider = BalldontlieProvider(NbaTeamRegistry.default(), fetch=fetch, clock=fetch.clock)
    assert provider.poll_slate(DAY).complete
    return provider


def _lines(poll: LinesPoll, game: LeagueScopedId = GAME_ID) -> LinesRead:
    (read,) = [each.read for each in poll.games if each.game == game]
    return read


def _names(read: LinesRead) -> list[str]:
    return [roster.line.name for roster in read.lines]


EDWARDS = stat_row(1, 101, "Keyonte", "George", AWAY_ID, pts=7, fgm=3, fga=5, fg3m=1, fg3a=2, ast=2, plus_minus=4)
REAVES = stat_row(2, 202, "Austin", "Reaves", HOME_ID, pts=9, fgm=3, fga=6, ftm=3, fta=4, oreb=1, dreb=2, pf=2)
MARKKANEN = stat_row(3, 303, "Lauri", "Markkanen", AWAY_ID, pts=2, ftm=2, fta=2, stl=1, blk=1, turnover=1)


def test_each_row_becomes_a_line_on_its_side_in_the_feeds_order() -> None:
    provider = _provider(ok(EDWARDS, REAVES, MARKKANEN))
    poll = provider.poll_lines([GAME_ID])
    assert poll.complete and len(poll.games) == 1 and poll.games[0].game == GAME_ID
    read = _lines(poll)
    assert (read.at, read.period, read.clock) == (MonotonicTime(2.0), 3, GameClock.of(6, 7))
    assert [(roster.key, roster.side) for roster in read.lines] == [
        (PlayerKey(101), HomeAway.AWAY),
        (PlayerKey(202), HomeAway.HOME),
        (PlayerKey(303), HomeAway.AWAY),
    ]
    assert read.lines[1].line == PlayerLine(
        name="A.Reaves",
        points=9,
        fg_made=3,
        fg_attempted=6,
        threes_made=0,
        threes_attempted=0,
        ft_made=3,
        ft_attempted=4,
        offensive_rebounds=1,
        defensive_rebounds=2,
        assists=0,
        steals=0,
        blocks=0,
        turnovers=0,
        fouls=2,
        plus_minus=0,
    )
    assert _names(read) == ["K.George", "A.Reaves", "L.Markkanen"]


def test_pages_are_joined_before_the_read() -> None:
    provider = _provider(ok(EDWARDS, cursor=1), ok(REAVES, MARKKANEN))
    poll = provider.poll_lines([GAME_ID])
    assert len(poll.exchanges) == 2 and [roster.key.value for roster in _lines(poll).lines] == [101, 202, 303]


def test_one_poll_asks_every_game_and_hands_on_a_read_for_each_with_rows() -> None:
    other = slate_row(game_id=OTHER, away=team(8, "DET"), home=team(19, "NOP"))
    other_row = stat_row(9, 909, "Cade", "Cunningham", 8, game_id=OTHER, period=2, time="Half")
    provider = _provider(ok(EDWARDS, other_row), ok(EDWARDS), slate=(slate_row(), other))
    poll = provider.poll_lines([OTHER_ID, GAME_ID, GAME_ID])
    assert [each.game for each in poll.games] == [OTHER_ID, GAME_ID]
    assert (_lines(poll, OTHER_ID).period, _lines(poll, OTHER_ID).clock) == (2, None)
    assert _lines(poll, OTHER_ID).lines[0].side is HomeAway.AWAY
    later = provider.poll_lines([OTHER_ID, GAME_ID])
    assert [each.game for each in later.games] == [GAME_ID]  # a game whose rows didn't come back has no read


def test_the_query_names_each_game_once_in_order() -> None:
    fetch = ScriptedFetch([ok(slate_row(), slate_row(game_id=OTHER)), ok()])
    provider = BalldontlieProvider(NbaTeamRegistry.default(), fetch=fetch, clock=fetch.clock)
    provider.poll_slate(DAY)
    assert provider.poll_lines({GAME_ID, OTHER_ID}).games == ()
    assert fetch.asked[-1] == (
        "/v1/stats",
        (("game_ids[]", str(OTHER)), ("game_ids[]", str(GAME)), ("per_page", "100")),
    )


def test_a_poll_names_its_games() -> None:
    provider = _provider()
    with pytest.raises(ValueError, match="at least one"):
        provider.poll_lines([])
    with pytest.raises(ValueError, match="BALLDONTLIE"):
        provider.poll_lines([LeagueScopedId(League.NBA, SourceRef("nba"), "1")])
    with pytest.raises(ValueError, match="BALLDONTLIE"):
        provider.poll_lines([LeagueScopedId(League.NFL, BALLDONTLIE_SOURCE, "1")])
    with pytest.raises(ValueError, match="BALLDONTLIE"):
        provider.poll_lines([LeagueScopedId(League.NBA, BALLDONTLIE_SOURCE, "tonight")])


@pytest.mark.parametrize(
    ("period", "time", "clock"),
    [
        (3, "Q3 :48.2", GameClock.of(0, 48, 2)),
        (1, "END Q1", None),
        (2, "Half", None),
        (4, "Final", None),
        (5, "OT1 3:10", GameClock.of(3, 10)),
        (3, None, None),
    ],
)
def test_a_read_takes_the_stat_rows_own_period_and_clock(period: int, time: Any, clock: GameClock | None) -> None:
    read = _lines(_provider(ok(stat_row(1, 101, "A", "B", period=period, time=time))).poll_lines([GAME_ID]))
    assert (read.period, read.clock) == (period, clock)


def test_the_newest_page_gives_the_read_its_view() -> None:
    early = stat_row(1, 101, "A", "B", period=3, time="Q3 6:07")
    late = stat_row(2, 202, "C", "D", HOME_ID, period=3, time="Q3 6:01")
    read = _lines(_provider(ok(early, cursor=1), ok(late)).poll_lines([GAME_ID]))
    assert read.clock == GameClock.of(6, 1)


def test_a_player_the_feed_stops_sending_keeps_their_last_line_in_their_place() -> None:
    provider = _provider(
        ok(EDWARDS, REAVES, MARKKANEN),
        ok(EDWARDS, stat_row(3, 303, "Lauri", "Markkanen", AWAY_ID, pts=5, fgm=1, fga=1, fg3m=1, fg3a=1, ftm=2, fta=2)),
    )
    first = _lines(provider.poll_lines([GAME_ID]))
    second = _lines(provider.poll_lines([GAME_ID]))
    assert [roster.key.value for roster in second.lines] == [101, 202, 303]
    assert second.lines[1] == first.lines[1]  # Reaves, unsent, as he was
    assert second.lines[2].line.points == 5


def test_a_player_who_appears_later_takes_their_place_by_the_feeds_order() -> None:
    newcomer = stat_row(4, 404, "Walker", "Kessler", HOME_ID)
    provider = _provider(ok(EDWARDS, MARKKANEN), ok(REAVES, newcomer))
    provider.poll_lines([GAME_ID])
    read = _lines(provider.poll_lines([GAME_ID]))
    assert [roster.key.value for roster in read.lines] == [101, 202, 303, 404]


def test_the_pages_own_order_stands_and_kept_lines_fall_in_by_the_feeds_order() -> None:
    provider = _provider(ok(EDWARDS, REAVES, MARKKANEN), ok(MARKKANEN, EDWARDS))
    provider.poll_lines([GAME_ID])
    read = _lines(provider.poll_lines([GAME_ID]))
    assert [roster.key.value for roster in read.lines] == [202, 303, 101]


def test_a_player_sent_twice_keeps_the_later_row_in_the_first_place() -> None:
    again = stat_row(1, 101, "Keyonte", "George", AWAY_ID, pts=10, fgm=4, fga=6, fg3m=2, fg3a=3)
    read = _lines(_provider(ok(EDWARDS, REAVES, cursor=2), ok(again)).poll_lines([GAME_ID]))
    assert [roster.key.value for roster in read.lines] == [101, 202] and read.lines[0].line.points == 10


def test_a_later_slate_poll_keeps_what_the_lines_have_seen() -> None:
    fetch = ScriptedFetch([ok(slate_row()), ok(EDWARDS), ok(slate_row(time="Q3 5:00")), ok(REAVES)])
    provider = BalldontlieProvider(NbaTeamRegistry.default(), fetch=fetch, clock=fetch.clock)
    provider.poll_slate(DAY)
    provider.poll_lines([GAME_ID])
    provider.poll_slate(DAY)
    assert [roster.key.value for roster in _lines(provider.poll_lines([GAME_ID])).lines] == [101, 202]


# --- rows that can't be read ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad", "problem"),
    [
        (stat_row(2, 202, "Austin", "Reaves", HOME_ID, pts=11, fgm=3, fga=6, ftm=3, fta=4), RowProblem.INVALID_LINE),
        (stat_row(2, 202, "Austin", "Reaves", HOME_ID, pts=6, fgm=3, fga=2), RowProblem.INVALID_LINE),
        (stat_row(2, 202, "Austin", "Reaves", HOME_ID, pf=7), RowProblem.INVALID_LINE),
        (stat_row(2, 202, "Austin", "Reaves", HOME_ID, pts=None), RowProblem.MALFORMED),
        (stat_row(2, 202, "Austin", "Reaves", HOME_ID, plus_minus="+3"), RowProblem.MALFORMED),
        (stat_row(2, 202, "Austin", "Reaves", 99), RowProblem.WRONG_TEAM),
        (stat_row(2, 202, "", "", HOME_ID), RowProblem.MALFORMED),
        (stat_row(2, 202, "Austin", None, HOME_ID), RowProblem.MALFORMED),
        (stat_row(None, 202, "Austin", "Reaves", HOME_ID), RowProblem.MALFORMED),
        ({**stat_row(2, 202, "Austin", "Reaves", HOME_ID), "team": None}, RowProblem.MALFORMED),
    ],
)
def test_a_bad_row_is_reported_and_its_player_keeps_their_last_line(bad: dict[str, Any], problem: RowProblem) -> None:
    provider = _provider(ok(EDWARDS, REAVES), ok(EDWARDS, bad))
    first = _lines(provider.poll_lines([GAME_ID]))
    poll = provider.poll_lines([GAME_ID])
    (unreadable,) = poll.unreadable
    assert (unreadable.problem, unreadable.game, unreadable.player) == (problem, GAME_ID, PlayerKey(202))
    assert unreadable.line.startswith(f"{problem}: game {GAME} player 202: ")
    assert poll.answered and not poll.complete
    assert _lines(poll).lines == first.lines


def test_a_player_whose_first_row_is_bad_waits_for_a_good_one() -> None:
    bad = stat_row(2, 202, "Austin", "Reaves", HOME_ID, pts=1)
    provider = _provider(ok(EDWARDS, bad), ok(EDWARDS, REAVES))
    assert _names(_lines(provider.poll_lines([GAME_ID]))) == ["K.George"]
    assert _names(_lines(provider.poll_lines([GAME_ID]))) == ["K.George", "A.Reaves"]


@pytest.mark.parametrize(
    "bad",
    [
        {**EDWARDS, "player": None},
        {**EDWARDS, "player": {"id": 0, "first_name": "A", "last_name": "B"}},
        {**EDWARDS, "player": {"id": "101", "first_name": "A", "last_name": "B"}},
    ],
)
def test_a_row_with_no_player_names_none(bad: dict[str, Any]) -> None:
    poll = _provider(ok(bad, REAVES)).poll_lines([GAME_ID])
    (unreadable,) = poll.unreadable
    assert (unreadable.problem, unreadable.game, unreadable.player) == (RowProblem.MALFORMED, GAME_ID, None)
    assert [roster.key.value for roster in _lines(poll).lines] == [202]


def test_a_row_for_another_game_or_none_is_set_aside() -> None:
    stray = stat_row(5, 505, "A", "B", game_id=OTHER)
    poll = _provider(ok(EDWARDS, stray, "not a row", {**REAVES, "game": {"id": "x"}})).poll_lines([GAME_ID])
    assert [(each.problem, each.game) for each in poll.unreadable] == [
        (RowProblem.STRAY, OTHER_ID),
        (RowProblem.MALFORMED, None),
        (RowProblem.MALFORMED, None),
    ]
    assert _names(_lines(poll)) == ["K.George"]


def test_lines_for_a_game_no_slate_has_named_cant_be_sided() -> None:
    fetch = ScriptedFetch([ok(EDWARDS)])
    provider = BalldontlieProvider(NbaTeamRegistry.default(), fetch=fetch, clock=fetch.clock)
    poll = provider.poll_lines([GAME_ID])
    assert poll.games == ()
    (unreadable,) = poll.unreadable
    assert (unreadable.problem, unreadable.game) == (RowProblem.NOT_ON_SLATE, GAME_ID)


@pytest.mark.parametrize(
    ("game", "problem"),
    [
        ({"id": GAME, "period": 0, "time": None}, RowProblem.IMPOSSIBLE),
        ({"id": GAME, "period": 3, "time": "soon"}, RowProblem.UNKNOWN_TIME),
        ({"id": GAME, "period": None, "time": "Q3 1:00"}, RowProblem.MALFORMED),
    ],
)
def test_a_game_whose_rows_give_no_view_of_it_has_no_read(game: dict[str, Any], problem: RowProblem) -> None:
    provider = _provider(ok(EDWARDS), ok({**REAVES, "game": game}), ok(MARKKANEN))
    provider.poll_lines([GAME_ID])
    poll = provider.poll_lines([GAME_ID])
    assert poll.games == ()
    (unreadable,) = poll.unreadable
    assert (unreadable.problem, unreadable.game) == (problem, GAME_ID)
    assert _names(_lines(provider.poll_lines([GAME_ID]))) == ["K.George", "L.Markkanen"]  # nothing of it was kept


def test_a_failed_poll_hands_on_nothing_and_changes_nothing() -> None:
    provider = _provider(ok(EDWARDS), ok(REAVES, cursor=9), BdlAnswer(429, "", RATE), ok(MARKKANEN))
    provider.poll_lines([GAME_ID])
    failed = provider.poll_lines([GAME_ID])
    assert (failed.outcome, failed.games, len(failed.exchanges)) == (FetchOutcome.RATE_LIMITED, (), 2)
    assert [roster.key.value for roster in _lines(provider.poll_lines([GAME_ID])).lines] == [101, 303]


def test_game_lines_carry_their_game() -> None:
    read = _lines(_provider(ok(EDWARDS)).poll_lines([GAME_ID]))
    assert GameLines(game=GAME_ID, read=read).read is read
    assert Unreadable(problem=RowProblem.MALFORMED, detail="x", player=PlayerKey(4)).line == "malformed: player 4: x"


# --- short names ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "last", "name"),
    [
        ("Anthony", "Edwards", "A.Edwards"),
        ("Jaime", "Jaquez Jr.", "J.Jaquez"),
        ("Jaime", "Jaquez Jr", "J.Jaquez"),
        ("Jaime", "Jaquez, Jr.", "J.Jaquez"),
        ("Ronald", "Holland II", "R.Holland"),
        ("Trey", "Murphy III", "T.Murphy"),
        ("Wendell", "Carter Sr.", "W.Carter"),
        ("Moe", "Smith IV", "M.Smith"),
        ("Joe", "Doe V", "J.Doe"),
        ("Tre", "V", "T.V"),  # a one-word last name is never a suffix
        ("Trayce", "Jackson-Davis", "T.Jackson-Davis"),
        ("Tristan", "da Silva", "T.da Silva"),
        ("Kasparas", "Jakučionis", "K.Jakučionis"),
        ("Luka", "Dončić", "L.Dončić"),
        ("Chris", "Mañon", "C.Mañon"),
        ("Álvaro", "Núñez", "Á.Núñez"),
        ("T.J.", "McConnell", "T.McConnell"),
        ("AJ", "Johnson", "A.Johnson"),
        ("Jae'Sean", "Tate", "J.Tate"),
        ("Royce", "O’Neale", "R.O'Neale"),
        ("'Bo", "Cruz", "B.Cruz"),
        ("Mo", "  Bamba  ", "M.Bamba"),
        ("", "Nene", "Nene"),
        ("Nene", "", "Nene"),
        ("Ștefan", "Pașcu", "S.Pascu"),  # comma-below letters fall outside the fonts: folded
        ("Ǆemal", "Bajramović", "D.Bajramović"),
        ("Γιάννης", "Smith", "Smith"),  # a letter with no ASCII form is left out
        ("", "", ""),
    ],
)
def test_the_boards_short_name(first: str, last: str, name: str) -> None:
    assert short_name(first, last) == name


def test_every_letter_a_short_name_keeps_is_one_the_name_fonts_draw() -> None:
    fonts = [bdfparser.Font(str(REPO / "assets" / "fonts" / "patched" / f"{name}.bdf")) for name in ("4x6", "5x7")]
    kept = set()
    for code in range(0x20, 0x3000):
        if unicodedata.category(chr(code)).startswith(("L", "P", "Z")):
            kept |= set(short_name("", f"X{chr(code)}"))
    missing = sorted(char for char in kept for font in fonts if ord(char) not in font.glyphs)
    assert missing == []
    assert {"č", "ć", "ñ", "Á", "ł", "ő"} <= kept
