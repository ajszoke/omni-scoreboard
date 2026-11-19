"""NBA cards for the basketball tests: the locked design's specimens restated as typed cards, and their builders.

Every value is synthetic; the specimens whose states were transcribed from a recorded slate are restated here as
plain fixtures. Each specimen names the moments it's drawn at, in seconds since its card went up: one moment for
a card that holds all turn, two for a live turn's halves (the first 6 s, then the second). A play queue's later
captions name the earlier ones that went up before them on the same turn.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from omni.cards.base import CardId, CardKind, CardPriority, DedupeKey, DisplayTiming, LayoutSupport, ScoreboardCard
from omni.cards.basketball import (
    BasketballGameCardPayload,
    BasketballPregameCardPayload,
    BestPlayers,
    BottomPanel,
    CaptionPanel,
    LineScorePanel,
    PlayerLinesPanel,
    QueuePlace,
    TeamBoxPanel,
)
from omni.core.enum import DisplayPriority, GameStatus, HomeAway, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import WinLossRecord, WinProbability
from omni.domain.basketball import (
    BasketballGameState,
    BasketballPhase,
    BasketballSide,
    BySide,
    CallChange,
    FavoriteLine,
    GameClock,
    PlayerCallChange,
    PlayerLine,
    ScoringRun,
    StandingRanks,
)
from omni.domain.contest import TeamGame
from omni.events.basketball import (
    BasketballCaption,
    CallReversed,
    ChallengeWon,
    FieldGoalMade,
    FreeThrows,
    PersonalFoul,
    ShotValue,
    TimeoutTaken,
)
from omni.providers.nba_teams import NbaTeamRegistry

SOURCE = SourceRef("balldontlie")
ZONE = ZoneInfo("America/Denver")
ALL_PROFILES = (PanelProfile.SINGLE_64X32, PanelProfile.STACK_64X64, PanelProfile.QUAD_128X64)
QUAD, STACK, SINGLE = PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64, PanelProfile.SINGLE_64X32
A, H = HomeAway.AWAY, HomeAway.HOME
LIVE = BasketballPhase.LIVE
T = datetime(2026, 11, 17, 2, 39, 42, tzinfo=timezone.utc)

_REGISTRY = NbaTeamRegistry.default(source=SOURCE)
_STATUS = {
    BasketballPhase.SCHEDULED: GameStatus.SCHEDULED,
    BasketballPhase.POSTPONED: GameStatus.POSTPONED,
    BasketballPhase.FINAL: GameStatus.FINAL,
}


def player(
    name: str,
    plus_minus: int,
    fgm: int,
    fga: int,
    tpm: int,
    tpa: int,
    ftm: int,
    fta: int,
    oreb: int,
    dreb: int,
    ast: int,
    stl: int,
    blk: int,
    tov: int,
    pf: int,
) -> PlayerLine:
    """A player's line in the stat rows' order, its points added up from the makes."""
    return PlayerLine(
        name=name,
        points=2 * fgm + tpm + ftm,
        fg_made=fgm,
        fg_attempted=fga,
        threes_made=tpm,
        threes_attempted=tpa,
        ft_made=ftm,
        ft_attempted=fta,
        offensive_rebounds=oreb,
        defensive_rebounds=dreb,
        assists=ast,
        steals=stl,
        blocks=blk,
        turnovers=tov,
        fouls=pf,
        plus_minus=plus_minus,
    )


def nba_game(away: str = "POR", home: str = "MIN", *, status: GameStatus = GameStatus.LIVE) -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.NBA, SOURCE, f"{away}-{home}"),
        league=League.NBA,
        status=status,
        scheduled_start=T,
        away=_REGISTRY.resolve(away),
        home=_REGISTRY.resolve(home),
    )


def game_card(
    payload: BasketballGameCardPayload, *, away: str = "POR", home: str = "MIN", game: TeamGame | None = None
) -> ScoreboardCard[BasketballGameCardPayload]:
    """A game card: a final's kind is FINAL, anything else's LIVE_GAME."""
    final = payload.state.phase is BasketballPhase.FINAL
    contest = (
        game if game is not None else nba_game(away, home, status=_STATUS.get(payload.state.phase, GameStatus.LIVE))
    )
    key = f"{contest.id}:{'final' if final else 'live'}"
    return ScoreboardCard(
        id=CardId(key),
        kind=CardKind.FINAL if final else CardKind.LIVE_GAME,
        contest=contest,
        timing=DisplayTiming(available_at=T, min_display=DurationSeconds(5), max_display=DurationSeconds(30)),
        priority=CardPriority(band=DisplayPriority.FAVORITE, score=50.0),
        layout_support=LayoutSupport(profiles=frozenset(ALL_PROFILES)),
        dedupe_key=DedupeKey(key),
        payload=payload,
    )


def pregame_card(
    payload: BasketballPregameCardPayload, *, away: str = "POR", home: str = "MIN"
) -> ScoreboardCard[BasketballPregameCardPayload]:
    contest = nba_game(away, home, status=_STATUS[payload.phase])
    key = f"{contest.id}:pregame"
    return ScoreboardCard(
        id=CardId(key),
        kind=CardKind.PREGAME,
        contest=contest,
        timing=DisplayTiming(available_at=T, min_display=DurationSeconds(5), max_display=DurationSeconds(30)),
        priority=CardPriority(band=DisplayPriority.FAVORITE, score=50.0),
        layout_support=LayoutSupport(profiles=frozenset(ALL_PROFILES)),
        dedupe_key=DedupeKey(key),
        payload=payload,
    )


def side(
    score: int, timeouts: int | None, bonus: bool, periods: tuple[int, ...]
) -> BasketballSide:  # the study's order
    return BasketballSide(score=score, periods=periods, timeouts=timeouts, in_bonus=bonus)


def live(
    away: BasketballSide,
    home: BasketballSide,
    period: int,
    clock: int | None,
    *,
    home_win: float | None,
    records: tuple[WinLossRecord | None, WinLossRecord | None] = (None, None),
    bottom: BottomPanel = LineScorePanel(),
    run: ScoringRun | None = None,
    timeout: HomeAway | None = None,
    phase: BasketballPhase = LIVE,
    break_ends_at: datetime | None = None,
) -> BasketballGameCardPayload:
    """A game card's payload from the study's terms: the market's reading for the home side, the clock in tenths."""
    state = BasketballGameState(
        phase=phase,
        period=period,
        away=away,
        home=home,
        clock=GameClock(clock) if clock is not None else None,
        timeout=timeout,
    )
    win = WinProbability(home=home_win, away=100.0 - home_win) if home_win is not None else None
    return BasketballGameCardPayload(
        state=state,
        records=BySide(*records),
        win_probability=win,
        bottom=bottom,
        run=run,
        break_ends_at=break_ends_at,
    )


