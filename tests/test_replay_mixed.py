"""The mixed MLB + NFL day, run through the production spine as one timed replay.

A synthetic Sunday, 2026-11-22, on a board in Mountain time, with MLB favorites ``mlb:MIN`` and a 45 s TV delay.
All times are local; the replay runs from Saturday 23:50 to Sunday 15:20.

- **Midnight.** TEX@SEA (``776102``, Saturday's) is live in extra innings at the start and final at 00:15. From
  midnight Sunday's slate lists it too: one contest, as the newer answer has it. Saturday's postponed KC@CWS
  (``776101``) sits on Saturday's slate.
- **Saturday's slate partial,** 00:30 to 02:00: it skips KC@CWS's row with a warning, and the row stays.
- **NFL** lists three games from midnight: DAL@PHI (``401``) and MIN@GB (``402``) kick off at 11:00, SF@SEA
  (``403``) at 14:25. Each feed answers with synthetic states (kickoff, drives, halftime, the final) and a few
  synthetic plays, read every 30 s while it's live; NFL's cards share the board with MLB's under the same 45 s delay.
  DAL@PHI ends 17-13 at 14:12 and MIN@GB 20-24 at 14:08; SF@SEA scores its touchdown after the wall clock's step.
  NFL's source fails from 11:55 to 11:59: its slate and its feeds.
- **The Twins and the Vikings** (``MIN`` both) play at once: MIN@CLE (``776201``) is listed at 11:00, live from
  11:10 with a MIN home run at 11:20, final at 11:32.
- **A shared raw id:** BOS@NYY is MLB ``401``, listed at 11:40, live from 11:50, final 3-2 at 12:20, corrected to
  4-2 at 12:27.
- **The wall clock steps back ten minutes** at 15:00. LAD@SD (``776203``) is live across it, with a LAD home run at
  14:59:40, and final at 15:12. HOU@SEA (``776204``) joins Sunday's slate at 15:00:10.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from omni.cards.base import CardKind, ScoreboardCard
from omni.cards.baseball import FinalCardPayload
from omni.cards.football import LiveFootballCardPayload
from omni.core.clock import ClockReading, FakeClock, MonotonicTime
from omni.core.enum import DisplayPriority, GameStatus, HomeAway, League, PanelProfile
from omni.core.ids import LeagueScopedId
from omni.core.time import DurationSeconds
from omni.domain.baseball import (
    BaseballBaseState,
    BaseballCount,
    BaseballGameState,
    InningPhase,
    PregameDetails,
    scoring_impact,
)
from omni.domain.contest import Contest, TeamGame
from omni.domain.football import FootballGameState, FootballInterval, PlaySummary, PlayTone, SetPiece
from omni.events.baseball import BaseballGameEvent, BaseballGameEventType, BaseballPlayPayload
from omni.events.football import FootballEvent, FootballEventKind
from omni.preferences.favorites import Favorites
from omni.providers.base import LocalDate, PollScope, ScopeOutcome
from omni.providers.mlb_statsapi import _event_importance
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import NFLMETA_SOURCE
from omni.app.poll_plan import PollKey, PollSource
from omni.app.quota import ProviderAccount
from omni.replay.mixed import (
    MLB_STATSAPI,
    NFL_FEED_EVERY,
    AlsoListed,
    ClockStep,
    Correction,
    EarlyReveal,
    MixedRun,
    MixedStep,
    MixedTimeline,
    PartialSlate,
    SlateFrame,
    SourceOutage,
    Wake,
    _ReplayMlb,
    _spoiled,
    _World,
    mixed_decisions,
    run_mixed,
)
from omni.replay.timeline import GameFrame
from omni.replay.harness import replay_versions
from omni.tower.board import SourceHealth

ZONE = ZoneInfo("America/Denver")
SAT, SUN = date(2026, 11, 21), date(2026, 11, 22)
FAVORITES = Favorites({League.MLB: ("MIN",)})
LAG = DurationSeconds(45)
QUAD = PanelProfile.QUAD_128X64
DECISIONS = Path(__file__).parent / "fixtures" / "replay" / "mixed-slate.decisions.txt"

_MLB = MlbTeamRegistry.from_color_file()
_NFL = NflTeamRegistry.default(source=NFLMETA_SOURCE)
_TOP, _BOTTOM = InningPhase.TOP, InningPhase.BOTTOM


def _at(day: date, hour: int, minute: int, second: int = 0) -> datetime:
    """A local time in Mountain time, as UTC."""
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=ZONE).astimezone(timezone.utc)


START, END = _at(SAT, 23, 50), _at(SUN, 15, 20)
BACKSTEP = _at(SUN, 15, 0)
STEP = 1 / 64  # the replay's step, in seconds
NFL_DOWN = SourceOutage(league=League.NFL, start=_at(SUN, 11, 55), end=_at(SUN, 11, 59))


def _mlb_id(raw: str) -> LeagueScopedId:
    return LeagueScopedId(League.MLB, MLB_STATSAPI, raw)


def _nfl_id(raw: str) -> LeagueScopedId:
    return LeagueScopedId(League.NFL, NFLMETA_SOURCE, raw)


POSTPONED, LATE, TWINS, SHARED, AFTERNOON, EVENING = (
    _mlb_id(raw) for raw in ("776101", "776102", "776201", "401", "776203", "776204")
)
NFL_SHARED, VIKINGS, SF_SEA = (_nfl_id(raw) for raw in ("401", "402", "403"))


@dataclass(frozen=True, slots=True)
class _Line:
    """A live game's situation: inning, half, score, hits and outs."""

    inning: int
    phase: InningPhase
    away: int
    home: int
    away_hits: int
    home_hits: int
    outs: int = 0

    def state(self) -> BaseballGameState:
        return BaseballGameState(
            away_score=self.away,
            home_score=self.home,
            inning=self.inning,
            phase=self.phase,
            count=BaseballCount(balls=1, strikes=1, outs=self.outs),
            bases=BaseballBaseState(),
            away_hits=self.away_hits,
            home_hits=self.home_hits,
        )


