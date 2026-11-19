"""NFLMeta's own words never reach the shared board's trace: a game read through the production path (the provider over
a fake HTTP feed, the shared plan, `LiveFootballPipeline`, `AppLoop` under `omni-board-policy/2`, `DecisionTracer`)
whose plays, injury, late play and failed read all carry one unique marker. The marker reaches the board's captions,
and no trace line, warning or skipped line holds it."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from omni.app.display import RecordingDisplaySink
from omni.app.planned_nfl import NFL_POLICIES, PlannedNfl, nfl_accounts
from omni.app.poll_plan import PollPlan
from omni.app.poller import Poller
from omni.app.quota import (
    HttpAnswer,
    MeteredHttp,
    Meter,
    NflMetaPlan,
    ProviderAccount,
    QuotaLedger,
    account_limits,
    nflmeta_billed,
)
from omni.app.runner import NflSource, build_loop
from omni.app.trace import DecisionTracer
from omni.cards.football import LiveFootballCardPayload
from omni.core.clock import FakeClock
from omni.core.enum import PanelProfile
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.policy.settings import PolicySettings
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import NFLMETA_SOURCE, NflMetaLiveProvider
from tests.test_planned_nfl import CAR, SUNDAY, _et, _NflMeta, _row

MARKER = "Q.Zyxwvut"  # reads as a player's name, so it lands in a play's words, its players' line and an injury
GAME_ID = 101
KICKOFF = _et(SUNDAY, 13)
S = timedelta(seconds=1)


class _Feed(_NflMeta):
    """NFLMeta as a fake whose play reads fail, with the marker in their message, while ``failing``."""

    failing = False

    def __call__(self, path: str, query: Mapping[str, str | int]) -> HttpAnswer:
        if self.failing and path.endswith("/play-changes"):
            self.requests.append((self.clock.now(), path, dict(query)))
            raise OSError(f"timed out reading {MARKER}")
        return super().__call__(path, query)


def _run(feed: _Feed, number: int, yards: int) -> dict[str, Any]:
    text = f"{MARKER} up the middle to ATL {40 - yards} for {yards} yards (A.Bcd)."
    play = {"play_number": number, "play_type": "rush", "quarter": 1, "description": text}
    return {"source_play_id": f"p{number}", "revision": 1, "changed_at": feed.clock.now().isoformat(), "play": play}


def _never(game: TeamGame, now: datetime) -> LiveBaseballFeed:  # pragma: no cover - the board has no MLB game
    raise AssertionError("no baseball here")


def test_the_feeds_words_reach_the_captions_and_never_the_shared_trace() -> None:
    clock = FakeClock(KICKOFF - 10 * S)
    feed = _Feed(clock)
    feed.rows[GAME_ID] = _row(GAME_ID, "CAR", "ATL", KICKOFF)
    ledger = QuotaLedger(account_limits(NflMetaPlan.BUILDER))
    plan = PollPlan(NFL_POLICIES, ledger=ledger, accounts=nfl_accounts())
    meter = Meter(ledger, ProviderAccount.NFLMETA, clock, billed=nflmeta_billed)
    provider = NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=MeteredHttp(feed, meter))
    planned = PlannedNfl(plan, clock, provider, favorites=CAR)
    loop = build_loop(
        None,
        _never,
        RecordingDisplaySink(PanelProfile.QUAD_128X64),
        favorites=CAR,
        nfl=NflSource(planned, planned),
        nfl_delay=DurationSeconds(45),
        settings=PolicySettings(),
    )
    poller = Poller(plan, clock)
    lines: list[str] = []
    tracer = DecisionTracer(lines.append)
    said: list[str] = []
    captions: list[str] = []

    for second in range(-10, 240):
        _script(feed, second)
        refresh = loop.refresh(clock.read())
        tracer.refreshed(refresh, took=timedelta(0), next_at=None)
        said += [*refresh.pipeline.warnings, *refresh.pipeline.skipped]
        poller.run_due()
        tracer.painted(loop.paint(clock.read()))
        card = loop._queue.card(f"nfl:nflmeta:{GAME_ID}:live")
        if card is not None and isinstance(card.payload, LiveFootballCardPayload) and card.payload.last_play:
            captions.append(card.payload.last_play.detail or "")
        clock.advance(S)
    assert any(MARKER in caption for caption in captions)  # the words reached the board, as captions
    assert [line for line in lines if MARKER in line] == []
    assert [line for line in said if MARKER in line] == []
    assert any("late play: routine play #4" in line for line in lines)  # the notes reached the trace, in our words
    assert any("play read failed" in line for line in lines)


def _script(feed: _Feed, second: int) -> None:
    """What the feed does ``second`` seconds past kickoff: the game starts with a play the board joins on, a new play
    comes, then one behind it (the feed's late play), an injury, and a play read that fails, then reads that don't."""
    if second == 0:
        feed.set(GAME_ID, phase="in")
        feed.plays[GAME_ID] = [_run(feed, 1, 3)]
    elif second == 30:
        _new_play(feed, _run(feed, 5, 10))
    elif second == 60:
        _new_play(feed, _run(feed, 4, 5))
    elif second == 80:
        feed.set(GAME_ID, injuries=[_injury(feed.clock.now())])
    elif second == 100:
        feed.failing = True
        _new_play(feed, _run(feed, 6, 2))
    elif second == 120:
        feed.failing = False


def _new_play(feed: _Feed, change: dict[str, Any]) -> None:
    feed.plays.setdefault(GAME_ID, []).append(change)
    feed.set(GAME_ID, play_sync={"status": "matched", "last_play_id": change["source_play_id"]})


def _injury(now: datetime) -> dict[str, Any]:
    return {"player_name": MARKER, "team_abbr": "CAR", "injury": "Knee", "state": "out", "reported_at": now.isoformat()}