def caption(event: BasketballCaption, place: tuple[int, int] | None = None) -> CaptionPanel:
    return CaptionPanel(caption=event, place=QueuePlace(*place) if place is not None else None)


# --- the specimens ------------------------------------------------------------------------------------------


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


_POR, _MIN = WinLossRecord(wins=6, losses=8), WinLossRecord(wins=9, losses=5)
_ORL, _BOS = WinLossRecord(wins=8, losses=6), WinLossRecord(wins=7, losses=7)
_DET, _GSW = WinLossRecord(wins=10, losses=4), WinLossRecord(wins=8, losses=7)
_DEN_LATE, _LAL_LATE = WinLossRecord(wins=41, losses=30), WinLossRecord(wins=44, losses=27)
_POR_MIN = (_POR, _MIN)

_P = player  # name, +/-, FGM, FGA, 3PM, 3PA, FTM, FTA, OREB, DREB, AST, STL, BLK, TOV, PF

# POR at MIN through Q3 7:18 (58-68).
POR_Q3 = (
    _P("T.Camara", 3, 3, 5, 2, 3, 0, 0, 1, 3, 1, 2, 1, 0, 2),
    _P("D.Avdija", -4, 6, 12, 2, 5, 2, 2, 1, 5, 4, 1, 0, 2, 2),
    _P("D.Clingan", -6, 3, 5, 0, 0, 0, 2, 3, 6, 1, 0, 2, 1, 3),
    _P("J.Grant", -8, 2, 7, 1, 3, 2, 2, 0, 2, 1, 0, 0, 1, 1),
    _P("S.Sharpe", -9, 5, 13, 2, 6, 2, 3, 0, 2, 1, 1, 0, 3, 2),
    _P("J.Holiday", -11, 3, 8, 1, 3, 0, 0, 1, 1, 5, 1, 0, 1, 2),
)
MIN_Q3 = (
    _P("A.Edwards", 15, 8, 15, 4, 9, 2, 2, 0, 4, 3, 1, 0, 2, 1),
    _P("R.Gobert", 12, 4, 5, 0, 0, 0, 1, 3, 7, 1, 0, 2, 1, 2),
    _P("J.Randle", 9, 4, 9, 1, 3, 3, 4, 1, 4, 4, 0, 0, 2, 3),
    _P("J.McDaniels", 7, 4, 7, 1, 2, 0, 0, 1, 2, 1, 2, 1, 0, 2),
    _P("N.Reid", 4, 4, 8, 2, 4, 1, 2, 1, 3, 2, 0, 1, 1, 1),
    _P("M.Conley", 3, 2, 4, 2, 3, 0, 0, 0, 1, 6, 1, 0, 0, 1),
)
# The final, 98-106. Plus-minus picks T.Camara (+1) for POR, not the top scorer, S.Sharpe (26).
POR_FINAL = (
    _P("D.Avdija", -6, 8, 13, 2, 5, 4, 5, 2, 7, 7, 1, 0, 3, 3),
    _P("S.Sharpe", -12, 9, 24, 4, 11, 4, 4, 0, 3, 2, 1, 0, 4, 2),
    _P("J.Holiday", -10, 6, 12, 3, 7, 0, 0, 0, 3, 6, 1, 0, 2, 2),
    _P("J.Grant", -9, 5, 11, 2, 6, 2, 2, 0, 4, 1, 0, 1, 1, 3),
    _P("D.Clingan", -2, 5, 7, 0, 0, 0, 2, 4, 8, 1, 0, 3, 1, 4),
    _P("T.Camara", 1, 4, 8, 3, 6, 0, 0, 1, 4, 2, 2, 1, 1, 3),
)
MIN_FINAL = (
    _P("A.Edwards", 11, 11, 20, 4, 9, 5, 6, 1, 6, 5, 2, 0, 3, 2),
    _P("R.Gobert", 14, 6, 7, 0, 0, 2, 3, 5, 10, 1, 1, 4, 1, 3),
    _P("J.Randle", 6, 6, 14, 1, 4, 5, 6, 1, 6, 5, 0, 0, 3, 3),
    _P("N.Reid", 4, 6, 11, 3, 6, 0, 0, 1, 4, 1, 1, 1, 1, 2),
    _P("J.McDaniels", 9, 5, 9, 2, 4, 0, 0, 1, 3, 2, 2, 1, 0, 3),
    _P("M.Conley", 8, 5, 9, 4, 7, 2, 2, 0, 2, 6, 1, 0, 1, 1),
)
# DET at GSW, 117-119 in double overtime.
DET_FINAL = (
    _P("C.Cunningham", -3, 12, 28, 3, 9, 7, 8, 1, 7, 11, 1, 0, 5, 3),
    _P("J.Duren", 2, 8, 10, 0, 0, 4, 6, 6, 11, 2, 1, 3, 2, 4),
    _P("A.Thompson", -1, 5, 9, 0, 1, 2, 4, 2, 5, 3, 3, 1, 1, 4),
    _P("T.Harris", -4, 6, 13, 2, 5, 2, 2, 1, 5, 2, 0, 0, 1, 2),
    _P("J.Ivey", -5, 7, 15, 2, 6, 2, 3, 0, 3, 3, 1, 0, 3, 3),
    _P("D.Robinson", 1, 6, 11, 5, 9, 0, 0, 0, 2, 1, 0, 0, 0, 2),
)
GSW_FINAL = (
    _P("S.Curry", 5, 13, 27, 7, 15, 5, 5, 0, 5, 6, 2, 0, 4, 2),
    _P("J.Butler", 7, 7, 13, 0, 1, 9, 11, 2, 6, 7, 3, 0, 2, 3),
    _P("D.Green", 4, 4, 8, 1, 4, 0, 0, 1, 8, 9, 2, 1, 3, 5),
    _P("B.Podziemski", -2, 5, 11, 2, 5, 2, 2, 1, 5, 3, 1, 0, 1, 2),
    _P("J.Kuminga", -1, 7, 14, 1, 3, 2, 4, 2, 4, 1, 0, 1, 2, 3),
    _P("M.Moody", 3, 6, 10, 4, 7, 2, 2, 0, 3, 1, 1, 0, 0, 2),
)


