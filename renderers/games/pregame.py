import utils

try:
    from rgbmatrix import graphics
except ImportError:
    from RGBMatrixEmulator import graphics

from data.config.color import Color
from data.config.layout import Layout
from data.scoreboard.pregame import Pregame
from renderers import scrollingtext
from utils import center_text_position


def render_pregame(canvas, layout: Layout, colors: Color, pregame: Pregame, probable_starter_pos, pregame_weather):
    # text_len = _render_probable_starters(canvas, layout, colors, pregame, probable_starter_pos, pregame_weather)

    time_text = pregame.start_time

    odds_spacer = 3

    away_record_text = pregame.away_record
    away_odds_text = pregame.get_away_odds()
    away_starter = pregame.get_away_starter()
    if away_starter is not None:
        away_starter_name = away_starter["name"] if away_starter is not None else "TBD"
        away_starter_stats_text = "{}-{} ({} ERA)".format(away_starter["wins"], away_starter["losses"],
                                                          away_starter["era"])
    else:
        away_starter_name = "TBD"
        away_starter_stats_text = ""
    home_record_text = pregame.home_record
    home_odds_text = pregame.get_home_odds()
    home_starter = pregame.get_home_starter()
    if home_starter is not None:
        home_starter_name = home_starter["name"] if home_starter is not None else "TBD"
        home_starter_stats_text = "{}-{} ({} ERA)".format(home_starter["wins"], home_starter["losses"],
                                                          home_starter["era"])
    else:
        home_starter_name = "TBD"
        home_starter_stats_text = ""
    start_time_coords = layout.coords("pregame.start_time")
    starter_name_coords = layout.coords("pregame.starting_pitcher_name")
    starter_stats_coords = layout.coords("pregame.starting_pitcher_stats")
    record_coords = layout.coords("pregame.record")
    odds_coords = layout.coords("pregame.odds")

    font = layout.font("pregame.start_time")
    record_font = layout.font("pregame.record")
    odds_font = layout.font("pregame.odds")
    starter_name_font = layout.font("pregame.starting_pitcher_name")
    starter_stats_font = layout.font("pregame.starting_pitcher_stats")
    color = colors.graphics_color("pregame.start_time")
    record_color = colors.graphics_color("pregame.record")
    odds_color = colors.graphics_color("pregame.odds")
    starter_name_color = colors.graphics_color("pregame.starter_name")
    starter_stats_color = colors.graphics_color("pregame.starter_stats")
    time_x = center_text_position(time_text, start_time_coords["x"], font["size"]["width"])
    graphics.DrawText(canvas, record_font["font"], record_coords["x"], record_coords["away_y"], record_color, away_record_text)
    graphics.DrawText(canvas, record_font["font"], record_coords["x"], record_coords["home_y"], record_color, home_record_text)
    graphics.DrawText(canvas, font["font"], time_x, start_time_coords["y"], color, time_text)
    graphics.DrawText(canvas, starter_name_font["font"], starter_name_coords["x"], starter_name_coords["away_y"], starter_name_color, away_starter_name)
    graphics.DrawText(canvas, starter_name_font["font"], starter_name_coords["x"], starter_name_coords["home_y"], starter_name_color, home_starter_name)
    away_odds_xpos = record_coords["x"] + utils.text_matrix_width(away_record_text, record_font) + odds_spacer
    away_starter_stats_xpos = starter_name_coords["x"] + utils.text_matrix_width(away_starter_name, starter_name_font) + odds_spacer
    home_starter_stats_xpos = starter_name_coords["x"] + utils.text_matrix_width(home_starter_name, starter_name_font) + odds_spacer
    home_odds_xpos = record_coords["x"] + utils.text_matrix_width(home_record_text, record_font) + odds_spacer
    graphics.DrawText(canvas, starter_stats_font["font"], away_starter_stats_xpos, starter_stats_coords["away_y"], starter_stats_color,
                      away_starter_stats_text)
    graphics.DrawText(canvas, starter_stats_font["font"], home_starter_stats_xpos, starter_stats_coords["home_y"],
                      starter_stats_color, home_starter_stats_text)
    graphics.DrawText(canvas, odds_font["font"], away_odds_xpos, odds_coords["away_y"], odds_color,
                      away_odds_text)
    graphics.DrawText(canvas, odds_font["font"], home_odds_xpos, odds_coords["home_y"], odds_color,
                      home_odds_text)

    return 0


def _render_start_time(canvas, layout, colors, pregame):
    time_text = pregame.start_time
    coords = layout.coords("pregame.start_time")
    font = layout.font("pregame.start_time")
    color = colors.graphics_color("pregame.start_time")
    time_x = center_text_position(time_text, coords["x"], font["size"]["width"])
    graphics.DrawText(canvas, font["font"], time_x, coords["y"], color, time_text)


def _render_warmup(canvas, layout, colors, pregame):
    warmup_text = pregame.status
    coords = layout.coords("pregame.warmup_text")
    font = layout.font("pregame.warmup_text")
    color = colors.graphics_color("pregame.warmup_text")
    warmup_x = center_text_position(warmup_text, coords["x"], font["size"]["width"])
    graphics.DrawText(canvas, font["font"], warmup_x, coords["y"], color, warmup_text)


def _render_probable_starters(canvas, layout, colors, pregame, probable_starter_pos, pregame_weather):
    coords = layout.coords("pregame.scrolling_text")
    font = layout.font("pregame.scrolling_text")
    color = colors.graphics_color("pregame.scrolling_text")
    bgcolor = colors.graphics_color("default.background")
    if pregame_weather and pregame.pregame_weather:
        pitchers_text = pregame.away_starter + " vs " + pregame.home_starter + " Weather: " + pregame.pregame_weather
    else :
        pitchers_text = pregame.away_starter + " vs " + pregame.home_starter
    return scrollingtext.render_text(
        canvas, coords["x"], coords["y"], coords["width"], font, color, bgcolor, pitchers_text, probable_starter_pos
    )
