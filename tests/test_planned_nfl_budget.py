"""PlannedNfl through the budget replay's typical Sunday, beside the requests and rows strategy E models for it.

The world is the budget replay's (`omni.replay.nfl_budget`): its games, kickoffs, halftime holds, finals, injury
entries and play changes, served as NFLMeta would serve them and billed as `nflmeta_billed` reads its header. The
board is the production one: `PlannedNfl` over a real `PollPlan` and quota ledger, an inline `Poller`, and the
provider behind `MeteredHttp`. Its requests and rows, read by read, are compared with the model's.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime, time, timedelta
from typing import Any

from omni.app.planned_nfl import NFL_POLICIES, PlannedNfl, nfl_accounts
from omni.app.poll_plan import PollPlan
from omni.app.poller import Poller
from omni.app.quota import UNLIMITED, HttpAnswer, MeteredHttp, Meter, ProviderAccount, QuotaLedger, nflmeta_billed
from omni.core.clock import FakeClock
from omni.core.enum import League
from omni.preferences.favorites import Favorites
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import NFLMETA_SOURCE, NflMetaLiveProvider
from omni.replay.nfl_budget import (
    EASTERN,
    STRATEGIES,
    TYPICAL_SUNDAY,
    Game,
    ReadKind,
    month_games,
    replay,
)

NFL = ProviderAccount.NFLMETA
_TEAMS: tuple[str, ...] = ("CAR", "ATL", "PIT", "CIN", "DAL", "NYG", "PHI", "WAS", "GB", "CHI", "DET", "MIN")
_TEAMS += ("KC", "LV", "DEN", "LAC", "BAL", "CLE", "HOU", "IND", "JAX", "TEN", "BUF", "MIA", "NE", "NYJ", "SF", "SEA")


class _ModelSunday:
    """NFLMeta over the budget replay's games, as of the clock: each game's scoreboard row (its phase, its
    halftime, its injury entries, its latest play) and its play changes, billing one row a game listed, one an
    injury entry and one a change. Each request is counted by the read it is, with the rows it billed."""

    def __init__(self, clock: FakeClock, games: list[Game]) -> None:
        self.clock = clock
        self.games = {1000 + n: game for n, game in enumerate(games)}
        self.teams = {game_id: (_TEAMS[2 * n], _TEAMS[2 * n + 1]) for n, game_id in enumerate(self.games)}
        self.favorite = next(game_id for game_id, game in self.games.items() if game.favorite)
        self.changes: dict[int, list[tuple[datetime, dict[str, Any]]]] = {}
        for game_id, game in self.games.items():
            changes = self.changes[game_id] = []
            for number, (written, rows) in enumerate(game.plays(), start=1):
                for revision in range(1, rows + 1):
                    play = {"play_number": number, "play_type": "rush", "quarter": 1, "description": "Run."}
                    change = {"source_play_id": f"p{number}", "revision": revision, "play": play}
                    changes.append((written, {**change, "changed_at": written.isoformat()}))
        self.requests: Counter[ReadKind] = Counter()
        self.rows: Counter[ReadKind] = Counter()

    def __call__(self, path: str, query: Mapping[str, str | int]) -> HttpAnswer:
        now = self.clock.now()
        if path.endswith("/play-changes"):
            written = [change for at, change in self.changes[int(path.split("/")[4])] if at <= now]
            start = int(query.get("cursor", 0))
            page = written[start : start + int(query["limit"])]
            meta = {"has_more": start + len(page) < len(written), "next_cursor": str(start + len(page))}
            return self._billed(ReadKind.FAVORITE_PLAYS, {"data": page, "meta": meta}, len(page))
        if "game_ids" in query:
            named = {int(each) for each in str(query["game_ids"]).split(",")}
            listed = [game_id for game_id in named if self.games[game_id].live(now)]
            kind = ReadKind.LIVE_GAMES
        elif "game_id" in query:
            listed = [int(query["game_id"])]
            kind = ReadKind.FAVORITE if listed == [self.favorite] else ReadKind.DRAIN
        else:
            listed, kind = list(self.games), ReadKind.DAY_LIST
        rows = [self._row(game_id, now) for game_id in sorted(listed)]
        billed = len(rows) + sum(len(row["injuries"]) for row in rows)
        return self._billed(kind, {"data": rows, "meta": {"recommended_poll_seconds": 10}}, billed)

    def _billed(self, kind: ReadKind, body: Any, rows: int) -> HttpAnswer:
        self.requests[kind] += 1
        self.rows[kind] += rows
        return HttpAnswer(body, {"x-nflmeta-response-rows": str(rows)})

    def _row(self, game_id: int, now: datetime) -> dict[str, Any]:
        """The game's scoreboard as of ``now``. It moves only with a play, so a scoreboard that moves always has a
        new play behind it, as the model's reads on change assume."""
        game = self.games[game_id]
        away, home = self.teams[game_id]
        plays = sum(1 for at, change in self.changes[game_id] if at <= now and change["revision"] == 1)
        phase = "pre" if now < game.kickoff else "post" if now >= game.final else "in"
        start, end = game.halftime
        detail = "Final" if phase == "post" else "Halftime" if start <= now < end else None
        synced = phase == "in" and plays > 0
        injury = {"player_name": "A. Player", "team_abbr": home, "injury": "Knee", "state": "questionable_to_return"}
        return {
            "game_id": game_id,
            "kickoff_at": game.kickoff.isoformat(),
            "away_team": {"abbr": away, "score": 0},
            "home_team": {"abbr": home, "score": 0},
            "phase": phase,
            "period": 1 + min(plays * 4 // 275, 3) if phase != "pre" else 0,
            "clock": f"{14 - plays % 15}:{59 - plays % 60:02d}",
            "status_detail": detail,
            "situation": {"possession_abbr": home, "down": 1, "distance": 10, "text": "1st & 10", "yardline_100": 75},
            "play_sync": {
                "status": "matched" if synced else "unknown",
                "last_play_id": f"p{plays}" if synced else None,
            },
            "week": 11,
            "injuries": [injury] * game.injuries(now),
        }


def _sunday_through_planned_nfl() -> tuple[_ModelSunday, int]:
    """The typical Sunday read by the production board from an hour before its first kickoff to midnight; returns
    the world, with its counts, and the requests the ledger counted."""
    games = [game for game in month_games() if game.day == TYPICAL_SUNDAY]
    start = min(game.kickoff for game in games) - timedelta(hours=1)
    end = datetime.combine(TYPICAL_SUNDAY + timedelta(days=1), time(), EASTERN)
    clock = FakeClock(start)
    world = _ModelSunday(clock, games)
    ledger = QuotaLedger({NFL: UNLIMITED})
    plan = PollPlan(NFL_POLICIES, ledger=ledger, accounts=nfl_accounts())
    meter = Meter(ledger, NFL, clock, billed=nflmeta_billed)
    provider = NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=MeteredHttp(world, meter))
    planned = PlannedNfl(plan, clock, provider, favorites=Favorites({League.NFL: ["CAR"]}))
    poller = Poller(plan, clock)
    kickoffs = sorted({game.kickoff for game in games})
    while clock.now() < end:
        planned.observe(clock.read(), ())
        poller.run_due()
        planned.observe(clock.read(), ())
        due = plan.next_due()
        if due is not None and due <= clock.monotonic():
            continue  # a read wanted just now (a final's) goes out at once
        now = clock.read()
        ahead = [now.utc + due.since(now.monotonic)] if due is not None else []
        ahead += [kickoff for kickoff in kickoffs if kickoff > now.utc]
        hold = provider.hold_ends(world.favorite)
        if hold is not None and hold > now.monotonic:
            ahead.append(now.utc + hold.since(now.monotonic))
        clock.advance(min([*ahead, end]) - now.utc)
    return world, ledger.usage(NFL, clock.read()).requests


