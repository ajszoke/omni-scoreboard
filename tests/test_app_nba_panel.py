"""The NBA live card's bottom panel: a play alone holds its turn, two or more run 6 s each numbered i/N with N growing,
a timeout or a ruling holds a whole turn, the stat rotation in its order (the stat panels alone through a timeout, the
line score alone without the players' lines), every change on the card's own half boundaries, and a caption that no
confirmed frame carried by its latest start is dropped, never shown late."""

from __future__ import annotations

from datetime import timedelta

from omni.app.nba_panel import BottomPanelQueue, PanelPolicy, PanelView, QueuedCaption, StatPanel, whole_turn
from omni.cards.basketball import (
    BottomPanel,
    CaptionPanel,
    LineScorePanel,
    PlayerLinesPanel,
    QueuePlace,
    TeamBoxPanel,
)
from omni.core.clock import MonotonicTime
from omni.core.enum import HomeAway
from omni.domain.basketball import BySide, PlayerLine
from omni.events.basketball import (
    BasketballCaption,
    ChallengeWon,
    FieldGoalMade,
    PersonalFoul,
    ShotValue,
    TimeoutTaken,
)
from tests.basketball_fixtures import player

A, H = HomeAway.AWAY, HomeAway.HOME
S = timedelta(seconds=1)
T0 = MonotonicTime(1000.0)
_AWAY = tuple(player(f"A.Away{n}", 10 - n, n, n + 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1) for n in range(3))
_HOME = tuple(player(f"H.Home{n}", 5 - n, n, n + 2, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0) for n in range(7))
LINES: BySide[tuple[PlayerLine, ...]] = BySide(_AWAY, _HOME)
PLAY = PanelView(live=True, timeout=False, lines=LINES, shown=T0 + 3600 * S)


def _basket(n: int) -> FieldGoalMade:
    return FieldGoalMade(side=A, shooter=f"A.Shooter{n}", value=ShotValue.TWO)


def _queued(caption: BasketballCaption, release: MonotonicTime, order: int = 1, **changes: object) -> QueuedCaption:
    start, end = PanelPolicy().bounds(caption)
    fields: dict[str, object] = {
        "order": order,
        "caption": caption,
        "release": release,
        "latest_start": release + start,
        "hard_end": release + end,
    }
    fields.update(changes)
    return QueuedCaption(**fields)  # type: ignore[arg-type]


class _Card:
    """A card's turn on the board, frame by frame: each frame drawn shows the panel's bottom, and is confirmed."""

    def __init__(self, panel: BottomPanelQueue | None = None) -> None:
        self.panel = panel if panel is not None else BottomPanelQueue()
        self.occurrence = 0
        self.shown: list[tuple[float, BottomPanel]] = []

    def up(self) -> None:
        self.occurrence += 1

    def frame(self, at: MonotonicTime, view: PanelView = PLAY) -> BottomPanel:
        """A refresh and a confirmed frame at ``at``: the frame shows what the refresh chose."""
        bottom = self.panel.bottom(at, view)
        self.panel.shown(self.occurrence, at, bottom)
        self.shown.append((at.since(T0).total_seconds(), bottom))
        return bottom

    def run(self, start: float, end: float, view: PanelView = PLAY, every: float = 1.0) -> list[tuple[float, str]]:
        """Frames every ``every`` seconds; each change of the panel, as when and a word for it."""
        changes: list[tuple[float, str]] = []
        last: str | None = None
        at = start
        while at < end:
            word = _word(self.frame(T0 + at * S, view))
            if word != last:
                changes.append((at, word))
            last = word
            at += every
        return changes


def _word(bottom: BottomPanel) -> str:
    if isinstance(bottom, CaptionPanel):
        place = f" {bottom.place.index}/{bottom.place.count}" if bottom.place is not None else ""
        return f"{getattr(bottom.caption, 'shooter', type(bottom.caption).__name__)}{place}"
    if isinstance(bottom, PlayerLinesPanel):
        return f"players {bottom.rank}"
    return "line score" if isinstance(bottom, LineScorePanel) else "team box"


# --- the rotation ------------------------------------------------------------------------------------------------