@dataclass
class _Game:
    """One MLB game's frames, written in order."""

    raw: str
    away: int
    home: int
    day: date
    start: datetime
    frames: list[GameFrame] = field(default_factory=list)
    plays: list[BaseballGameEvent] = field(default_factory=list)

    def team_game(self, status: GameStatus) -> TeamGame:
        return TeamGame(
            id=_mlb_id(self.raw),
            league=League.MLB,
            status=status,
            scheduled_start=self.start,
            provider_date=self.day,
            away=_MLB.resolve(self.away),
            home=_MLB.resolve(self.home),
        )

    def listed(self, at: datetime, status: GameStatus) -> _Game:
        self.frames.append(GameFrame(at=at, game=self.team_game(status)))
        return self

    def live(self, at: datetime, line: _Line) -> _Game:
        state = line.state()
        self.frames.append(
            GameFrame(at=at, game=self.team_game(GameStatus.LIVE), state=state, events=tuple(self.plays))
        )
        return self

    def home_run(self, at: datetime, line: _Line) -> _Game:
        """A one-run home run at ``at``, leaving the game at ``line``."""
        impact = scoring_impact(phase=line.phase, inning=line.inning, rbi=1, away_score=line.away, home_score=line.home)
        self.plays.append(
            BaseballGameEvent(
                id=_mlb_id(f"{self.raw}:ab:{len(self.plays) + 1}"),
                contest=self.team_game(GameStatus.LIVE),
                event_type=BaseballGameEventType.HOME_RUN,
                source=MLB_STATSAPI,
                source_time=at,
                observed_at=at,
                importance=_event_importance(BaseballGameEventType.HOME_RUN, impact),
                payload=BaseballPlayPayload(
                    inning=line.inning,
                    phase=line.phase,
                    description="A synthetic home run.",
                    rbi=1,
                    away_score=line.away,
                    home_score=line.home,
                    scoring_impact=impact,
                ),
            )
        )
        return self.live(at, line)

    def final(self, at: datetime, line: _Line) -> _Game:
        self.frames.append(
            GameFrame(at=at, game=self.team_game(GameStatus.FINAL), state=line.state(), events=tuple(self.plays))
        )
        return self


def _nfl(raw: str, away: str, home: str, kickoff: datetime, final: datetime | None = None) -> list[SlateFrame]:
    """An NFL game listed from the start, live from ``kickoff``, and final at ``final`` if it ends in the day: its
    schedule entries alone, with nothing from its feed."""
    game = _Football(raw, away, home, kickoff).listed(START)
    game.frames.append(SlateFrame(at=kickoff, game=game.team_game(GameStatus.LIVE)))
    if final is not None:
        game.frames.append(SlateFrame(at=final, game=game.team_game(GameStatus.FINAL)))
    return game.frames


@dataclass
class _Football:
    """One NFL game's frames, written in order: listed, then each answer its feed gives from kickoff."""

    raw: str
    away: str
    home: str
    kickoff: datetime
    frames: list[SlateFrame] = field(default_factory=list)
    plays: int = 0

    def team_game(self, status: GameStatus) -> TeamGame:
        return TeamGame(
            id=_nfl_id(self.raw),
            league=League.NFL,
            status=status,
            scheduled_start=self.kickoff,
            provider_date=SUN,
            away=_NFL.resolve(self.away),
            home=_NFL.resolve(self.home),
        )

    def listed(self, at: datetime) -> _Football:
        self.frames.append(SlateFrame(at=at, game=self.team_game(GameStatus.SCHEDULED)))
        return self

    def live(
        self, at: datetime, state: FootballGameState, *plays: PlaySummary, break_ends_at: datetime | None = None
    ) -> _Football:
        """The game as its feed answers from ``at``, with ``plays`` written then."""
        events = []
        for summary in plays:
            self.plays += 1
            events.append(
                FootballEvent(
                    key=f"{self.raw}:{self.plays}",
                    kind=FootballEventKind.PLAY,
                    summary=summary,
                    play=self.plays,
                    written_at=at,
                )
            )
        game = self.team_game(GameStatus.LIVE)
        self.frames.append(SlateFrame(at=at, game=game, state=state, plays=tuple(events), break_ends_at=break_ends_at))
        return self

    def final(self, at: datetime, away: int, home: int) -> _Football:
        state = FootballGameState(
            period=4, clock_seconds=0, away_score=away, home_score=home, interval=FootballInterval.FINAL
        )
        self.frames.append(SlateFrame(at=at, game=self.team_game(GameStatus.FINAL), state=state))
        return self


def _kickoff(away: int = 0, home: int = 0, *, period: int = 1, clock: int = 900) -> FootballGameState:
    """The home side kicking off from its 35."""
    return FootballGameState(
        period=period,
        clock_seconds=clock,
        away_score=away,
        home_score=home,
        possession=HomeAway.HOME,
        yards_to_goal=65,
        set_piece=SetPiece.KICKOFF,
    )


def _snap(
    period: int, clock: int, away: int, home: int, ball: HomeAway, down: int, distance: int, to_go: int
) -> FootballGameState:
    return FootballGameState(
        period=period,
        clock_seconds=clock,
        away_score=away,
        home_score=home,
        possession=ball,
        down=down,
        distance=distance,
        yards_to_goal=to_go,
    )


def _half(away: int, home: int) -> FootballGameState:
    return FootballGameState(
        period=2, clock_seconds=0, away_score=away, home_score=home, interval=FootballInterval.HALFTIME
    )


def _run(yards: int) -> PlaySummary:
    return PlaySummary(tone=PlayTone.STANDARD, headline=f"RUN +{yards}", detail="A synthetic carry.")


def _pass(yards: int) -> PlaySummary:
    return PlaySummary(tone=PlayTone.STANDARD, headline=f"PASS +{yards}", detail="A synthetic catch.")


_TOUCHDOWN = PlaySummary(tone=PlayTone.SCORE, headline="TOUCHDOWN", detail="A synthetic score.")
_AWAY, _HOME = HomeAway.AWAY, HomeAway.HOME


