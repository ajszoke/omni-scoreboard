import os

from PIL import Image

import utils

try:
    from rgbmatrix import RGBMatrix, graphics
except ImportError:
    from RGBMatrixEmulator import graphics


def render_team_banner(canvas, layout, team_colors, home_team, away_team, run_text_colors, win_probability, should_render_runline, should_render_prob):

    ALT = "-alt"
    default_colors = team_colors.color("default")

    away_colors = __team_colors(team_colors, away_team.abbrev)
    try:
        away_team_color = away_colors["home"]
    except KeyError:
        away_team_color = team_colors.color("default.home")

    home_colors = __team_colors(team_colors, home_team.abbrev)
    try:
        home_team_color = home_colors["home"]
    except KeyError:
        home_team_color = team_colors.color("default.home")

    try:
        away_team_accent = away_colors["accent"]
    except KeyError:
        away_team_accent = team_colors.color("default.accent")

    try:
        home_team_accent = home_colors["accent"]
    except KeyError:
        home_team_accent = team_colors.color("default.accent")

    bg_coords = {}
    bg_coords["away"] = layout.coords("teams.background.away")
    bg_coords["home"] = layout.coords("teams.background.home")

    accent_coords = {}
    accent_coords["away"] = layout.coords("teams.accent.away")
    accent_coords["home"] = layout.coords("teams.accent.home")

    delta_e = utils.color_delta_e(away_team_color, home_team_color)
    similarity_threshold = 15
    away_team_alt_option = ALT if delta_e < similarity_threshold else ""

    away_team_path = "assets/img/team-icons/mlb/20/" + away_team.abbrev + away_team_alt_option + ".png"
    away_team_icon = None
    if os.path.exists(away_team_path):
        away_team_icon = Image.open(away_team_path).convert("RGB")
    else:
        print(away_team_path + " not found!")
    home_team_path = "assets/img/team-icons/mlb/20/" + home_team.abbrev + ".png"
    home_team_icon = None
    if os.path.exists(home_team_path):
        home_team_icon = Image.open(home_team_path).convert("RGB")
    else:
        print(home_team_path + " not found!")


    for team in ["away", "home"]:
        try:
            # render team icons
            x_offset = layout.coords("teams.team_icons." + team + ".x")
            y_offset = layout.coords("teams.team_icons." + team + ".y")
            icon_size = layout.coords("teams.team_icons.size")
            for x in range(icon_size):
                for y in range(icon_size):
                    color = away_team_icon.getpixel((x, y)) if team == "away" else home_team_icon.getpixel((x, y))
                    canvas.SetPixel(x + x_offset, y + y_offset, color[0], color[1], color[2])

            # render win prob meter
            if should_render_prob:
                prob_width = layout.coords("teams.win_prob_width")
                do_render_win_prob = True
                if team == "away" and win_probability > 0:
                    win_prob_color = away_team_color if away_team_alt_option == ALT \
                                                        and not utils.is_color_black(away_team_color) else away_team_accent
                    iter_dir = -1
                elif team == "home" and win_probability < 0:
                    win_prob_color = home_team_accent
                    iter_dir = 1
                else:
                    win_prob_color = None
                    do_render_win_prob = False
                    iter_dir = 0

                if do_render_win_prob:  # fixme
                    step_size = int(100/icon_size)
                    for y in range(icon_size):
                        if abs(win_probability) >= step_size:
                            color = win_prob_color
                        else:
                            color_mult = abs(win_probability) / step_size
                            color = {
                                "r": int(color_mult * win_prob_color["r"]),
                                "g": int(color_mult * win_prob_color["g"]),
                                "b": int(color_mult * win_prob_color["b"])
                            }
                        for x in range(prob_width):
                            prob_x = x + icon_size
                            prob_y = icon_size + (iter_dir * y) + min(0, iter_dir)
                            canvas.SetPixel(prob_x, prob_y, color["r"], color["g"], color["b"])
                        win_probability += iter_dir * step_size
                        if (win_probability >= 0 and iter_dir > 0) or (win_probability <= 0 and iter_dir < 0):
                            break

        except:
            pass


    # for team in ["away", "home"]:
    #     for x in range(accent_coords[team]["width"]):
    #         for y in range(accent_coords[team]["height"]):
    #             color = away_team_accent if team == "away" else home_team_accent
    #             x_offset = accent_coords[team]["x"]
    #             y_offset = accent_coords[team]["y"]
    #             canvas.SetPixel(x + x_offset, y + y_offset, color["r"], color["g"], color["b"])

    # use_full_team_names = can_use_full_team_names(canvas, full_team_names, short_team_names_for_runs_hits, [home_team, away_team])

    # __render_team_text(canvas, layout, away_colors, away_team, "away", use_full_team_names, default_colors)
    # __render_team_text(canvas, layout, home_colors, home_team, "home", use_full_team_names, default_colors)

    # Number of characters in each score.
    if should_render_runline:
        score_spacing = {
            "runs": max(len(str(away_team.runs)), len(str(home_team.runs))),
            "hits": max(len(str(away_team.hits)), len(str(home_team.hits))),
            "errors": max(len(str(away_team.errors)), len(str(home_team.errors))),
        }
        __render_team_score(canvas, layout, away_colors, run_text_colors["away"], away_team, "away", default_colors, score_spacing)
        __render_team_score(canvas, layout, home_colors, run_text_colors["home"], home_team, "home", default_colors, score_spacing)