def test_with_no_play_waiting_the_rotation_runs_a_turn_each_in_its_order_and_walks_the_players_down() -> None:
    card = _Card()
    card.up()
    assert card.run(0, 96) == [
        (0, "line score"),
        (12, "team box"),
        (24, "players 0"),
        (36, "line score"),
        (48, "team box"),
        (60, "players 1"),
        (72, "line score"),
        (84, "team box"),
    ]


def test_the_players_panel_walks_no_deeper_than_its_policy_or_the_longer_roster() -> None:
    card = _Card(BottomPanelQueue(PanelPolicy(player_ranks=2)))
    card.up()
    ranks = [word for _, word in card.run(0, 12 * 9) if word.startswith("players")]
    assert ranks == ["players 0", "players 1", "players 0"]
    short = _Card(BottomPanelQueue(PanelPolicy(player_ranks=9)))
    short.up()
    view = PanelView(live=True, timeout=False, lines=BySide(_AWAY, _AWAY[:2]), shown=PLAY.shown)
    assert [word for _, word in short.run(0, 12 * 15, view) if word.startswith("players")] == [
        "players 0",
        "players 1",
        "players 2",
        "players 0",
        "players 1",
    ]


def test_through_a_timeout_the_stat_panels_rotate_and_without_lines_the_line_score_is_all() -> None:
    timeout = PanelView(live=True, timeout=True, lines=LINES, shown=PLAY.shown)
    card = _Card()
    card.up()
    assert card.run(0, 48, timeout) == [(0, "team box"), (12, "players 0"), (24, "team box"), (36, "players 1")]
    slate = PanelView(live=True, timeout=False, lines=None, shown=PLAY.shown)
    alone = _Card()
    alone.up()
    assert alone.run(0, 48, slate) == [(0, "line score")]


def test_a_box_score_whose_lines_go_away_mid_turn_shows_the_line_score() -> None:
    card = _Card()
    card.up()
    card.run(0, 12)  # the line score's turn
    assert isinstance(card.frame(T0 + 12 * S), TeamBoxPanel)
    assert isinstance(
        card.frame(T0 + 13 * S, PanelView(live=True, timeout=False, lines=None, shown=None)), LineScorePanel
    )


def test_the_rotation_picks_up_where_it_left_off_on_the_card_s_next_turn() -> None:
    card = _Card()
    card.up()
    card.run(0, 12)  # its first turn: the line score
    card.panel.off_air()
    card.panel.bottom(T0 + 30 * S, PLAY)  # off the board, the line score's turn ran out
    card.up()
    assert card.run(40, 52) == [(40, "team box")]


# --- the play queue --------------------------------------------------------------------------------------------


def test_one_play_holds_a_whole_turn_with_no_marker() -> None:
    card = _Card()
    card.panel.offer(_queued(_basket(1), T0))
    card.up()
    assert card.run(0, 24) == [(0, "A.Shooter1"), (12, "line score")]


def test_two_or_more_waiting_run_6_s_each_numbered_and_n_grows_as_plays_arrive() -> None:
    card = _Card()
    for n in (1, 2):
        card.panel.offer(_queued(_basket(n), T0, order=n))
    card.up()
    changes = card.run(0, 3)
    for n in (3, 4):  # two more arrive during the first
        card.panel.offer(_queued(_basket(n), T0 + 3 * S, order=n))
    changes += card.run(3, 36)
    assert changes == [
        (0, "A.Shooter1 1/2"),
        (3, "A.Shooter1 1/4"),
        (6, "A.Shooter2 2/4"),
        (12, "A.Shooter3 3/4"),
        (18, "A.Shooter4 4/4"),
        (24, "line score"),
    ]


def test_a_timeout_and_a_ruling_hold_a_whole_turn_even_in_a_run() -> None:
    card = _Card()
    card.panel.offer(_queued(_basket(1), T0, order=1))
    card.panel.offer(_queued(TimeoutTaken(side=H, left=5), T0, order=2))
    card.panel.offer(_queued(ChallengeWon(side=H), T0, order=3))
    card.up()
    assert card.run(0, 36) == [
        (0, "A.Shooter1 1/3"),
        (6, "TimeoutTaken 2/3"),
        (18, "ChallengeWon 3/3"),
        (30, "line score"),
    ]
    assert whole_turn(TimeoutTaken(side=H, left=5)) and not whole_turn(_basket(1))