def nfl_day() -> tuple[_Football, ...]:
    """The day's NFL games, with their feeds' answers. As NFLMeta's do, the scoreboard moves first and the play
    behind it is written half a minute later, so each play reaches the board a read after its field."""
    dal = _Football("401", "DAL", "PHI", _at(SUN, 11, 0)).listed(START).live(_at(SUN, 11, 0), _kickoff())
    gain = _snap(1, 192, 0, 0, _AWAY, 2, 6, 34)
    dal.live(_at(SUN, 11, 20, 10), gain).live(_at(SUN, 11, 20, 40), gain, _pass(12))
    scored = _kickoff(7, 0, period=2, clock=895)
    dal.live(_at(SUN, 11, 34, 10), scored).live(_at(SUN, 11, 34, 40), scored, _TOUCHDOWN)
    dal.live(_at(SUN, 12, 28), _half(7, 3), break_ends_at=_at(SUN, 12, 41))
    dal.live(_at(SUN, 12, 43), _snap(3, 890, 7, 3, _HOME, 1, 10, 70))
    late = _snap(4, 120, 14, 13, _AWAY, 3, 4, 22)
    dal.live(_at(SUN, 13, 40, 20), late).live(_at(SUN, 13, 40, 50), late, _run(3))
    dal.final(_at(SUN, 14, 12), 17, 13)
    gb = _Football("402", "MIN", "GB", _at(SUN, 11, 0)).listed(START).live(_at(SUN, 11, 0), _kickoff())
    gb.live(_at(SUN, 12, 0), _snap(2, 480, 3, 7, _HOME, 1, 10, 55))
    gb.live(_at(SUN, 12, 33), _half(3, 10), break_ends_at=_at(SUN, 12, 46))
    gb.live(_at(SUN, 13, 30), _snap(4, 360, 13, 17, _AWAY, 2, 8, 40))
    gb.final(_at(SUN, 14, 8), 20, 24)
    sea = _Football("403", "SF", "SEA", _at(SUN, 14, 25)).listed(START).live(_at(SUN, 14, 25), _kickoff())
    deep = _snap(1, 90, 0, 0, _AWAY, 1, 10, 18)
    sea.live(_at(SUN, 14, 50, 15), deep).live(_at(SUN, 14, 50, 45), deep, _pass(22))
    touchdown = _kickoff(7, 0, period=2, clock=720)
    sea.live(_at(SUN, 15, 5, 15), touchdown).live(_at(SUN, 15, 5, 45), touchdown, _TOUCHDOWN)
    return dal, gb, sea


def mixed_slate() -> MixedTimeline:
    """The day, as a mixed timeline."""
    postponed = _Game("776101", away=118, home=145, day=SAT, start=_at(SAT, 18, 10))
    postponed.listed(START, GameStatus.POSTPONED)
    late = _Game("776102", away=140, home=136, day=SAT, start=_at(SAT, 20, 10))
    late.live(START, _Line(12, _TOP, 4, 4, 9, 8)).live(_at(SAT, 23, 56), _Line(12, _BOTTOM, 4, 4, 9, 8, 1))
    late.live(_at(SUN, 0, 4), _Line(13, _TOP, 5, 4, 11, 8, 2)).live(_at(SUN, 0, 10), _Line(13, _BOTTOM, 5, 4, 11, 9))
    late.final(_at(SUN, 0, 15), _Line(13, _BOTTOM, 5, 4, 11, 9, 3))
    twins = _Game("776201", away=142, home=114, day=SUN, start=_at(SUN, 11, 10))
    twins.listed(_at(SUN, 11, 0), GameStatus.PREGAME).live(_at(SUN, 11, 10), _Line(1, _TOP, 0, 0, 0, 0))
    twins.live(_at(SUN, 11, 14), _Line(2, _TOP, 0, 0, 1, 1)).home_run(_at(SUN, 11, 20), _Line(3, _TOP, 1, 0, 2, 1))
    twins.live(_at(SUN, 11, 26), _Line(4, _BOTTOM, 1, 0, 4, 3)).final(_at(SUN, 11, 32), _Line(9, _BOTTOM, 2, 1, 8, 6))
    shared = _Game("401", away=111, home=147, day=SUN, start=_at(SUN, 11, 50))
    shared.listed(_at(SUN, 11, 40), GameStatus.PREGAME).live(_at(SUN, 11, 50), _Line(1, _TOP, 0, 0, 0, 0))
    shared.live(_at(SUN, 12, 0), _Line(3, _BOTTOM, 1, 1, 3, 2)).live(_at(SUN, 12, 10), _Line(6, _TOP, 3, 2, 7, 5))
    shared.final(_at(SUN, 12, 20), _Line(9, _BOTTOM, 3, 2, 9, 7, 3))
    afternoon = _Game("776203", away=119, home=135, day=SUN, start=_at(SUN, 14, 45))
    afternoon.listed(_at(SUN, 14, 35), GameStatus.PREGAME).live(_at(SUN, 14, 45), _Line(1, _TOP, 0, 0, 0, 0))
    afternoon.live(_at(SUN, 14, 52), _Line(2, _TOP, 0, 0, 1, 1))
    afternoon.home_run(_at(SUN, 14, 59, 40), _Line(3, _TOP, 1, 0, 2, 1))
    afternoon.live(_at(SUN, 15, 0, 30), _Line(3, _BOTTOM, 1, 0, 2, 2, 1))
    afternoon.live(_at(SUN, 15, 6), _Line(4, _TOP, 2, 0, 4, 2)).final(_at(SUN, 15, 12), _Line(9, _BOTTOM, 2, 0, 7, 4))
    evening = _Game("776204", away=117, home=136, day=SUN, start=_at(SUN, 18, 10))
    evening.listed(_at(SUN, 15, 0, 10), GameStatus.SCHEDULED)
    games = (postponed, late, twins, shared, afternoon, evening)
    return MixedTimeline(
        zone=ZONE,
        start=START,
        end=END,
        mlb=tuple(frame for game in games for frame in game.frames),
        nfl=tuple(frame for game in nfl_day() for frame in game.frames),
        also_listed=(AlsoListed(LATE, SUN),),
        faults=(
            PartialSlate(
                league=League.MLB,
                day=SAT,
                skipped=POSTPONED,
                warning="game 776101: unreadable row skipped",
                start=_at(SUN, 0, 30),
                end=_at(SUN, 2, 0),
            ),
            NFL_DOWN,
            Correction(at=_at(SUN, 12, 27), game=SHARED, away=4, home=2),
            ClockStep(at=BACKSTEP, by=timedelta(minutes=-10)),
        ),
    )


# --- the run, step by step -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Scope:
    """One scope at one step: its last outcome, its own games, and its last answer's receipt."""

    outcome: ScopeOutcome | None
    members: dict[LeagueScopedId, GameStatus]
    answered: ClockReading | None


