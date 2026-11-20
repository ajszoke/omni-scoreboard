"""NBA replay: a night of BALLDONTLIE's answers, run through the shared board's NBA path.

The fetch answers as the feed did at each moment of the replay's clock (the tests' `RecordedNight` serves the recorded
games that way): whatever the board asks, whenever it asks. The runner (`run_nba`) wires the production spine around
it, as the running app does with ``--leagues nba``: the quota ledger at the tier's limits, the board's `PollPlan` with
NBA's policies (`omni.app.runner.board_plan`), BALLDONTLIE behind it (`omni.app.runner.planned_nba_source`, through
`MeteredBdl`), an inline `Poller`, and an `AppLoop` built by `build_planned_loop` with NBA alone. It warms up as the
running board does, then steps at every instant something can change: the plan's next ask, on the poller's own
quarter-second beat (an ask the ledger holds back stays due, and is checked again a beat later, as the running poller
checks it), and the loop's own deadlines (a read clearing the delay, the bottom panel's next half, a card's turn), each
just past. Every step refreshes, then paints; the display takes each
frame as it's drawn, and the renderers are chosen and checked but not drawn.

**The decisions** (`nba_decisions`), one line per change, timed in seconds since the start on the running clock:
- the board's lines, as `omni.replay.harness.replay_decisions` writes them: ``up``, ``down``, ``drop``, ``frame``,
  ``unseen``, ``note``, ``stale`` and ``final``;
- ``panel``: the bottom panel of the NBA live card on the board changing, while it's up: a caption by its kind, with
  its place in the queue (``2/4``) or ``alone``, or a stat panel (``line score``, ``team box``, ``players #2``);
- ``break``: a break the policy's view gains (its kind and where in the game), and ``ended`` when its end clears the
  delay;
- ``skip``: what a pass set aside: a caption dropped unseen, an outage's catch-up;
- ``say``: what the source said in the log;
- under `omni-board-policy/2`, what the policy decided (`omni.replay.policy_lines`), its break leases among it.

What it measures (`NbaRun`): the requests BALLDONTLIE got, by kind.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from omni.app.poller import Poller
from omni.app.quota import BalldontliePlan, QuotaLedger, account_limits
from omni.app.runner import NbaSource, board_plan, build_planned_loop, planned_nba_source, warm_up
from omni.cards.base import CardKind, ScoreboardCard
from omni.cards.basketball import BasketballGameCardPayload, BottomPanel, CaptionPanel, PlayerLinesPanel
from omni.core.clock import FakeClock
from omni.core.enum import PanelProfile
from omni.core.time import DurationSeconds
from omni.domain.breaks import BreakId
from omni.policy.settings import PolicySettings
from omni.policy.view import PolicyView
from omni.preferences.favorites import Favorites
from omni.providers.balldontlie import BdlFetch
from omni.renderers.canvas import Canvas, RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.registry import RendererRegistry, default_registry
from omni.panels.geometry import geometry_for
from omni.replay.harness import _describe, replay_versions
from omni.replay.policy_lines import policy_line
from omni.tower.lane import earliest

__all__ = ["NbaRun", "nba_decisions", "run_nba"]

_NUDGE = timedelta(milliseconds=1)  # just past a deadline, so spans measured in floating point have surely run
_LONGEST_STEP = timedelta(seconds=30)  # a step never runs longer, so nothing is missed between asks
_POLLER_BEAT = timedelta(milliseconds=250)  # the running poller checks the plan this often


@dataclass(frozen=True, slots=True)
class NbaRun:
    """What one replay decided, and the requests BALLDONTLIE got, by path and the kind of read."""

    decisions: tuple[str, ...]
    requests: tuple[tuple[str, int], ...]


@dataclass(slots=True)
class _Seen:
    """What the replay has written about so far: the panel on the board, the breaks in the view, a stale card."""

    panel: tuple[str, str] | None = None
    breaks: set[BreakId] = field(default_factory=set)
    stale: str | None = None


def nba_decisions(
    fetch: BdlFetch,
    clock: FakeClock,
    *,
    until: datetime,
    profile: PanelProfile = PanelProfile.QUAD_128X64,
    tier: BalldontliePlan = BalldontliePlan.ALL_STAR,
    delay: DurationSeconds = DurationSeconds(0),
    favorites: Favorites = Favorites(),
    settings: PolicySettings | None = None,
) -> list[str]:
    """Every change on the board across the night, one line each, under a header naming what the replay decided with
    (`replay_versions`, with NBA's delay and tier)."""
    run = run_nba(
        fetch, clock, until=until, profile=profile, tier=tier, delay=delay, favorites=favorites, settings=settings
    )
    header = replay_versions(profile, favorites=favorites, settings=settings, nba_delay=delay, nba_plan=tier).header()
    return [header, *run.decisions]


def run_nba(
    fetch: BdlFetch,
    clock: FakeClock,
    *,
    until: datetime,
    profile: PanelProfile = PanelProfile.QUAD_128X64,
    tier: BalldontliePlan = BalldontliePlan.ALL_STAR,
    delay: DurationSeconds = DurationSeconds(0),
    favorites: Favorites = Favorites(),
    settings: PolicySettings | None = None,
    watch: Callable[[ScoreboardCard[Any]], None] | None = None,
) -> NbaRun:
    """Run the board's NBA path from the clock's moment to ``until`` over ``fetch``, which answers by the same clock,
    and say what it decided. ``tier`` paces the reads, ``delay`` is NBA's TV delay, ``favorites`` the board's, and
    ``settings`` the board policy's (without them the board decides by the queue's bands). ``watch`` sees each card
    the board draws. Pure and deterministic."""
    start = clock.now()
    lines: list[str] = []
    asked: dict[str, int] = {}

    def counted(path: str, query: Any) -> Any:
        kind = "season" if any(name == "seasons[]" for name, _ in query) else path
        asked[kind] = asked.get(kind, 0) + 1
        return fetch(path, query)

    ledger = QuotaLedger(account_limits(balldontlie=tier))
    plan = board_plan(ledger=ledger, mlb=None, nfl=False, nba=tier)
    said: list[str] = []
    planned = planned_nba_source(plan, clock, counted, ledger=ledger, tier=tier, log=said.append)
    drawn = _Drawn(watch)
    loop = build_planned_loop(
        None,
        _Discarding(profile),
        favorites=favorites,
        registry=drawn,
        nba=NbaSource.of(planned),
        nba_delay=delay,
        settings=settings,
    )
    poller = Poller(plan, clock)
    warm_up(loop, poller, clock)
    seen = _Seen()
    while clock.now() <= until:
        poller.run_due()
        at = clock.read()
        refresh = loop.refresh(at)
        painted = loop.paint(at)
        stamp = f"{(at.utc - start).total_seconds():8.1f}"
        lines.extend(f"{stamp}  say     {line}" for line in said)
        said.clear()
        lines.extend(f"{stamp}  skip    {line}" for line in refresh.pipeline.skipped)
        lines.extend(f"{stamp}  unseen  {missed.card.id.raw}  ({missed.reason.value})" for missed in refresh.unseen)
        for card_id, ended in refresh.pipeline.ends:
            since = (ended.at - start).total_seconds()
            lines.append(f"{stamp}  final   {card_id.raw}  (ended {since:.1f}, {ended.basis.value})")
        lines.extend(f"{stamp}  {line}" for line in _breaks(refresh.policy, seen))
        chosen = painted.presented.reason if painted.presented is not None else None
        lines.extend(
            f"{stamp}  {line}" for line in map(lambda change: _describe(change, chosen), painted.changes) if line
        )
        lines.extend(f"{stamp}  note    {note.value}" for note in painted.notes)
        stale = painted.shown.raw if painted.shown is not None and painted.stale else None
        if stale is not None and stale != seen.stale:
            lines.append(f"{stamp}  stale   {stale}")
        seen.stale = stale
        panel = _panel(drawn.card) if painted.shown is not None else None
        if panel is not None and panel != seen.panel:
            lines.append(f"{stamp}  panel   {panel[0]}  {panel[1]}")
        seen.panel = panel
        lines.extend(f"{stamp}  {policy_line(decision)}" for decision in painted.decisions)
        deadline, due = loop.next_deadline(clock.read()), plan.next_due()
        soonest = earliest(
            (
                deadline + _NUDGE if deadline is not None else None,
                max(due, at.monotonic + _POLLER_BEAT) if due is not None else None,  # the poller's own beat
                at.monotonic + _LONGEST_STEP,
            )
        )
        assert soonest is not None
        clock.advance(max(soonest.since(at.monotonic), _NUDGE))
    return NbaRun(decisions=tuple(lines), requests=tuple(sorted(asked.items())))


def _breaks(view: PolicyView, seen: _Seen) -> list[str]:
    """The breaks the policy's view gained since the last step, and those whose end it now shows."""
    lines: list[str] = []
    for facts in view.games.values():
        for evidence in facts.breaks:
            if evidence.ended and evidence.id in seen.breaks:
                seen.breaks.discard(evidence.id)
                lines.append(f"break   {evidence.id.contest} {evidence.kind.value} @ {evidence.id.marker}  (ended)")
            elif not evidence.ended and evidence.id not in seen.breaks:
                seen.breaks.add(evidence.id)
                lines.append(f"break   {evidence.id.contest} {evidence.kind.value} @ {evidence.id.marker}")
    return lines


def _panel(card: ScoreboardCard[Any] | None) -> tuple[str, str] | None:
    """The bottom panel of the NBA live card just drawn, as the card's game and a few words."""
    if card is None or card.kind is not CardKind.LIVE_GAME or not isinstance(card.payload, BasketballGameCardPayload):
        return None
    return str(card.contest.id), _panel_words(card.payload.bottom)


def _panel_words(bottom: BottomPanel) -> str:
    if isinstance(bottom, CaptionPanel):
        place = f"{bottom.place.index}/{bottom.place.count}" if bottom.place is not None else "alone"
        return f"{type(bottom.caption).__name__} {place}"
    if isinstance(bottom, PlayerLinesPanel):
        return f"players #{bottom.rank + 1}"
    return {"LineScorePanel": "line score", "TeamBoxPanel": "team box"}[type(bottom).__name__]


class _Drawn(RendererRegistry):
    """Today's renderers, chosen and checked as for drawing, but not drawn; it keeps the card last asked for, and
    hands each to ``watch``."""

    def __init__(self, watch: Callable[[ScoreboardCard[Any]], None] | None) -> None:
        super().__init__()
        self._real = default_registry()
        self._watch = watch
        self.card: ScoreboardCard[Any] | None = None

    def render(self, card: ScoreboardCard[Any], context: RenderContext, canvas: Canvas) -> None:
        self._real.renderer_for(card)
        self.card = card
        if self._watch is not None:
            self._watch(card)


class _Discarding:
    """A display that takes every frame and keeps none."""

    def __init__(self, profile: PanelProfile) -> None:
        self._profile = profile
        geometry = geometry_for(profile)
        self._canvas = RecordingCanvas(geometry.width, geometry.height)

    @property
    def profile(self) -> PanelProfile:
        return self._profile

    def new_frame(self) -> Canvas:
        return self._canvas

    def commit(self, frame: Canvas) -> None:
        self._canvas.ops.clear()