@dataclass(frozen=True)
class Specimen:
    """One card of the locked design: its file key, the card, the render clock, and the moments it's drawn at.

    ``moments`` are seconds since the card went up, one per frame: a lone frame is named ``key``, a pair
    ``key-a`` then ``key-b``. ``earlier`` are cards that went up before this one on the same turn, each with
    the second it went up; this card went up at ``up_at``.
    """

    key: str
    card: ScoreboardCard[Any]
    now: datetime
    profiles: frozenset[PanelProfile] = frozenset(ALL_PROFILES)
    moments: tuple[float, ...] = (1.0, 7.0)
    earlier: tuple[tuple[ScoreboardCard[Any], float], ...] = ()
    up_at: float = 0.0

    def frames(self) -> tuple[tuple[str, float], ...]:
        """Each frame's name and moment."""
        if len(self.moments) == 1:
            return ((self.key, self.moments[0]),)
        return tuple((f"{self.key}-{suffix}", moment) for suffix, moment in zip("ab", self.moments))


_NO_SINGLE = frozenset({QUAD, STACK})
_ONE = (1.0,)

# POR at MIN at Q3 7:18: POR's count drops 5 to 4 with the clock stopped, the quarter's first timeout. Its first
# turn is the caption; the stat panels take the rest of the break.
_TO_AWAY = side(58, 4, False, (26, 25, 7))
_TO_HOME = side(68, 4, False, (26, 33, 9))
_TO_NOW = _utc("2026-11-17T02:35:30Z")