@dataclass(frozen=True)
class _Moment:
    """What the board held, and drew, at one step of the run."""

    elapsed: float  # seconds since the start, on the running clock
    world: datetime
    wall: datetime
    contests: dict[LeagueScopedId, Contest]
    listed: tuple[LeagueScopedId, ...]  # the board's contests' ids, as the store hands them out
    scopes: dict[PollScope, _Scope]
    leagues: dict[League, SourceHealth]
    board: SourceHealth
    drawn: ScoreboardCard[Any] | None
    stale: bool
    render_error: str | None


class _Staged:
    """Each MLB card's versions in the order they were first staged: its key, what it showed, and its band."""

    def __init__(self) -> None:
        self._last: dict[str, tuple[Any, DisplayPriority]] = {}
        self.versions: list[tuple[str, Any, DisplayPriority]] = []

    def __call__(self, step: MixedStep) -> None:
        for key, card in step.view.cards.items():
            shown = (card.payload, card.priority.band)
            if card.league is League.MLB and self._last.get(key) != shown:
                self._last[key] = shown
                self.versions.append((key, *shown))


@dataclass(frozen=True)
class _Day:
    run: MixedRun
    moments: list[_Moment]
    staged: list[tuple[str, Any, DisplayPriority]]  # each MLB card's versions, as `_Staged` gathers them


def _record(timeline: MixedTimeline) -> _Day:
    """Run ``timeline`` on the quad board, noting what the board held and drew at every step."""
    moments: list[_Moment] = []
    staged = _Staged()

    def watch(step: MixedStep) -> None:
        staged(step)
        store = step.store
        scopes = {}
        for scope in store.scopes:
            health = store.health(scope, step.at.monotonic)
            assert health is not None
            members = {contest.id: contest.status for contest in store.membership(scope)}
            scopes[scope] = _Scope(outcome=health.outcome, members=members, answered=health.answered)
        moments.append(
            _Moment(
                elapsed=step.elapsed.total_seconds(),
                world=step.world,
                wall=step.at.utc,
                contests={contest.id: contest for contest in store.contests},
                listed=tuple(contest.id for contest in store.contests),
                scopes=scopes,
                leagues={league.league: league.source for league in step.refresh.leagues},
                board=step.refresh.health.source,
                drawn=step.drawn,
                stale=step.painted.stale,
                render_error=step.painted.render_error,
            )
        )

    run = run_mixed(timeline, profile=QUAD, favorites=FAVORITES, broadcast_lag=LAG, watch=watch)
    return _Day(run=run, moments=moments, staged=staged.versions)


@pytest.fixture(scope="module")
def day() -> _Day:
    return _record(mixed_slate())


def _mlb(day: date) -> PollScope:
    return PollScope(MLB_STATSAPI, League.MLB, LocalDate(day))


def _nfl_scope(day: date) -> PollScope:
    return PollScope(NFLMETA_SOURCE, League.NFL, LocalDate(day))


def _since(instant: datetime) -> float:
    """``instant`` as seconds since the start, on the running clock."""
    return (instant - START).total_seconds()


def _between(day: _Day, start: datetime, end: datetime) -> list[_Moment]:
    return [moment for moment in day.moments if start <= moment.world < end]


def _asked(run: MixedRun, subject: LeagueScopedId | PollScope, source: PollSource) -> list[float]:
    """When ``subject`` was asked of ``source``, in seconds since the start."""
    key: PollKey[Any] = PollKey(source, subject)
    return [fetched.at.total_seconds() for fetched in run.fetched if fetched.key == key]


def _first_ask(run: MixedRun, subject: LeagueScopedId | PollScope, source: PollSource, after: datetime) -> float:
    return min(at for at in _asked(run, subject, source) if at >= _since(after))


def _gaps(run: MixedRun, game: LeagueScopedId, start: datetime, end: datetime) -> set[float]:
    """The spans between a game's feed asks from ``start`` to ``end``."""
    asked = [at for at in _asked(run, game, PollSource.GAME_FEED) if _since(start) <= at < _since(end)]
    return {round(later - earlier, 1) for earlier, later in zip(asked, asked[1:])}


def _drawn(day: _Day, card: str) -> list[_Moment]:
    return [moment for moment in day.moments if moment.drawn is not None and moment.drawn.id.raw == card]


def _league_of(key: PollKey[Any]) -> League:
    return key.subject.league


def _received(scope: _Scope) -> MonotonicTime:
    assert scope.answered is not None
    return scope.answered.monotonic


def _runs(values: list[SourceHealth]) -> list[SourceHealth]:
    """``values`` with each run of equal ones collapsed to one."""
    return [value for index, value in enumerate(values) if index == 0 or values[index - 1] is not value]


# --- the fixture -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", list(PanelProfile))
def test_the_mixed_day_replays_to_its_recorded_decisions_on_every_profile(profile: PanelProfile) -> None:
    """The board decides alike on every profile; only the drawing differs. The profile is one of the settings, so
    each profile's header carries its own digest, and the fixture's is the quad board's."""
    header, *decided = mixed_decisions(mixed_slate(), profile=profile, favorites=FAVORITES, broadcast_lag=LAG)
    recorded, *expected = DECISIONS.read_text().splitlines()
    assert decided == expected
    assert header == replay_versions(profile, broadcast_lag=LAG, favorites=FAVORITES).header()
    assert recorded == replay_versions(QUAD, broadcast_lag=LAG, favorites=FAVORITES).header()


def test_two_runs_of_the_mixed_day_decide_ask_and_measure_alike(day: _Day) -> None:
    assert run_mixed(mixed_slate(), profile=QUAD, favorites=FAVORITES, broadcast_lag=LAG) == day.run


def test_nfl_on_the_board_leaves_mlb_s_cards_and_requests_as_they_were(day: _Day) -> None:
    """NFL's cards share the board's airtime, but MLB stages the same cards, showing the same, and asks the same."""
    alone = _Staged()
    without = run_mixed(mixed_slate(), profile=QUAD, favorites=FAVORITES, broadcast_lag=LAG, nfl=False, watch=alone)
    assert day.staged == alone.versions and len(alone.versions) > 20
    assert [fetched for fetched in day.run.fetched if _league_of(fetched.key) is League.MLB] == list(without.fetched)
    nfl = [line for line in day.run.decisions if "nfl:nflmeta:" in line or line.split()[1:3] == ["health", "nfl"]]
    assert any("  up      nfl:nflmeta:" in line for line in nfl)  # NFL's cards air now


