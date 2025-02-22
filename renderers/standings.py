try:
    from rgbmatrix import graphics
except ImportError:
    from RGBMatrixEmulator import graphics

from data.config.color import Color
from data.config.layout import Layout
from data.standings import Division, League
from utils import center_text_position


def render_standings(canvas, layout: Layout, colors: Color, division: Division, stat):
    league = division.name[:2]  # al or nl
    __fill_bg(canvas, colors, league)
    if canvas.width > 32:
        __render_static_wide_standings(canvas, layout, colors, division, league)
    else:
        return __render_rotating_standings(canvas, layout, colors, division, stat, league)


def __render_rotating_standings(canvas, layout, colors, division, stat, league):
    coords = layout.coords("standings")
    font = layout.font("standings")
    divider_color = get_standings_color_node(colors, "divider", league)
    stat_color = get_standings_color_node(colors, "stat", league)
    team_stat_color = get_standings_color_node(colors, "team.stat", league)
    team_name_color = get_standings_color_node(colors, "team.name", league)
    team_elim_color = get_standings_color_node(colors, "team.elim", league)
    team_clinched_color = get_standings_color_node(colors, "team.clinched", league)

    offset = coords["offset"]

    graphics.DrawLine(canvas, 0, 0, coords["width"], 0, divider_color)

    graphics.DrawText(canvas, font["font"], coords["stat_title"]["x"], offset, stat_color, stat.upper())
    graphics.DrawLine(canvas, coords["divider"]["x"], 0, coords["divider"]["x"], coords["height"], divider_color)

    for team in division.teams:
        graphics.DrawLine(canvas, 0, offset, coords["width"], offset, divider_color)

        team_text = "{:3s}".format(team.team_abbrev)
        stat_text = str(getattr(team, stat))
        color = team_elim_color if team.elim else (team_clinched_color if team.clinched else team_name_color)
        graphics.DrawText(canvas, font["font"], coords["team"]["name"]["x"], offset, color, team_text)
        color = team_elim_color if team.elim else (team_clinched_color if team.clinched else team_stat_color)
        graphics.DrawText(canvas, font["font"], coords["team"]["record"]["x"], offset, color, stat_text)

        offset += coords["offset"]


def __render_static_wide_standings(canvas, layout, colors, division, league):
    coords = layout.coords("standings")
    font = layout.font("standings")
    divider_color = get_standings_color_node(colors, "divider", league)
    team_stat_color = get_standings_color_node(colors, "team.stat", league)
    team_name_color = get_standings_color_node(colors, "team.name", league)
    team_elim_color = get_standings_color_node(colors, "team.elim", league)
    team_clinched_color = get_standings_color_node(colors, "team.clinched", league)
    start = coords.get("start", 0)
    offset = coords["offset"]

    graphics.DrawLine(canvas, 0, start, coords["width"], start, divider_color)

    graphics.DrawLine(
        canvas, coords["divider"]["x"], start, coords["divider"]["x"], start + coords["height"], divider_color
    )

    offset += start

    for team in division.teams:
        graphics.DrawLine(canvas, 0, offset, coords["width"], offset, divider_color)

        color = team_elim_color if team.elim else (team_clinched_color if team.clinched else team_name_color)
        team_text = team.team_abbrev
        graphics.DrawText(canvas, font["font"], coords["team"]["name"]["x"], offset, color, team_text)

        record_text = "{}-{}".format(team.w, team.l)
        record_text_x = center_text_position(record_text, coords["team"]["record"]["x"], font["size"]["width"])

        if "-" in str(team.gb):
            gb_text = " -"
        else:
            gb_text = "{:>4s}".format(str(team.gb))
        half_offset = 3 if "½" in gb_text else 0
        gb_text_x = coords["team"]["games_back"]["x"] - (len(gb_text) * font["size"]["width"]) - half_offset

        color = team_elim_color if team.elim else (team_clinched_color if team.clinched else team_stat_color)
        graphics.DrawText(canvas, font["font"], record_text_x, offset, color, record_text)
        graphics.DrawText(canvas, font["font"], gb_text_x, offset, color, gb_text)

        offset += coords["offset"]


def __fill_bg(canvas, colors, league: str):
    bg_color = get_standings_color_node(colors, "background", league)
    canvas.Fill(bg_color.red, bg_color.green, bg_color.blue)