def test_planned_nfl_reads_a_typical_sunday_as_strategy_e_models_it_but_for_reads_the_model_leaves_out() -> None:
    games = [game for game in month_games() if game.day == TYPICAL_SUNDAY]
    model = replay(games, next(strategy for strategy in STRATEGIES if strategy.key == "E"), UNLIMITED)
    assert (model.requests, model.rows) == (2_144, 11_324)  # the budget report's Sunday for E
    world, metered = _sunday_through_planned_nfl()
    modeled = model.asked
    assert dict(world.requests) == {
        # Once before the first kickoff, and again half an hour past each window's kickoffs while a later one is
        # ahead (13:30 and 16:55): the schedule as it moves. The model lists the day once.
        ReadKind.DAY_LIST: modeled[ReadKind.DAY_LIST] + 2,
        # The model starts the hold at halftime; the board starts it on the read that finds halftime.
        ReadKind.FAVORITE: modeled[ReadKind.FAVORITE] + 1,
        # The first look at kickoff, and a read each poll through the final's drain: none of them bills a row.
        ReadKind.FAVORITE_PLAYS: modeled[ReadKind.FAVORITE_PLAYS] + 13,
        # The model stops at each window's last final; the board learns of it from the read that no longer lists it.
        ReadKind.LIVE_GAMES: modeled[ReadKind.LIVE_GAMES] + 2,
        ReadKind.DRAIN: modeled[ReadKind.DRAIN],  # one for each other final
    }
    assert dict(world.rows) == {  # the model's rows by kind, but for the read that finds halftime (a game, an injury)
        ReadKind.DAY_LIST: 14 + 14 + 52,  # the day's 14 games each time, and by 16:55 the early finals' injuries
        ReadKind.FAVORITE: 2_720 + 2,
        ReadKind.FAVORITE_PLAYS: 280,
        ReadKind.LIVE_GAMES: 8_245,
        ReadKind.DRAIN: 65,
    }
    requests, rows = sum(world.requests.values()), sum(world.rows.values())
    assert (requests, rows) == (model.requests + 18, model.rows + 68) == (2_162, 11_392)
    assert requests <= model.requests * 1.01 and rows <= model.rows * 1.01  # within 1% of the model's budget
    assert metered == requests  # every request reached the ledger