def test_nfl_s_games_air_live_and_then_final_each_final_replacing_its_live_card_in_the_order_they_ended(
    day: _Day,
) -> None:
    finals = [line.split()[2] for line in day.run.decisions if line.split()[1] == "final" and "nfl:" in line]
    assert finals == ["nfl:nflmeta:402:final", "nfl:nflmeta:401:final"]  # MIN@GB ended at 14:08, DAL@PHI at 14:12
    for card, ended in (("402", _at(SUN, 14, 8)), ("401", _at(SUN, 14, 12))):
        read = _first_ask(day.run, _nfl_id(card), PollSource.GAME_FEED, after=ended)
        revealed = f"{read + LAG.value:8.1f}"
        assert (
            f"{revealed}  final   nfl:nflmeta:{card}:final  (ended {ended.astimezone(ZONE):%H:%M:%S}, first seen final)"
            in (day.run.decisions)
        )
        down = [line for line in day.run.decisions if line.startswith(f"{revealed}  down    nfl:nflmeta:{card}:live")]
        assert len(down) <= 1  # its live card comes down as the final reveals, if it was on air
        final = _drawn(day, f"nfl:nflmeta:{card}:final")
        assert final and final[0].elapsed >= read + LAG.value and final[-1].elapsed < read + LAG.value + 1800
    # SF@SEA kicked off at 14:25 and is still live at the day's end.
    assert _drawn(day, "nfl:nflmeta:403:live") and not _drawn(day, "nfl:nflmeta:403:final")


def _caption(moment: _Moment) -> str | None:
    assert moment.drawn is not None and isinstance(moment.drawn.payload, LiveFootballCardPayload)
    play = moment.drawn.payload.last_play
    return play.headline if play is not None else None


def test_an_nfl_play_captions_its_card_from_its_release_until_its_hard_end_and_halftime_counts_down(day: _Day) -> None:
    """DAL@PHI's touchdown is written at 11:34:40 and read at 11:35:00, so it clears the delay 20 s early, and as a
    major play it's gone 150 s after. SF@SEA's, after the wall clock's step, can't be dated: the full delay."""
    live = _drawn(day, "nfl:nflmeta:401:live")
    scored = [moment.elapsed for moment in live if _caption(moment) == "TOUCHDOWN"]
    read = _first_ask(day.run, NFL_SHARED, PollSource.GAME_FEED, after=_at(SUN, 11, 34, 40))
    assert scored and read + LAG.value - 20 <= scored[0] and scored[-1] < read + LAG.value - 20 + 150
    late = [moment.elapsed for moment in _drawn(day, "nfl:nflmeta:403:live") if _caption(moment) == "TOUCHDOWN"]
    read = _first_ask(day.run, SF_SEA, PollSource.GAME_FEED, after=_at(SUN, 15, 5, 45))
    assert late and read + LAG.value <= late[0]
    halftime = [moment for moment in live if moment.drawn is not None and moment.drawn.payload.break_ends_at]
    assert halftime and {moment.drawn.payload.break_ends_at for moment in halftime if moment.drawn} == {
        _at(SUN, 12, 41) + LAG.as_timedelta()
    }
    assert _gaps(day.run, NFL_SHARED, _at(SUN, 11, 0), _at(SUN, 11, 55)) == {NFL_FEED_EVERY.total_seconds()}


def test_a_raw_id_both_leagues_use_stays_two_members_with_their_own_cards_and_asks(day: _Day) -> None:
    both = [moment for moment in day.moments if SHARED in moment.contests and NFL_SHARED in moment.contests]
    assert both
    for moment in both:
        mlb, nfl = moment.scopes[_mlb(SUN)].members, moment.scopes[_nfl_scope(SUN)].members
        assert SHARED in mlb and NFL_SHARED not in mlb
        assert NFL_SHARED in nfl and SHARED not in nfl
    drawn = {moment.drawn.id.raw for moment in day.moments if moment.drawn is not None}
    assert {card for card in drawn if ":401:" in card} == {
        "mlb:mlb_statsapi:401:pregame",
        "mlb:mlb_statsapi:401:live",
        "mlb:mlb_statsapi:401:final",
        "nfl:nflmeta:401:live",
        "nfl:nflmeta:401:final",
    }
    for moment in day.moments:  # each card's game is its own league's
        if moment.drawn is not None:
            assert moment.drawn.contest.league is League(moment.drawn.id.raw.split(":")[0])
    assert _asked(day.run, SHARED, PollSource.GAME_FEED) and _asked(day.run, NFL_SHARED, PollSource.GAME_FEED)
    assert _asked(day.run, _mlb(SUN), PollSource.SLATE) and _asked(day.run, _nfl_scope(SUN), PollSource.SLATE)


def test_the_twins_are_the_favorite_and_the_vikings_who_share_their_abbreviation_are_not(day: _Day) -> None:
    live = [moment for moment in day.moments if moment.drawn is not None and moment.drawn.kind is CardKind.LIVE_GAME]
    bands = {(moment.drawn.contest.id, moment.drawn.priority.band) for moment in live if moment.drawn is not None}
    assert (TWINS, DisplayPriority.FAVORITE) in bands
    assert all(band is not DisplayPriority.FAVORITE for game, band in bands if game != TWINS)
    # The favorite's feed is asked at the focus cadence while it's live; another live game's at the normal one.
    assert _gaps(day.run, TWINS, _at(SUN, 11, 12), _at(SUN, 11, 30)) == {8.0}
    assert _gaps(day.run, SHARED, _at(SUN, 11, 52), _at(SUN, 12, 18)) == {15.0}
    # The Vikings play at the same time: on the board's slate with a card of their own, but no favorite.
    playing = [
        moment
        for moment in day.moments
        if all(game in moment.contests and moment.contests[game].status is GameStatus.LIVE for game in (TWINS, VIKINGS))
    ]
    assert playing
    twins, vikings = playing[0].contests[TWINS], playing[0].contests[VIKINGS]
    assert isinstance(twins, TeamGame) and isinstance(vikings, TeamGame)
    assert twins.away.abbreviation == vikings.away.abbreviation == "MIN"
    assert (FAVORITES.of(twins), FAVORITES.of(vikings)) == (1, None)
    assert (VIKINGS, DisplayPriority.NORMAL) in bands
    assert _gaps(day.run, VIKINGS, _at(SUN, 11, 0), _at(SUN, 11, 55)) == {NFL_FEED_EVERY.total_seconds()}