def can_use_full_team_names(canvas, enabled, abbreviate_on_overflow, teams):
    # Settings enabled and size is able to display it
    if enabled and canvas.width > 32:

        # If config enabled for abbreviating if runs or hits takes up an additional column (i.e. 9 -> 10)
        if abbreviate_on_overflow:

            # Iterate through the teams to see if we should abbreviate
            for team in teams:
                if team.runs > 9 or team.hits > 9:
                    return False
            
            # Else use full names if no stats column has overflowed
            return True

        # If config for abbreviating is not set, use full name
        else:
            return True

    # Fallback to abbreviated names for all cases
    return False

def __team_colors(team_colors, team_abbrev):
    try:
        team_colors = team_colors.color(team_abbrev.lower())
    except KeyError:
        team_colors = team_colors.color("default")
    return team_colors


def __render_team_text(canvas, layout, colors, team, homeaway, full_team_names, default_colors):
    text_color = colors.get("text", default_colors["text"])
    text_color_graphic = graphics.Color(text_color["r"], text_color["g"], text_color["b"])
    coords = layout.coords("teams.name.{}".format(homeaway))
    font = layout.font("teams.name.{}".format(homeaway))
    team_text = "{:3s}".format(team.abbrev.upper())
    if full_team_names:
        team_text = "{:13s}".format(team.name)
    graphics.DrawText(canvas, font["font"], coords["x"], coords["y"], text_color_graphic, team_text)


def __render_score_component(canvas, layout, colors, font, default_colors, coords, component_val, width_chars):
    # The coords passed in are the rightmost pixel.
    font_width = font["size"]["width"]
    # Number of pixels between runs/hits and hits/errors.
    rhe_coords = layout.coords("teams.runs.runs_hits_errors")
    component_val = str(component_val)
    # Draw each digit from right to left.
    for i, c in enumerate(component_val[::-1]):
        if i > 0 and rhe_coords["compress_digits"]:
            coords["x"] += 1
        char_draw_x = coords["x"] - font_width * (i + 1)  # Determine character position
        graphics.DrawText(canvas, font["font"], char_draw_x, coords["y"], colors, c)
    if rhe_coords["compress_digits"]:
        coords["x"] += width_chars - len(component_val)  # adjust for compaction on values not rendered
    coords["x"] -= font_width * width_chars + rhe_coords["spacing"] - 1  # adjust coordinates for next score.


def __render_team_score(canvas, layout, colors, run_text_colors, team, homeaway, default_colors, score_spacing):
    coords = layout.coords(f"teams.runs.{homeaway}").copy()
    font = layout.font("teams.runs.small") if (score_spacing["hits"] > 1 and score_spacing["runs"] > 1) else layout.font("teams.runs.normal")
    if layout.coords("teams.runs.runs_hits_errors")["show"]:
        __render_score_component(
            canvas, layout, run_text_colors, font, default_colors, coords, team.errors, score_spacing["errors"]
        )
        __render_score_component(
            canvas, layout, run_text_colors, font, default_colors, coords, team.hits, score_spacing["hits"]
        )
    __render_score_component(canvas, layout, run_text_colors, font, default_colors, coords, team.runs, score_spacing["runs"])