def _timeout(bottom: BottomPanel) -> ScoreboardCard[BasketballGameCardPayload]:
    payload = live(_TO_AWAY, _TO_HOME, 3, 4380, home_win=83.0, records=_POR_MIN, bottom=bottom, timeout=A)
    return game_card(payload)


# The play queue: the first read has both teams scoring; a second lands during the first play's 6 s with a foul and
# the free throws it gave. Plays within a read go away, then home.
_QUEUE_PLAYS: tuple[BasketballCaption, ...] = (
    FieldGoalMade(side=A, shooter="J.Grant", value=ShotValue.THREE, assister="J.Holiday"),
    FieldGoalMade(side=H, shooter="A.Edwards", value=ShotValue.TWO, assister="N.Reid"),
    PersonalFoul(side=A, player="D.Clingan", count=4),
    FreeThrows(side=H, shooter="A.Edwards", made=2, attempted=2, game_made=6, game_attempted=6),
)
_QUEUE_READS = (
    (side(68, 3, False, (26, 25, 17)), side(79, 4, True, (26, 33, 20)), 1300),
    (side(68, 3, False, (26, 25, 17)), side(81, 4, True, (26, 33, 22)), 1250),
)


def _queue_card(i: int) -> ScoreboardCard[BasketballGameCardPayload]:
    """Play ``i``: 1/2 as the first read leaves it, then 2/4, 3/4 and 4/4 once the second lands."""
    first = i == 1
    away, home, clock = _QUEUE_READS[0 if first else 1]
    bottom = caption(_QUEUE_PLAYS[i - 1], (i, 2 if first else 4))
    return game_card(live(away, home, 3, clock, home_win=88.0 if first else 89.0, records=_POR_MIN, bottom=bottom))