def get_standings_color_node(colors, node_name, league):
    # try the league-specific color node.
    # If not present, go with the standard "standings"
    try:
        return colors.graphics_color(f"standings.{league.lower()}.{node_name}")
    except KeyError:
        return colors.graphics_color(f"standings.{node_name}")


def render_bracket(canvas, layout, colors, league: League):
    __fill_bg(canvas, colors, league.name)

    coords = layout.coords("standings.postseason")
    font = layout.font("standings")
    team_name_color = get_standings_color_node(colors, "team.name", league.name)
    divider_color = get_standings_color_node(colors, "divider", league.name)

    matchup_gap = coords["matchup_y_gap"]
    winner_offset = matchup_gap // 2
    series_gap = coords["series_x_gap"]
    char_width = font["size"]["width"] + 2

    wc_x = coords["wc_x_start"]
    wc_y = coords["wc_y_start"]
    ds_x = wc_x + series_gap
    ds_a_y = wc_y + winner_offset
    ds_b_y = coords["ds_b_y_start"]
    lcs_x = ds_x + series_gap
    champ_y = (ds_b_y + ds_a_y) // 2 + winner_offset
    champ_x = lcs_x + series_gap

    # draw bracket lines
    # wc divider
    graphics.DrawLine(canvas, wc_x, wc_y, wc_x + series_gap - char_width // 2, wc_y, divider_color)
    # drop down
    graphics.DrawLine(canvas, ds_x - char_width // 2, wc_y, ds_x - char_width // 2, ds_a_y, divider_color)
    # ds a divider
    graphics.DrawLine(
        canvas, ds_x - char_width // 2, ds_a_y, ds_x + series_gap - char_width // 2, ds_a_y, divider_color
    )
    # connect to lcs
    graphics.DrawLine(
        canvas, lcs_x - char_width // 2, ds_a_y, lcs_x - char_width // 2, ds_a_y + winner_offset, divider_color
    )
    # ds b divider
    graphics.DrawLine(canvas, ds_x, ds_b_y, ds_x + series_gap - char_width // 2, ds_b_y, divider_color)
    # connect to lcs
    graphics.DrawLine(
        canvas, lcs_x - char_width // 2, ds_b_y, lcs_x - char_width // 2, ds_b_y - winner_offset, divider_color
    )
    # lcs horizonals
    graphics.DrawLine(
        canvas,
        lcs_x - char_width // 2,
        ds_a_y + winner_offset,
        lcs_x + series_gap - char_width // 2,
        ds_a_y + winner_offset,
        divider_color,
    )
    graphics.DrawLine(
        canvas,
        lcs_x - char_width // 2,
        ds_b_y - winner_offset,
        lcs_x + series_gap - char_width // 2,
        ds_b_y - winner_offset,
        divider_color,
    )
    # champ lines
    graphics.DrawLine(
        canvas,
        champ_x - char_width // 2,
        ds_a_y + winner_offset,
        champ_x - char_width // 2,
        ds_b_y - winner_offset,
        divider_color,
    )
    graphics.DrawLine(
        canvas, champ_x - char_width // 2, champ_y - winner_offset, champ_x, champ_y - winner_offset, divider_color,
    )

    # draw bracket text
    # wc teams
    graphics.DrawText(canvas, font["font"], wc_x, wc_y, team_name_color, league.wc2)
    graphics.DrawText(canvas, font["font"], wc_x, wc_y + matchup_gap, team_name_color, league.wc1)

    # DS A teams
    graphics.DrawText(canvas, font["font"], ds_x, ds_a_y, team_name_color, league.wc_winner)
    graphics.DrawText(canvas, font["font"], ds_x, ds_a_y + matchup_gap, team_name_color, league.ds_one)

    # DS B
    graphics.DrawText(canvas, font["font"], ds_x, ds_b_y, team_name_color, league.ds_three)
    graphics.DrawText(canvas, font["font"], ds_x, ds_b_y + matchup_gap, team_name_color, league.ds_two)

    # LCS
    graphics.DrawText(canvas, font["font"], lcs_x, ds_a_y + winner_offset, team_name_color, league.l_two)
    graphics.DrawText(canvas, font["font"], lcs_x, ds_b_y + winner_offset, team_name_color, league.l_one)

    # league champ
    graphics.DrawText(canvas, font["font"], champ_x + 1, champ_y, team_name_color, league.champ)