def test_after_the_wall_clock_steps_back_the_next_slate_lands_at_once_and_mlb_stays_fresh(day: _Day) -> None:
    listed = _at(SUN, 15, 0, 10)
    asked = _first_ask(day.run, _mlb(SUN), PollSource.SLATE, after=listed)
    assert asked - _since(listed) <= 15  # its next ask, on its usual cadence
    [landed] = [moment for moment in day.moments if moment.elapsed == asked]
    before = [moment for moment in day.moments if moment.world < BACKSTEP][-1]
    assert EVENING in landed.scopes[_mlb(SUN)].members and EVENING in landed.contests
    assert landed.wall == landed.world - timedelta(minutes=10)
    landed_at, before_at = landed.scopes[_mlb(SUN)].answered, before.scopes[_mlb(SUN)].answered
    assert landed_at is not None and before_at is not None
    assert landed_at.utc < before_at.utc  # by the wall clock it arrived before the answer it replaced
    around = _between(day, BACKSTEP - timedelta(minutes=2), BACKSTEP + timedelta(minutes=5))
    assert (
        {moment.leagues[League.MLB] for moment in around} == {moment.board for moment in around} == {SourceHealth.FRESH}
    )


def test_nfl_goes_stale_through_its_outage_and_recovers_while_mlb_stays_fresh(day: _Day) -> None:
    span = _between(day, _at(SUN, 11, 50), _at(SUN, 12, 5))
    nfl = _nfl_scope(SUN)
    answered = max(at for at in _asked(day.run, nfl, PollSource.SLATE) if at < _since(NFL_DOWN.start))
    recovered = _first_ask(day.run, nfl, PollSource.SLATE, after=NFL_DOWN.end)
    assert _runs([moment.leagues[League.NFL] for moment in span]) == [
        SourceHealth.FRESH,
        SourceHealth.STALE,
        SourceHealth.FRESH,
    ]
    stale = [moment for moment in span if moment.leagues[League.NFL] is SourceHealth.STALE]
    assert stale[0].elapsed == answered + 121 + STEP  # the replay's first step past its 2 minutes, in whole seconds
    assert stale[-1].elapsed < recovered and all(moment.scopes[nfl].outcome is ScopeOutcome.FAILED for moment in stale)
    for moment in span:
        assert set(moment.scopes[nfl].members) == {NFL_SHARED, VIKINGS, SF_SEA}  # a failing scope keeps its games
        assert moment.leagues[League.MLB] is moment.board is SourceHealth.FRESH
    shown = [(moment.drawn, moment.stale) for moment in stale if moment.drawn is not None]
    assert shown and all(dimmed is (card.league is League.NFL) for card, dimmed in shown)  # only NFL's feeds are quiet
    assert {card.league for card, _ in shown} == {League.MLB, League.NFL}
    # Its last good read was at 11:54:30: dimmed from 11:55:30, down two minutes later, and back once a read from
    # after the outage clears the delay.
    quiet = [moment for moment in _drawn(day, "nfl:nflmeta:401:live") if moment.stale]
    assert (
        quiet and _since(_at(SUN, 11, 55, 30)) <= quiet[0].elapsed and quiet[-1].elapsed < _since(_at(SUN, 11, 57, 30))
    )
    back = [moment.elapsed for moment in _drawn(day, "nfl:nflmeta:401:live") if moment.elapsed > quiet[-1].elapsed]
    assert back[0] >= _since(NFL_DOWN.end) + LAG.value


def test_around_midnight_the_game_both_dates_list_is_one_contest_as_the_newer_answer_has_it(day: _Day) -> None:
    both = [
        moment
        for moment in day.moments
        if _mlb(SUN) in moment.scopes
        and LATE in moment.scopes[_mlb(SAT)].members
        and LATE in moment.scopes[_mlb(SUN)].members
    ]
    assert both and all(moment.listed.count(LATE) == 1 for moment in both)
    split = [
        moment for moment in both if moment.scopes[_mlb(SAT)].members[LATE] != moment.scopes[_mlb(SUN)].members[LATE]
    ]
    assert split  # around its final, the two dates' answers disagree for a while
    for moment in split:
        newer = max((moment.scopes[_mlb(SAT)], moment.scopes[_mlb(SUN)]), key=_received)
        assert moment.contests[LATE].status is newer.members[LATE]


def test_the_late_game_stays_on_through_midnight_its_final_and_the_rest_of_the_day(day: _Day) -> None:
    assert all(LATE in moment.contests for moment in day.moments)
    assert any(moment.elapsed > _since(_at(SUN, 0, 0)) for moment in _drawn(day, "mlb:mlb_statsapi:776102:live"))
    final = _first_ask(day.run, LATE, PollSource.GAME_FEED, after=_at(SUN, 0, 15))
    finals = _drawn(day, "mlb:mlb_statsapi:776102:final")
    assert finals and finals[0].elapsed >= final + LAG.value


def test_saturday_s_partial_slate_keeps_the_row_it_skipped(day: _Day) -> None:
    partial = [moment for moment in day.moments if moment.scopes[_mlb(SAT)].outcome is ScopeOutcome.PARTIAL]
    assert partial and _at(SUN, 0, 30) <= partial[0].world and partial[-1].world < _at(SUN, 2, 1)
    assert all(POSTPONED in moment.scopes[_mlb(SAT)].members and POSTPONED in moment.contests for moment in partial)
    # Those answers did skip its row, with a warning.
    middle = _at(SUN, 1, 0)
    update = mixed_slate().slate(League.MLB, SAT, middle, source=MLB_STATSAPI, asked=middle)
    assert [contest.id for contest in update.contests] == [LATE]
    assert update.warnings == ("game 776101: unreadable row skipped",)