def test_a_lone_play_whose_run_partner_has_gone_shows_with_no_marker() -> None:
    card = _Card()
    card.panel.offer(_queued(_basket(1), T0, order=1))
    card.panel.offer(_queued(_basket(2), T0, order=2, after=T0 + 99999 * S))  # its slate never catches up
    card.up()
    assert card.run(0, 6) == [(0, "A.Shooter1")]  # the second can't go up beside this score


def test_a_stat_panel_gives_way_to_a_play_at_the_first_half_boundary_after_it_has_had_a_half() -> None:
    card = _Card()
    card.up()
    card.run(0, 3)  # the line score went up at 0
    card.panel.offer(_queued(_basket(1), T0 + 3 * S))
    assert card.panel.next_change(T0 + 3 * S, PLAY) == T0 + 12 * S  # not seen waiting yet
    changes = card.run(3, 5)
    assert card.panel.next_change(T0 + 5 * S, PLAY) == T0 + 6 * S  # seen at 3 s, in its first half: at 6 s
    changes += card.run(5, 15)
    assert changes == [(3, "line score"), (5, "line score"), (6, "A.Shooter1")]
    late = _Card()
    late.up()
    late.run(0, 7)
    late.panel.offer(_queued(_basket(1), T0 + 7 * S))
    late.run(7, 8)
    assert late.panel.next_change(T0 + 8 * S, PLAY) == T0 + 12 * S  # seen in its second half: at its end


def test_a_play_cut_by_a_break_or_past_its_hard_end_goes_and_the_rotation_takes_over() -> None:
    card = _Card()
    card.panel.offer(_queued(_basket(1), T0))
    card.up()
    card.frame(T0)
    end = PanelView(live=False, timeout=False, lines=LINES, shown=PLAY.shown)
    assert isinstance(card.frame(T0 + 2 * S, end), LineScorePanel)
    hard = _Card()
    hard.panel.offer(_queued(_basket(2), T0, hard_end=T0 + 5 * S))  # a policy whose hard end comes this soon
    hard.up()
    assert hard.run(0, 8) == [(0, "A.Shooter2"), (5, "line score")]


def test_a_caption_waits_for_a_slate_read_no_older_than_the_lines_that_told_it() -> None:
    card = _Card()
    card.panel.offer(_queued(_basket(1), T0, after=T0 + 5 * S))
    card.up()
    old = PanelView(live=True, timeout=False, lines=LINES, shown=T0 + 4 * S)
    assert card.run(0, 12, old) == [(0, "line score")]
    none = PanelView(live=True, timeout=False, lines=LINES, shown=None)
    assert card.run(12, 13, none) == [(12, "team box")]
    assert card.run(18, 25) == [(18, "team box"), (24, "A.Shooter1")]  # first seen waiting at 18 s


# --- staging -------------------------------------------------------------------------------------------------


def test_a_caption_no_confirmed_frame_carried_by_its_latest_start_is_dropped_and_said_so() -> None:
    panel = BottomPanelQueue()
    waiting = _queued(_basket(1), T0)
    panel.offer(waiting)
    panel.bottom(T0, PLAY)  # proposed, but the card isn't up
    assert panel.next_change(T0, PLAY) == T0 + 60 * S
    assert isinstance(panel.bottom(T0 + 60 * S, PLAY), LineScorePanel)
    assert panel.missed() == [waiting] and panel.missed() == []


def test_a_frame_carrying_a_caption_past_its_latest_start_starts_nothing() -> None:
    panel = BottomPanelQueue()
    panel.offer(_queued(_basket(1), T0))
    bottom = panel.bottom(T0 + 59 * S, PLAY)
    panel.shown(1, T0 + 60 * S, bottom)  # confirmed a second too late
    assert isinstance(panel.bottom(T0 + 61 * S, PLAY), LineScorePanel)
    assert len(panel.missed()) == 1