def _queue(i: int) -> Specimen:
    """Play ``i`` a second after it went up, ``6 (i - 1)`` seconds into the turn: the turn's halves alternate."""
    up = 6.0 * (i - 1)
    earlier = tuple((_queue_card(j), 6.0 * (j - 1)) for j in range(1, i))
    return Specimen(
        f"live-queue-{i}",
        _queue_card(i),
        _utc("2026-11-17T02:52:08Z") + timedelta(seconds=up),
        moments=(up + 1.0,),
        earlier=earlier,
        up_at=up,
    )


_LATE_PERIODS = ((26, 25, 22, 25), (26, 33, 28, 11))  # POR at MIN, tied after four
_FINAL = live(
    side(98, 2, True, (26, 25, 22, 25)),
    side(106, 1, True, (26, 33, 28, 19)),
    4,
    None,
    home_win=None,
    records=(WinLossRecord(wins=6, losses=9), WinLossRecord(wins=10, losses=5)),
    phase=BasketballPhase.FINAL,
)
_FINAL_OT = live(
    side(117, None, False, (29, 24, 29, 22, 7, 6)),
    side(119, None, False, (25, 28, 25, 26, 7, 8)),
    6,
    None,
    home_win=None,
    records=(WinLossRecord(wins=10, losses=5), WinLossRecord(wins=9, losses=7)),
    phase=BasketballPhase.FINAL,
)


def _with_best(
    payload: BasketballGameCardPayload, away: Sequence[PlayerLine], home: Sequence[PlayerLine]
) -> BasketballGameCardPayload:
    return BasketballGameCardPayload(state=payload.state, records=payload.records, best=BestPlayers.of(away, home))


_ORL_BOS_RANKS = BySide[StandingRanks | None](
    StandingRanks(conference=6, division=2), StandingRanks(conference=9, division=3)
)
_ORL_BOS = BySide[WinLossRecord | None](_ORL, _BOS)
_PREGAME_TIP = _utc("2026-11-17T01:00:00Z")