def test_a_final_corrected_by_a_later_answer_is_redrawn_once_that_answer_clears_the_delay(day: _Day) -> None:
    drawn = []
    for moment in _drawn(day, "mlb:mlb_statsapi:401:final"):
        assert moment.drawn is not None and isinstance(moment.drawn.payload, FinalCardPayload)
        drawn.append((moment.elapsed, (moment.drawn.payload.away_score, moment.drawn.payload.home_score)))
    [line] = [line for line in day.run.decisions if line.endswith("  corrected  mlb:mlb_statsapi:401:final")]
    corrected = float(line.split()[0])
    assert {score for at, score in drawn if at < corrected} == {(3, 2)}
    assert {score for at, score in drawn if at >= corrected} == {(4, 2)}  # from its next airing on
    assert corrected >= _first_ask(day.run, SHARED, PollSource.GAME_FEED, after=_at(SUN, 12, 27)) + LAG.value


def test_no_card_in_either_league_shows_a_score_before_its_tv_delay_release_across_the_backstep(day: _Day) -> None:
    assert day.run.early == ()
    # Scores were on the board across the step: the afternoon's live card, and its home run.
    home_run = _drawn(day, "mlb:mlb_statsapi:776203:bigplay:776203:ab:1")
    assert home_run and BACKSTEP < home_run[0].world
    asked = _first_ask(day.run, AFTERNOON, PollSource.GAME_FEED, after=_at(SUN, 14, 59, 40))
    assert home_run[0].elapsed >= asked + LAG.value
    # And SF@SEA's touchdown, read only after the step.
    scored = [moment for moment in _drawn(day, "nfl:nflmeta:403:live") if moment.drawn and _score(moment) == (7, 0)]
    asked = _first_ask(day.run, SF_SEA, PollSource.GAME_FEED, after=_at(SUN, 15, 5, 15))
    assert scored and BACKSTEP < scored[0].world and scored[0].elapsed >= asked + LAG.value
    assert all(moment.render_error is None for moment in day.moments)


def _score(moment: _Moment) -> tuple[int, int]:
    assert moment.drawn is not None and isinstance(moment.drawn.payload, LiveFootballCardPayload)
    return moment.drawn.payload.state.away_score, moment.drawn.payload.state.home_score


# --- the parts, one at a time ----------------------------------------------------------------------------------------

SHORT = _at(SUN, 12, 0)


def _short(*games: _Game, end: timedelta = timedelta(minutes=10), **parts: Any) -> MixedTimeline:
    frames = tuple(frame for game in games for frame in game.frames)
    return MixedTimeline(zone=ZONE, start=SHORT, end=SHORT + end, mlb=frames, **parts)


def _playing(raw: str = "1") -> _Game:
    """A game live from the short start, MIN 1 CLE 0."""
    return _Game(raw, away=142, home=114, day=SUN, start=SHORT).live(SHORT, _Line(8, _TOP, 1, 0, 5, 5))


def test_an_nfl_game_whose_feed_has_nothing_to_say_is_a_member_with_no_card() -> None:
    nfl = (*_nfl("401", "DAL", "PHI", SHORT + timedelta(minutes=1)),)
    held: set[LeagueScopedId] = set()

    def watch(step: MixedStep) -> None:
        assert step.painted.render_error is None
        held.update(contest.id for contest in step.store.contests)

    run = run_mixed(_short(_playing("401"), nfl=nfl), profile=QUAD, watch=watch)
    assert held == {_mlb_id("401"), NFL_SHARED}
    assert not any("nfl:nflmeta:401:" in line for line in run.decisions)
    assert _asked(run, NFL_SHARED, PollSource.GAME_FEED)  # asked, with nothing in its answer


def test_an_nfl_game_and_an_mlb_game_with_one_raw_id_air_their_own_cards() -> None:
    game = _Football("401", "DAL", "PHI", SHORT).listed(SHORT).live(SHORT + timedelta(seconds=30), _kickoff())
    lines = mixed_decisions(_short(_playing("401"), nfl=tuple(game.frames)), profile=QUAD)
    up = [line.split()[2] for line in lines if line.split()[1] == "up"]
    assert {"mlb:mlb_statsapi:401:live", "nfl:nflmeta:401:live"} <= set(up)


def test_while_mlb_s_source_is_out_its_live_card_dims_then_comes_down() -> None:
    outage = SourceOutage(league=League.MLB, start=SHORT + timedelta(minutes=1), end=SHORT + timedelta(minutes=9))
    lines = mixed_decisions(_short(_playing(), faults=(outage,)), profile=QUAD, nfl=False)
    assert lines[6:] == [  # after the versions header and the first answers
        "    60.0  scope   mlb 2026-11-21  failed",
        "    60.0  scope   mlb 2026-11-22  failed",
        "    75.0  stale   mlb:mlb_statsapi:1:live",  # its last answer, at 45, was current for two cadences
        "   166.0  health  mlb  stale  (no schedule for over 2m: ProviderError: mlb source down)",
        "   195.0  down    mlb:mlb_statsapi:1:live  (no longer current, 150.0s)",  # two minutes after it went stale
        "   195.0  frame   offline  (no feed for a game)",
        "   547.0  health  mlb  fresh",
        "   547.0  scope   mlb 2026-11-22  complete",
        "   547.0  frame   held  (a game in the TV delay)",
        "   562.0  scope   mlb 2026-11-21  complete",
        "   592.0  up      mlb:mlb_statsapi:1:live  (rotation)",
    ]


def test_a_final_whose_feed_never_answers_shows_no_result() -> None:
    silent = _Game("1", away=142, home=114, day=SUN, start=SHORT).listed(SHORT, GameStatus.FINAL)
    lines = mixed_decisions(_short(silent), profile=QUAD, nfl=False)
    assert "     0.0  frame   offline  (no feed for a game)" in lines
    assert not any(":final" in line for line in lines)


def test_a_game_is_on_its_slate_from_its_first_frame() -> None:
    later = SHORT + timedelta(minutes=5)
    timeline = _short(_playing(), nfl=(*_nfl("401", "DAL", "PHI", later),)[1:])  # listed from its kickoff
    for league in League.MLB, League.NFL:
        source = MLB_STATSAPI if league is League.MLB else NFLMETA_SOURCE
        assert len(timeline.slate(league, SUN, later, source=source, asked=later).contests) == 1
    assert timeline.slate(League.NFL, SUN, SHORT, source=NFLMETA_SOURCE, asked=SHORT).contests == ()
    assert timeline.nfl_feed_at(NFL_SHARED, SHORT) == (None, ())  # nor does its feed say anything before it
    assert timeline.slate(League.MLB, SAT, SHORT, source=MLB_STATSAPI, asked=SHORT).contests == ()


