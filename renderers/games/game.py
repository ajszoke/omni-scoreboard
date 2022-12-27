import utils
from data.playresult import PlayResult

try:
    from rgbmatrix import graphics
except ImportError:
    from RGBMatrixEmulator import graphics

from data.config.color import Color
from data.config.layout import Layout
from data.scoreboard import Scoreboard
from data.scoreboard.atbat import AtBat
from data.scoreboard.bases import Bases
from data.scoreboard.inning import Inning
from data.scoreboard.pitches import Pitches
from renderers import scrollingtext
import data.config.layout as cfglayout


def render_live_game(canvas, layout: Layout, colors: Color, scoreboard: Scoreboard, text_pos, animation_time, pitcher_stats, batter_stats):
    pos = 0

    if scoreboard.inning.state == Inning.TOP or scoreboard.inning.state == Inning.BOTTOM:

        # Check if we're deep enough into a game and it's a no hitter or perfect game
        is_perfect = False
        is_no_hitter = False
        should_display_nohitter = layout.coords("nohitter")["innings_until_display"]
        layout.state = cfglayout.LAYOUT_STATE_WARMUP  # FIXME
        if scoreboard.inning.number > should_display_nohitter:
            if layout.state == cfglayout.LAYOUT_STATE_PERFECT:
                is_perfect = True
            elif layout.state == cfglayout.LAYOUT_STATE_NOHIT:
                is_no_hitter = True

        pos = _render_at_bat(
            canvas,
            layout,
            colors,
            scoreboard.atbat,
            text_pos,
            scoreboard.play_result_data,
            # (animation_time // 6) % 2,
            scoreboard.pitches,
            pitcher_stats,
            batter_stats,
            is_perfect,
            is_no_hitter
        )

        _render_count(canvas, layout, colors, scoreboard.pitches)
        _render_outs(canvas, layout, colors, scoreboard.outs)
        _render_bases(canvas, layout, colors, scoreboard.bases, scoreboard.homerun(), (animation_time % 16) // 5)

        _render_inning_display(canvas, layout, colors, scoreboard.inning)
    else:
        _render_inning_break(canvas, layout, colors, scoreboard.inning)
        _render_due_up(canvas, layout, colors, scoreboard.atbat)

    return pos


# --------------- at-bat ---------------
def _render_at_bat(canvas, layout, colors, atbat: AtBat, text_pos, play_result_data, pitches: Pitches, pitcher_stats,
                   batter_stats, is_perfect, is_no_hitter):
    spacer_width = 3
    plength = __render_pitcher_text(canvas, layout, colors, atbat.pitcher, text_pos)
    blength = __render_batter_text(canvas, layout, colors, atbat.batter, text_pos, atbat.batter_order_num)

    if 'eventType' in play_result_data and play_result_data["eventType"] not in ['pitching_substitution', 'defensive_switch']:  # todo
        play_result = PlayResult(layout, colors, canvas)
        play_result.populate(play_result_data)
        __render_play_result(canvas, layout, colors, play_result)

    else:
        __render_pitch_text(canvas, layout, colors, pitches)
        __render_pitch_count(canvas, layout, colors, pitcher_stats)
        __render_pitcher_stats_text(canvas, layout, colors, plength, pitcher_stats, spacer_width, is_perfect,
                                    is_no_hitter, text_pos)
        __render_batter_stats_text(canvas, layout, colors, blength, batter_stats, spacer_width, text_pos)

    # if strikeout:
    #     if animation:
    #         __render_strikeout(canvas, layout, colors, looking)
    #     # return plength
    # else:
    #     pass
    #     # return max(plength, blength)
    return 0

def __render_play_result(canvas, layout, colors, play_result: PlayResult):
    text_color = colors.graphics_color("play_result.text")
    center_x = int((layout.coords("atbat.result.x") + canvas.width) / 2)
    width = canvas.width - layout.coords("atbat.result.x")
    if play_result.result_color is not None:
        y1 = int(canvas.height)
        y2 = int(layout.coords("atbat.result.y"))
        x1 = int(layout.coords("atbat.result.x"))
        x2 = int(canvas.width)
        color = colors.graphics_color("play_result." + play_result.result_color)
        for yi in range(y2, y1):
            graphics.DrawLine(canvas, x1, yi, x2, yi, color)
    for line in range(1, 4):
        line_root = play_result.text_lines["line_" + str(line)]
        if line_root["text"] != '':
            x = utils.center_text_position(line_root["text"], center_x, int(line_root["font"]["size"]["width"]))
            y = line_root["y"]
            graphics.DrawText(canvas, line_root["font"]["font"], x, y, text_color, line_root["text"])

def __render_strikeout(canvas, layout, colors, looking):
    coords = layout.coords("atbat.strikeout")
    color = colors.graphics_color("atbat.strikeout")
    font = layout.font("atbat.strikeout")
    text = "ꓘ" if looking else "K"
    graphics.DrawText(canvas, font["font"], coords["x"], coords["y"], color, text)


def __render_batter_text(canvas, layout, colors, batter, text_pos, batter_order_num):
    coords = layout.coords("atbat.batter")
    color = colors.graphics_color("atbat.batter")
    font = layout.font("atbat.batter")
    # bgcolor = colors.graphics_color("default.background")
    # offset = coords.get("offset", 0)
    # pos = scrollingtext.render_text(
    #     canvas,
    #     coords["x"] + font["size"]["width"] * 3,
    #     coords["y"],
    #     coords["width"],
    #     font,
    #     color,
    #     bgcolor,
    #     batter,
    #     text_pos + offset,
    # )
    display_text = str(batter_order_num) + ". " + batter
    graphics.DrawText(canvas, font["font"], coords["x"], coords["y"], color, display_text)
    return coords["x"] + len(display_text) * font["size"]["width"]


def __render_pitcher_text(canvas, layout, colors, pitcher, text_pos):
    coords = layout.coords("atbat.pitcher")
    color = colors.graphics_color("atbat.pitcher")
    font = layout.font("atbat.pitcher")
    # bgcolor = colors.graphics_color("default.background")
    # pos = scrollingtext.render_text(
    #     canvas,
    #     coords["x"] + font["size"]["width"] * 2,
    #     coords["y"],
    #     coords["width"],
    #     font,
    #     color,
    #     bgcolor,
    #     pitcher,
    #     text_pos,
    # )
    display_text = "P: " + pitcher
    graphics.DrawText(canvas, font["font"], coords["x"], coords["y"], color, display_text)
    return coords["x"] + len(display_text) * font["size"]["width"]

def __render_pitcher_stats_text(canvas, layout, colors, p_length, pitcher_stats, spacer_width, is_perfect, is_no_hitter, text_pos):
    y_coord = layout.coords("atbat.pitcher_stats.y")
    font = layout.font("atbat.pitcher_stats")
    core_color = colors.graphics_color("perfect_game_text") if is_perfect \
        else colors.graphics_color("nohit_text") if is_no_hitter \
        else colors.graphics_color("atbat.pitcher")
    expanded_color = colors.graphics_color("atbat.pitcher")
    bgcolor = colors.graphics_color("default.background")
    display_text_core = " " + __getPitcherCoreGameStatsStr(pitcher_stats)
    display_text_expanded = __getPitcherExpandedGameStatsStr(pitcher_stats)

    if is_perfect or is_no_hitter:
        core_len = graphics.DrawText(canvas, font["font"], p_length + spacer_width, y_coord, core_color,
                                     display_text_core)
        pos = scrollingtext.render_text(
            canvas,
            p_length + (spacer_width * 3) + core_len,
            y_coord,
            canvas.width - p_length - (spacer_width * 1) - core_len,
            font,
            expanded_color,
            bgcolor,
            display_text_core + display_text_expanded,
            text_pos,
        )
    else:
        x = p_length + (spacer_width * 3)
        pos = scrollingtext.render_text(
            canvas,
            x,
            y_coord,
            canvas.width - p_length - (spacer_width * 1),
            font,
            core_color,
            bgcolor,
            display_text_core + display_text_expanded,
            text_pos,
        )
    return pos

def __render_batter_stats_text(canvas, layout, colors, b_length, batter_stats, spacer_width, text_pos):
    y_coord = layout.coords("atbat.batter_stats.y")
    color = colors.graphics_color("atbat.batter")
    font = layout.font("atbat.batter_stats")
    bgcolor = colors.graphics_color("default.background")
    display_text = " " + __getBatterGameStatsStr(batter_stats)
    pos = scrollingtext.render_text(
        canvas,
        b_length + (spacer_width * 3),
        y_coord,
        canvas.width - b_length - (spacer_width * 1),
        font,
        color,
        bgcolor,
        display_text,
        text_pos,
    )
    return pos

def __render_pitch_count(canvas, layout, colors, pitcher_stats):
    coords = layout.coords("atbat.pitch_count")
    font = layout.font("atbat.pitch_count")
    color = colors.graphics_color("atbat.pitch_count")
    count = "P:" + str(pitcher_stats["pitches"])
    graphics.DrawText(canvas, font["font"], coords["x"], coords["y"], color, count)

def __render_pitch_text(canvas, layout, colors, pitches: Pitches):
    coords = layout.coords("atbat.pitch")
    color = colors.graphics_color("atbat.pitch")
    font = layout.font("atbat.pitch")
    bgcolor = colors.graphics_color("default.background")
    if int(pitches.last_pitch_speed) > 97:
        bgcolor = colors.graphics_color("atbat.fast-pitch-bg")
    if(int(pitches.last_pitch_speed) > 0 and layout.coords("atbat.pitch")["enabled"]):
        mph= " "
        if(layout.coords("atbat.pitch")["mph"]):
            mph="mph "
        if(layout.coords("atbat.pitch")["desc_length"]=="Long"):
            pitch_text = str(pitches.last_pitch_speed) + mph + pitches.last_pitch_type_long
        elif(layout.coords("atbat.pitch")["desc_length"]=="Short"):
            pitch_text = str(pitches.last_pitch_speed) + mph + pitches.last_pitch_type
        else:
            pitch_text = None
        graphics.DrawText(canvas, font["font"], coords["x"], coords["y"], color, pitch_text)

def __getPitcherCoreGameStatsStr(pitcher_stats):
    return str(pitcher_stats["ip"]) + " IP " + str(pitcher_stats["hits"]) + " H"

def __getPitcherExpandedGameStatsStr(pitcher_stats):
    hr_str = " {} HR".format(pitcher_stats["hr"]) if pitcher_stats["hr"] > 0 else ""
    er_str = " {} ER".format(pitcher_stats["er"]) if pitcher_stats["er"] > 0 else ""
    balks_str = " {} BALK".format(pitcher_stats["balks"]) if pitcher_stats["balks"] > 0 else ""
    walks_str = " {} BB".format(pitcher_stats["walks"]) if pitcher_stats["walks"] > 0 else ""
    ks_str = " {} K".format(pitcher_stats["strikeouts"]) if pitcher_stats["strikeouts"] > 0 else ""
    return er_str + ks_str + balks_str + walks_str + hr_str

def __getBatterGameStatsStr(batter_stats):
    ab_str = "{}-{}".format(batter_stats["hits"], batter_stats["at_bats"])
    hr_str = " {} HR".format(batter_stats["hr"]) if batter_stats["hr"] > 1 else " HR" if batter_stats["hr"] > 0 else ""
    rbi_str = " {} RBI".format(batter_stats["rbi"]) if batter_stats["rbi"] > 1 else " RBI" if batter_stats["rbi"] > 0 else ""
    walks_str = " {} BB".format(batter_stats["bb"]) if batter_stats["bb"] > 1 else " BB" if batter_stats["bb"] > 0 else ""
    hbp_str = " {} HBP".format(batter_stats["hbp"]) if batter_stats["hbp"] > 1 else " HBP" if batter_stats["hbp"] > 0 else ""
    triple_str = " {} 3B".format(batter_stats["3b"]) if batter_stats["3b"] > 1 else " 3B" if batter_stats["3b"] > 0 else ""
    double_str = " {} 2B".format(batter_stats["2b"]) if batter_stats["2b"] > 1 else " 2B" if batter_stats["2b"] > 0 else ""
    sac_str = " {} SAC".format(batter_stats["sac"]) if batter_stats["sac"] > 1 else " SAC" if batter_stats["sac"] > 0 else ""
    ks_str = " {} K".format(batter_stats["k"]) if batter_stats["k"] > 1 else " K" if batter_stats["k"] > 0 else ""
    gitp_str = " {} GITP".format(batter_stats["gitp"]) if batter_stats["gitp"] > 1 else " GITP" if batter_stats["gitp"] > 0 else ""
    gidp_str = " {} GIDP".format(batter_stats["gidp"]) if batter_stats["gidp"] > 1 else " GIDP" if batter_stats["gidp"] > 0 else ""
    avg_str = " {} AVG".format(batter_stats["avg"])
    ops_str = " {} OPS".format(batter_stats["ops"])
    return ab_str + hr_str + triple_str + double_str + rbi_str + walks_str + ks_str + hbp_str + sac_str + gitp_str + gidp_str + avg_str
    
# --------------- bases ---------------
def _render_bases(canvas, layout, colors, bases: Bases, home_run, animation):
    base_runners = bases.runners
    base_colors = []
    base_colors.append(colors.graphics_color("bases.1B"))
    base_colors.append(colors.graphics_color("bases.2B"))
    base_colors.append(colors.graphics_color("bases.3B"))

    base_px = []
    base_px.append(layout.coords("bases.1B"))
    base_px.append(layout.coords("bases.2B"))
    base_px.append(layout.coords("bases.3B"))

    for base in range(len(base_runners)):
        __render_base_outline(canvas, base_px[base], base_colors[base])

        # Fill in the base if there's currently a baserunner or cycle if theres a homer
        if base_runners[base] or (home_run and animation == base):
            __render_baserunner(canvas, base_px[base], base_colors[base])


def __render_base_outline(canvas, base, color):
    x, y = (base["x"], base["y"])
    size = base["size"]
    half = abs(size // 2)
    graphics.DrawLine(canvas, x + half, y, x, y + half, color)
    graphics.DrawLine(canvas, x + half, y, x + size, y + half, color)
    graphics.DrawLine(canvas, x + half, y + size, x, y + half, color)
    graphics.DrawLine(canvas, x + half, y + size, x + size, y + half, color)


def __render_baserunner(canvas, base, color):
    x, y = (base["x"], base["y"])
    size = base["size"]
    half = abs(size // 2)
    for offset in range(1, half + 1):
        graphics.DrawLine(canvas, x + half - offset, y + size - offset, x + half + offset, y + size - offset, color)
        graphics.DrawLine(canvas, x + half - offset, y + offset, x + half + offset, y + offset, color)


# --------------- count ---------------
def _render_count(canvas, layout, colors, pitches: Pitches):
    font = layout.font("batter_count")
    coords = layout.coords("batter_count")
    pitches_color = colors.graphics_color("batter_count")
    batter_count_text = "{}-{}".format(pitches.balls, pitches.strikes)
    graphics.DrawText(canvas, font["font"], coords["x"], coords["y"], pitches_color, batter_count_text)


# --------------- outs ---------------
def __out_colors(colors):
    outlines = []
    fills = []
    for i in range(3):
        color = colors.graphics_color(f"outs.{i+1}")
        outlines.append(color)
        try:
            color = colors.graphics_color(f"outs.fill.{i+1}")
        except KeyError:
            pass
        fills.append(color)
    return outlines, fills


def _render_outs(canvas, layout, colors, outs):
    out_px = []
    out_px.append(layout.coords("outs.1"))
    out_px.append(layout.coords("outs.2"))
    out_px.append(layout.coords("outs.3"))

    out_colors = []
    out_colors, fill_colors = __out_colors(colors)

    for out in range(len(out_px)):
        __render_out_circle(canvas, out_px[out], out_colors[out])
        # Fill in the circle if that out has occurred
        if outs.number > out:
            __fill_out_circle(canvas, out_px[out], fill_colors[out])


def __render_out_circle(canvas, out, color):
    x, y, size = (out["x"], out["y"], out["size"])

    graphics.DrawLine(canvas, x, y, x + size, y, color)
    graphics.DrawLine(canvas, x, y, x, y + size, color)
    graphics.DrawLine(canvas, x + size, y + size, x, y + size, color)
    graphics.DrawLine(canvas, x + size, y + size, x + size, y, color)


def __fill_out_circle(canvas, out, color):
    size = out["size"]
    x, y = (out["x"], out["y"])
    x += 1
    y += 1
    size -= 1
    for y_offset in range(size):
        graphics.DrawLine(canvas, x, y + y_offset, x + size - 1, y + y_offset, color)


# --------------- inning information ---------------
def _render_inning_break(canvas, layout, colors, inning: Inning):
    text_font = layout.font("inning.break.text")
    num_font = layout.font("inning.break.number")
    text_coords = layout.coords("inning.break.text")
    num_coords = layout.coords("inning.break.number")
    color = colors.graphics_color("inning.break.text")
    text = inning.state
    if text == "Middle":
        text = "Mid"
    num = inning.ordinal
    graphics.DrawText(canvas, text_font["font"], text_coords["x"], text_coords["y"], color, text)
    graphics.DrawText(canvas, num_font["font"], num_coords["x"], num_coords["y"], color, num)


def _render_due_up(canvas, layout, colors, atbat: AtBat):
    due_font = layout.font("inning.break.due_up.due")
    due_color = colors.graphics_color("inning.break.due_up")

    due = layout.coords("inning.break.due_up.due")
    up = layout.coords("inning.break.due_up.up")
    graphics.DrawText(canvas, due_font["font"], due["x"], due["y"], due_color, "Due")
    graphics.DrawText(canvas, due_font["font"], up["x"], up["y"], due_color, "Up:")

    divider = layout.coords("inning.break.due_up.divider")
    if divider["draw"]:
        graphics.DrawLine(
            canvas,
            divider["x"],
            divider["y_start"],
            divider["x"],
            divider["y_end"],
            colors.graphics_color("inning.break.due_up_divider"),
        )

    batter_font = layout.font("inning.break.due_up.leadoff")
    batter_color = colors.graphics_color("inning.break.due_up_names")

    leadoff = layout.coords("inning.break.due_up.leadoff")
    on_deck = layout.coords("inning.break.due_up.on_deck")
    in_hole = layout.coords("inning.break.due_up.in_hole")
    graphics.DrawText(canvas, batter_font["font"], leadoff["x"], leadoff["y"], batter_color, atbat.batter)
    graphics.DrawText(canvas, batter_font["font"], on_deck["x"], on_deck["y"], batter_color, atbat.onDeck)
    graphics.DrawText(canvas, batter_font["font"], in_hole["x"], in_hole["y"], batter_color, atbat.inHole)


def _render_inning_display(canvas, layout, colors, inning: Inning):
    __render_number(canvas, layout, colors, inning)
    __render_inning_half(canvas, layout, colors, inning)


def __render_number(canvas, layout, colors, inning):
    number_color = colors.graphics_color("inning.number")
    coords = layout.coords("inning.number")
    font = layout.font("inning.number")
    pos_x = coords["x"] - (len(str(inning.number)) * font["size"]["width"])
    graphics.DrawText(canvas, font["font"], pos_x, coords["y"], number_color, str(inning.number))


def __render_inning_half(canvas, layout, colors, inning):
    font = layout.font("inning.number")
    num_coords = layout.coords("inning.number")
    arrow_coords = layout.coords("inning.arrow")
    inning_size = len(str(inning.number)) * font["size"]["width"]
    size = arrow_coords["size"]
    top = inning.state == Inning.TOP
    if top:
        x = num_coords["x"] - inning_size + arrow_coords["up"]["x_offset"]
        y = num_coords["y"] + arrow_coords["up"]["y_offset"]
        dir = 1
    else:
        x = num_coords["x"] - inning_size + arrow_coords["down"]["x_offset"]
        y = num_coords["y"] + arrow_coords["down"]["y_offset"]
        dir = -1

    keypath = "inning.arrow.up" if top else "inning.arrow.down"
    color = colors.graphics_color(keypath)
    for offset in range(size):
        graphics.DrawLine(canvas, x - offset, y + (offset * dir), x + offset, y + (offset * dir), color)