def test_a_frame_of_another_panel_starts_nothing_and_a_slot_up_holds_through_later_frames() -> None:
    panel = BottomPanelQueue()
    panel.offer(_queued(_basket(1), T0))
    panel.bottom(T0, PLAY)
    panel.shown(1, T0, LineScorePanel())  # a frame drawn before the caption was chosen
    panel.shown(1, T0 + S, panel.bottom(T0 + S, PLAY))
    panel.shown(1, T0 + 2 * S, panel.bottom(T0 + 2 * S, PLAY))
    assert isinstance(panel.bottom(T0 + 3 * S, PLAY), CaptionPanel)


def test_the_card_leaving_mid_slot_ends_its_run_and_its_next_turn_starts_the_slot_up_afresh() -> None:
    card = _Card()
    for n in (1, 2, 3):
        card.panel.offer(_queued(_basket(n), T0, order=n))
    card.up()
    card.run(0, 7)  # 1/3, then 2/3 at 6 s
    card.panel.off_air()  # another card's frame
    assert card.panel.next_change(T0 + 8 * S, PLAY) == T0 + 60 * S  # off the board: only the latest starts
    card.panel.bottom(T0 + 20 * S, PLAY)  # a refresh while it's off: 2/3 ran out, the third is next
    card.up()
    assert card.run(24, 37) == [(24, "A.Shooter3"), (36, "line score")]  # alone now, a whole turn


def test_a_host_s_gap_clears_the_waiting_and_the_slot_up_but_keeps_the_rotation_s_place() -> None:
    card = _Card()
    card.up()
    card.run(0, 13)  # the line score's turn, then the team box's
    card.panel.offer(_queued(_basket(1), T0 + 13 * S))
    card.panel.reset()
    assert card.panel.next_change(T0 + 13 * S, PLAY) is None
    card.up()
    assert card.run(14, 15) == [(14, "team box")]


def test_while_the_card_is_up_the_panel_wakes_at_its_slot_s_end_and_with_nothing_up_never() -> None:
    card = _Card()
    assert card.panel.next_change(T0, PLAY) is None
    card.up()
    card.frame(T0)
    assert card.panel.next_change(T0 + S, PLAY) == T0 + 12 * S
    fresh = BottomPanelQueue()
    fresh.bottom(T0, PLAY)
    fresh.shown(1, T0, LineScorePanel())
    fresh.bottom(T0 + 13 * S, PLAY)  # the next slot is proposed, not yet shown: it would start in this half
    assert fresh.next_change(T0 + 13 * S, PLAY) == T0 + 24 * S


def test_each_slot_knows_its_own_frames() -> None:
    panel = BottomPanelQueue()
    panel.bottom(T0, PLAY)
    panel.shown(1, T0, PlayerLinesPanel.ranked(0, _AWAY, _HOME))  # not the line score proposed
    assert panel.next_change(T0, PLAY) == T0 + 12 * S  # still proposed: as if it started now
    assert CaptionPanel(caption=_basket(1), place=QueuePlace(1, 2)).place == QueuePlace(1, 2)


def test_the_policy_s_bounds_and_the_rotation_s_order() -> None:
    policy = PanelPolicy()
    assert policy.bounds(_basket(1)) == (60 * S, 90 * S)
    assert policy.bounds(PersonalFoul(side=A, player="A.B", count=1)) == (60 * S, 90 * S)
    assert policy.bounds(TimeoutTaken(side=A, left=3)) == (120 * S, 150 * S)
    assert [panel.following for panel in StatPanel] == [
        StatPanel.TEAM_BOX,
        StatPanel.PLAYER_LINES,
        StatPanel.LINE_SCORE,
    ]
    assert BottomPanelQueue(policy).policy is policy


def test_the_card_back_before_its_slot_ran_out_starts_that_slot_afresh() -> None:
    card = _Card()
    card.panel.offer(_queued(_basket(1), T0))
    card.up()
    card.frame(T0)
    card.panel.off_air()
    card.up()
    assert card.run(4, 17) == [(4, "A.Shooter1"), (16, "line score")]  # its whole turn again, from 4 s