def test_the_replay_s_statsapi_answers_pregame_details_and_no_win_probability() -> None:
    clock = FakeClock(SHORT)
    mlb = _ReplayMlb(_short(_playing()), _World(SHORT, clock))
    game = _playing().team_game(GameStatus.LIVE)
    assert mlb.fetch_win_probability(game) is None
    assert mlb.fetch_pregame_details(game) == PregameDetails()


def test_a_card_drawn_with_a_score_its_feed_gave_less_than_a_delay_before_is_an_early_reveal() -> None:
    steps: list[MixedStep] = []

    def watch(step: MixedStep) -> None:
        if step.drawn is not None:
            steps.append(step)

    run_mixed(_short(_playing(), end=timedelta(minutes=2)), profile=QUAD, nfl=False, watch=watch)
    step = steps[0]
    assert step.drawn is not None
    lags, key = {League.MLB: LAG.as_timedelta()}, (step.drawn.contest.id, 1, 0, False)
    early = [EarlyReveal(card=step.drawn.id, at=step.elapsed, away=1, home=0)]
    assert _spoiled(step, {}, lags) == early  # a score no answer carried
    assert _spoiled(step, {key: step.at.monotonic + timedelta(seconds=-44)}, lags) == early  # carried 44 s before
    assert _spoiled(step, {key: step.at.monotonic + timedelta(seconds=-45)}, lags) == []


def test_a_host_that_sleeps_takes_no_step_while_the_world_runs_on_and_wakes_with_its_wall_clock_ahead() -> None:
    """Asleep from 12:02 for 49 minutes, the board's monotonic clock stands still and nothing is asked; it wakes with
    its wall clock at the world's 12:51, and the source answers as of then."""
    game = _playing().live(SHORT + timedelta(minutes=30), _Line(9, _TOP, 3, 0, 8, 5))
    sleep = Wake(at=SHORT + timedelta(minutes=2), slept=timedelta(minutes=49))
    steps: list[MixedStep] = []
    run = run_mixed(
        _short(game, end=timedelta(minutes=55), faults=(sleep,)), profile=QUAD, nfl=False, watch=steps.append
    )
    awake = [step for step in steps if step.world >= sleep.at + sleep.slept]
    assert awake and all(step.at.utc == step.world for step in steps)  # its wall clock jumped with the world
    assert awake[0].elapsed < timedelta(minutes=3) and steps[-1].elapsed < timedelta(minutes=7)
    asked = [fetched.at for fetched in run.fetched]
    assert max(later - earlier for earlier, later in zip(asked, asked[1:])) < timedelta(minutes=1)
    assert run.spent(ProviderAccount.MLB_STATSAPI).requests == len(run.fetched)
    assert (awake[0].store.contests[0].status, awake[-1].world) == (GameStatus.LIVE, SHORT + timedelta(minutes=55))


def _naive_start() -> MixedTimeline:
    return MixedTimeline(zone=ZONE, start=SHORT.replace(tzinfo=None), end=SHORT, mlb=tuple(_playing().frames))


def _mlb_among_nfl() -> MixedTimeline:
    return _short(_playing(), nfl=(SlateFrame(at=SHORT, game=_playing().team_game(GameStatus.LIVE)),))


def _undated() -> MixedTimeline:
    game = dataclasses.replace(_playing().team_game(GameStatus.SCHEDULED), provider_date=None)
    return MixedTimeline(
        zone=ZONE, start=SHORT, end=SHORT + timedelta(minutes=1), mlb=(GameFrame(at=SHORT, game=game),)
    )


def _live_corrected() -> MixedTimeline:
    return _short(_playing(), faults=(Correction(at=SHORT, game=_mlb_id("1"), away=2, home=0),))


def _naive_frame() -> SlateFrame:
    return SlateFrame(at=SHORT.replace(tzinfo=None), game=_playing().team_game(GameStatus.LIVE))


def _backward_outage() -> SourceOutage:
    return SourceOutage(league=League.NFL, start=SHORT, end=SHORT - timedelta(seconds=1))


@pytest.mark.parametrize(
    ("build", "match"),
    [
        (_naive_start, "timezone-aware"),
        (_backward_outage, "must end after it starts"),
        (_mlb_among_nfl, "listed among nfl's games"),
        (_undated, "needs the date its slate files it under"),
        (_live_corrected, "corrects a final"),
        (_naive_frame, "timezone-aware"),
        (lambda: Wake(at=SHORT, slept=timedelta(0)), "a sleep must last"),
    ],
)
def test_a_timeline_that_can_t_be_replayed_is_refused(build: Callable[[], object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        build()


# --- a step of the board's wall clock, under a final on air ----------------------------------------------------------


def _final_day(end: timedelta, *faults: ClockStep) -> list[str]:
    """A game final at 12:02, its result on air from 165 s until ``end``, with ``faults``."""
    game = _playing().final(SHORT + timedelta(minutes=2), _Line(9, _BOTTOM, 1, 0, 6, 6, 3))
    return mixed_decisions(_short(game, end=end, faults=faults), profile=QUAD, nfl=False)


def _step_at_12_10(by: timedelta) -> ClockStep:
    """The board's wall clock stepping by ``by`` at 12:10, with the final on air."""
    return ClockStep(at=SHORT + timedelta(minutes=10), by=by)


def test_a_wall_clock_step_back_leaves_the_final_on_air_up() -> None:
    lines = _final_day(timedelta(minutes=20), _step_at_12_10(timedelta(minutes=-10)))
    assert not any("frame   idle" in line for line in lines)
    assert lines == _final_day(timedelta(minutes=20))  # the board decides as if the clock hadn't stepped


def test_a_wall_clock_step_ahead_leaves_a_final_its_whole_post_game_window() -> None:
    lines = _final_day(timedelta(minutes=40), _step_at_12_10(timedelta(minutes=10)))
    [down] = [line for line in lines if "  down    mlb:mlb_statsapi:1:final" in line]
    # Its 1800 s window runs from its reveal at 165 s; it airs until too little is left for a turn's 8 s minimum.
    assert down == "  1957.0  down    mlb:mlb_statsapi:1:final  (ran its time, 1792.0s)"
    assert lines == _final_day(timedelta(minutes=40))  # the board decides as if the clock hadn't stepped