SPECIMENS: tuple[Specimen, ...] = (
    Specimen(
        "pregame",
        pregame_card(
            BasketballPregameCardPayload(
                phase=BasketballPhase.SCHEDULED,
                tip=_PREGAME_TIP,
                records=BySide(_POR, _MIN),
                ranks=BySide[StandingRanks | None](
                    StandingRanks(conference=11, division=4), StandingRanks(conference=5, division=2)
                ),
                line=FavoriteLine(favorite=H, moneyline=-265, spread=-6.5),
            )
        ),
        _PREGAME_TIP - timedelta(hours=1, minutes=23),
        moments=_ONE,
    ),
    Specimen(
        "pregame-postponed-new-tip",
        pregame_card(
            BasketballPregameCardPayload(
                phase=BasketballPhase.POSTPONED,
                tip=_utc("2026-11-17T19:00:00Z"),
                records=_ORL_BOS,
                ranks=_ORL_BOS_RANKS,
            ),
            away="ORL",
            home="BOS",
        ),
        _utc("2026-11-16T23:37:00Z"),
        moments=_ONE,
    ),
    Specimen(
        "postponed-tbd",
        pregame_card(
            BasketballPregameCardPayload(
                phase=BasketballPhase.POSTPONED, tip=None, records=_ORL_BOS, ranks=_ORL_BOS_RANKS
            ),
            away="ORL",
            home="BOS",
        ),
        _utc("2026-11-16T23:37:00Z"),
        moments=_ONE,
    ),
    Specimen(
        "live-line-score",
        game_card(
            live(
                side(58, 4, False, (26, 25, 7)),
                side(68, 5, False, (26, 33, 9)),
                3,
                3670,
                home_win=84.0,
                records=_POR_MIN,
            )
        ),
        T,
    ),
    # The detail scrolls on the stack, so its second half is drawn where the line is back at its head: 31 s in,
    # the second half of the card's third turn.
    Specimen(
        "live-caption",
        game_card(
            live(
                side(54, 5, False, (26, 25, 3)),
                side(62, 4, False, (26, 33, 3)),
                3,
                5820,
                home_win=79.0,
                records=_POR_MIN,
                bottom=caption(FieldGoalMade(side=A, shooter="S.Sharpe", value=ShotValue.THREE, assister="D.Avdija")),
            )
        ),
        _utc("2026-11-17T02:30:29Z"),
        moments=(1.0, 31.0),
    ),
    *(_queue(i) for i in range(1, 5)),
    Specimen("live-timeout", _timeout(caption(TimeoutTaken(side=A, left=4))), _TO_NOW),
    Specimen("live-box-score", _timeout(TeamBoxPanel.of(POR_Q3, MIN_Q3)), _TO_NOW, profiles=_NO_SINGLE),
    # The player lines scroll: their first half at the head, their second at the tail.
    Specimen(
        "live-player-lines",
        _timeout(PlayerLinesPanel.ranked(0, POR_Q3, MIN_Q3)),
        _TO_NOW,
        profiles=_NO_SINGLE,
        moments=(1.0, 11.5),
    ),
    Specimen(
        "live-player-lines-next",
        _timeout(PlayerLinesPanel.ranked(1, POR_Q3, MIN_Q3)),
        _TO_NOW,
        profiles=_NO_SINGLE,
        moments=(1.0, 11.5),
    ),
    Specimen(
        "live-challenge",
        game_card(
            live(
                side(58, 4, False, (26, 25, 7)),
                side(73, 5, False, (26, 33, 14)),
                3,
                3060,
                home_win=88.0,
                records=_POR_MIN,
                bottom=caption(
                    ChallengeWon(side=A, change=PlayerCallChange(change=CallChange.FOUL_TO_BLOCK, player="T.Camara"))
                ),
            )
        ),
        _utc("2026-11-17T02:44:30Z"),
        profiles=_NO_SINGLE,
    ),
    Specimen(
        "live-reversal",
        game_card(
            live(
                side(47, 5, False, (26, 21)),
                side(50, 4, False, (26, 24)),
                2,
                2200,
                home_win=61.0,
                records=_POR_MIN,
                bottom=caption(CallReversed(change=PlayerCallChange(change=CallChange.FOUL_OFF, player="J.Randle"))),
            )
        ),
        _utc("2026-11-17T02:05:40Z"),
        profiles=_NO_SINGLE,
    ),
    Specimen(
        "live-run",
        game_card(
            live(
                side(45, 5, False, (26, 19)),
                side(48, 4, False, (26, 22)),
                2,
                2930,
                home_win=58.0,
                records=_POR_MIN,
                run=ScoringRun(team=H, scored=7, allowed=0, span_seconds=88),
            )
        ),
        _utc("2026-11-17T02:01:04Z"),
        profiles=_NO_SINGLE,
    ),
    Specimen(
        "live-bonus",
        game_card(
            live(side(49, 5, False, (26, 23)), side(52, 4, True, (26, 26)), 2, 1400, home_win=63.0, records=_POR_MIN)
        ),
        _utc("2026-11-17T02:08:09Z"),
    ),
    Specimen(
        "live-late-close",
        game_card(
            live(
                side(98, 1, True, _LATE_PERIODS[0]),
                side(98, 2, True, _LATE_PERIODS[1]),
                4,
                482,
                home_win=56.0,
                records=_POR_MIN,
            )
        ),
        _utc("2026-11-17T03:40:00Z"),
    ),
    Specimen(
        "late-season-two-digits",
        game_card(
            live(
                side(74, 3, False, (28, 24, 22)),
                side(71, 4, True, (25, 27, 19)),
                3,
                2520,
                home_win=48.0,
                records=(_DEN_LATE, _LAL_LATE),
            ),
            away="DEN",
            home="LAL",
        ),
        _utc("2026-03-20T03:31:00Z"),
    ),
    Specimen(
        "late-season-three-digits",
        game_card(
            live(
                side(104, 1, True, (28, 24, 26, 26)),
                side(101, 2, True, (25, 27, 24, 25)),
                4,
                620,
                home_win=39.0,
                records=(_DEN_LATE, _LAL_LATE),
            ),
            away="DEN",
            home="LAL",
        ),
        _utc("2026-03-20T04:12:00Z"),
    ),
    Specimen(
        "end-q1",
        game_card(
            live(
                side(20, 6, True, (20,)),
                side(17, 6, False, (17,)),
                1,
                None,
                home_win=41.0,
                records=(_ORL, _BOS),
                phase=BasketballPhase.END_OF_PERIOD,
            ),
            away="ORL",
            home="BOS",
        ),
        _utc("2026-11-17T00:36:46Z"),
    ),
    Specimen(
        "overtime",
        game_card(
            live(
                side(105, 2, False, (*_LATE_PERIODS[0], 7)),
                side(107, 1, True, (*_LATE_PERIODS[1], 9)),
                5,
                1360,
                home_win=71.0,
                records=_POR_MIN,
            )
        ),
        _utc("2026-11-17T03:52:00Z"),
    ),
    Specimen(
        "double-overtime",
        game_card(
            live(
                side(113, 1, True, (29, 24, 29, 22, 7, 2)),
                side(115, 0, True, (25, 28, 25, 26, 7, 4)),
                6,
                650,
                home_win=66.0,
                records=(_DET, _GSW),
            ),
            away="DET",
            home="GSW",
        ),
        _utc("2026-11-17T04:30:00Z"),
    ),
    Specimen(
        "halftime",
        game_card(
            live(
                side(51, 5, False, (26, 25)),
                side(59, 4, True, (26, 33)),
                2,
                None,
                home_win=74.0,
                records=_POR_MIN,
                phase=BasketballPhase.HALFTIME,
                break_ends_at=_utc("2026-11-17T02:11:56Z") + timedelta(minutes=15),
            )
        ),
        _utc("2026-11-17T02:14:21Z"),
        moments=_ONE,
    ),
    # The stack's final shows the winner's best player, then the loser's; the others name the teams all turn.
    Specimen(
        "final",
        game_card(_with_best(_FINAL, POR_FINAL, MIN_FINAL)),
        _utc("2026-11-17T03:56:15Z"),
        profiles=frozenset({STACK}),
    ),
    Specimen(
        "final",
        game_card(_with_best(_FINAL, POR_FINAL, MIN_FINAL)),
        _utc("2026-11-17T03:56:15Z"),
        profiles=frozenset({QUAD, SINGLE}),
        moments=_ONE,
    ),
    Specimen(
        "final-overtime",
        game_card(_with_best(_FINAL_OT, DET_FINAL, GSW_FINAL), away="DET", home="GSW"),
        _utc("2026-11-17T04:40:00Z"),
        profiles=frozenset({STACK}),
    ),
    Specimen(
        "final-overtime",
        game_card(_with_best(_FINAL_OT, DET_FINAL, GSW_FINAL), away="DET", home="GSW"),
        _utc("2026-11-17T04:40:00Z"),
        profiles=frozenset({QUAD, SINGLE}),
        moments=_ONE,
    ),
)
