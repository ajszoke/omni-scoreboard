import time

import json
from datetime import datetime

import requests
from rgbmatrix.core import Canvas

import debug
from data.config import Layout, Color
from data.update import UpdateStatus

ODDS_UPDATE_RATE = 5 * 60  # 10 minutes between odds updates


class PlayResult:
    def __init__(self, layout: Layout, colors: Color, canvas: Canvas):
        self.result_code = ''
        self.result_desc = ''
        self.result_json = {}
        self.layout = layout
        self.colors = colors
        self.canvas = canvas
        self.result_color = None
        self.text_lines = {
            "line_1": {
                "text": '',
                "font": '',
                "y": ''
            },
            "line_2": {
                "text": '',
                "font": '',
                "y": ''
            },
            "line_3": {
                "text": '',
                "font": '',
                "y": ''
            },
        }

    def retrieve_font_height(self, font):
        return font["font"].baseline  # works better than height

    def determine_out_type(self):
        play_str = ''
        if " pops " in self.result_desc:
            play_str = 'P'
        elif " flies " in self.result_desc:
            play_str = 'F'
        elif " foul" in self.result_desc:
            play_str = 'X'
        elif " lines " in self.result_desc:
            play_str = 'L'
        ground_out = " grounds " in self.result_desc
        play_str += self.determine_play_string(not ground_out)
        if play_str == '-3':
            play_str = "3u"
        return play_str

    def determine_play_string(self, ignore_to):
        desc_split = self.result_desc.split(" ")
        play_str = ''
        for word in desc_split:
            if word == "pitcher":
                play_str += '1'
            elif word == "catcher":
                play_str += '2'
            elif word == "first":
                play_str += '3'
            elif word == "second":
                play_str += '4'
            elif word == "third":
                play_str += '5'
            elif word == "shortstop":
                play_str += '6'
            elif word == "left":
                play_str += '7'
            elif word == "center":
                play_str += '8'
            elif word == "right":
                play_str += '9'
            elif word == "to" and not ignore_to:
                play_str += '-'
            elif word == '':
                return play_str
        return play_str

    def populate(self, json):
        self.result_json = json
        self.result_code = json["eventType"]
        self.result_desc = json["description"]

        GIANT_FONT = self.layout.font("atbat.result.giant")
        NORMAL_FONT = self.layout.font("atbat.result.normal")
        SMALL_FONT = self.layout.font("atbat.result.small")

        OUT_COLOR = 'out'
        ON_BASE_COLOR = 'on_base'
        STRIKEOUT_COLOR = 'strikeout'
        HOMERUN_COLOR = 'homerun'

        if json["rbi"] > 0:
            self.text_lines["line_3"]["text"] = str(json["rbi"]) + " RBI"
            self.text_lines["line_3"]["font"] = SMALL_FONT

        if "pickoff" in self.result_code:
            if "error" in self.result_code:
                pass  # FIXME
            else:
                self.text_lines["line_1"]["text"] = "PO"
                self.text_lines["line_1"]["font"] = GIANT_FONT
                self.text_lines["line_2"]["text"] = '' # fixme
                self.text_lines["line_2"]["font"] = NORMAL_FONT
                self.result_color = OUT_COLOR
        elif self.result_code == "single":
            self.text_lines["line_1"]["text"] = "1B"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "double":
            self.text_lines["line_1"]["text"] = "2B"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "triple":
            self.text_lines["line_1"]["text"] = "3B"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "home_run":
            self.result_color = HOMERUN_COLOR
            if json["rbi"] == 4:
                self.text_lines["line_1"]["text"] = "GRAND"
                self.text_lines["line_1"]["font"] = GIANT_FONT
                self.text_lines["line_2"]["text"] = "SLAM"
                self.text_lines["line_2"]["font"] = GIANT_FONT
                self.text_lines["line_3"]["text"] = ""
                self.text_lines["line_3"]["font"] = ""
            else:
                self.text_lines["line_1"]["text"] = "HR"
                self.text_lines["line_1"]["font"] = GIANT_FONT
        elif "double_play" in self.result_code:
            self.text_lines["line_1"]["text"] = "DP"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.text_lines["line_2"]["text"] = self.determine_play_string(False)
            self.text_lines["line_2"]["font"] = NORMAL_FONT
            self.result_color = OUT_COLOR

        elif self.result_code == "field_out":
            self.result_color = OUT_COLOR
            play_str = self.determine_out_type()
            self.text_lines["line_1"]["text"] = play_str
            self.text_lines["line_1"]["font"] = GIANT_FONT

        elif self.result_code == "force_out":
            self.text_lines["line_1"]["text"] = "FC"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = OUT_COLOR
        elif "fielders_choice" in self.result_code:
            self.text_lines["line_1"]["text"] = "FC"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = OUT_COLOR
        elif self.result_code == "strikeout":
            self.text_lines["line_1"]["text"] = "ꓘ" if "looking" in self.result_desc else "K"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = STRIKEOUT_COLOR
        elif self.result_code == "strikeout_double_play":
            self.text_lines["line_1"]["text"] = "K DP"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = STRIKEOUT_COLOR
        elif self.result_code == "strikeout_triple_play":
            self.text_lines["line_1"]["text"] = "K TP"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = STRIKEOUT_COLOR
        elif self.result_code == "triple_play":
            self.text_lines["line_1"]["text"] = "TP"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = OUT_COLOR
            self.text_lines["line_2"]["text"] = self.determine_play_string(False)
            self.text_lines["line_2"]["font"] = NORMAL_FONT
        elif self.result_code == "sac_fly":
            self.text_lines["line_1"]["text"] = "SAC F" + self.determine_play_string(True)
            self.text_lines["line_1"]["font"] = NORMAL_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "sac_bunt":
            self.text_lines["line_1"]["text"] = "SAC B" + self.determine_play_string(False)
            self.text_lines["line_1"]["font"] = NORMAL_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "cs_double_play":
            self.text_lines["line_1"]["text"] = "CS DP"
            self.text_lines["line_1"]["font"] = NORMAL_FONT
            self.text_lines["line_2"]["text"] = self.determine_play_string(False)
            self.text_lines["line_2"]["font"] = NORMAL_FONT
            self.result_color = OUT_COLOR
        elif self.result_code == "defensive_indiff":
            self.text_lines["line_1"]["text"] = "DI"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "sac_fly_double_play":
            self.text_lines["line_1"]["text"] = "SAC DP"
            self.text_lines["line_1"]["font"] = NORMAL_FONT
            self.text_lines["line_2"]["text"] = self.determine_play_string(False)
            self.text_lines["line_2"]["font"] = NORMAL_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "sac_bunt_double_play":
            self.text_lines["line_1"]["text"] = "SAC DP"
            self.text_lines["line_1"]["font"] = NORMAL_FONT
            self.text_lines["line_2"]["text"] = 'B' + self.determine_play_string(False)
            self.text_lines["line_2"]["font"] = NORMAL_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "walk":
            self.text_lines["line_1"]["text"] = "BB"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "intent_walk":
            self.text_lines["line_1"]["text"] = "IBB"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "hit_by_pitch":
            self.text_lines["line_1"]["text"] = "HBP"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "passed_ball":
            self.text_lines["line_1"]["text"] = "PB"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "pitching_substitution":
            pass  # todo
        elif self.result_code in ["runner_placed", "offensive_substitution", "defensive_switch", "umpire_substitution"]:
            pass
        elif self.result_code == "game_advisory":
            if self.result_desc == "Mound visit.":
                self.text_lines["line_1"]["text"] = "Mound"
                self.text_lines["line_1"]["font"] = SMALL_FONT
                self.text_lines["line_2"]["text"] = "Visit"
                self.text_lines["line_2"]["font"] = SMALL_FONT
                self.result_color = OUT_COLOR
        elif self.result_code == "stolen_base":
            self.text_lines["line_1"]["text"] = "SB"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "stolen_base_2b":
            self.text_lines["line_1"]["text"] = "SB"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.text_lines["line_2"]["text"] = "2B"
            self.text_lines["line_2"]["font"] = NORMAL_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "stolen_base_3b":
            self.text_lines["line_1"]["text"] = "SB"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.text_lines["line_2"]["text"] = "3B"
            self.text_lines["line_2"]["font"] = NORMAL_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "stolen_base_home":
            self.text_lines["line_1"]["text"] = "SB"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.text_lines["line_2"]["text"] = "RUN"
            self.text_lines["line_2"]["font"] = NORMAL_FONT
            self.result_color = HOMERUN_COLOR
        elif self.result_code == "caught_stealing":
            self.text_lines["line_1"]["text"] = "CS"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = OUT_COLOR
        elif self.result_code == "caught_stealing_2b":
            self.text_lines["line_1"]["text"] = "CS"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.text_lines["line_2"]["text"] = "2B"
            self.text_lines["line_2"]["font"] = NORMAL_FONT
            self.result_color = OUT_COLOR
        elif self.result_code == "caught_stealing_3b":
            self.text_lines["line_1"]["text"] = "CS"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.text_lines["line_2"]["text"] = "3B"
            self.text_lines["line_2"]["font"] = NORMAL_FONT
            self.result_color = OUT_COLOR
        elif self.result_code == "caught_stealing_home":
            self.text_lines["line_1"]["text"] = "CS"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.text_lines["line_2"]["text"] = "HOME"
            self.text_lines["line_2"]["font"] = NORMAL_FONT
            self.result_color = OUT_COLOR
        elif self.result_code == "pickoff_caught_stealing_2b":
            self.text_lines["line_1"]["text"] = "CS"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.text_lines["line_2"]["text"] = "2B"
            self.text_lines["line_2"]["font"] = NORMAL_FONT
            self.result_color = OUT_COLOR
        elif self.result_code == "pickoff_caught_stealing_3b":
            self.text_lines["line_1"]["text"] = "CS"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.text_lines["line_2"]["text"] = "3B"
            self.text_lines["line_2"]["font"] = NORMAL_FONT
            self.result_color = OUT_COLOR
        elif self.result_code == "pickoff_caught_stealing_home":
            self.text_lines["line_1"]["text"] = "CS"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.text_lines["line_2"]["text"] = "HOME"
            self.text_lines["line_2"]["font"] = NORMAL_FONT
            self.result_color = OUT_COLOR
        elif self.result_code == "balk":
            self.text_lines["line_1"]["text"] = "BALK"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "wild_pitch":
            self.text_lines["line_1"]["text"] = "WP"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = ON_BASE_COLOR
        elif self.result_code == "other_out":
            self.text_lines["line_1"]["text"] = "OUT"
            self.text_lines["line_1"]["font"] = GIANT_FONT
            self.result_color = OUT_COLOR
        else:
            print("Play result unknown: " + self.result_code)
            return

        text_height = 0
        spacer_height = 0
        if self.text_lines["line_1"]["text"] != '':
            text_height += self.retrieve_font_height(self.text_lines["line_1"]["font"]) + spacer_height
        if self.text_lines["line_2"]["text"] != '':
            text_height += self.retrieve_font_height(self.text_lines["line_2"]["font"]) + spacer_height
        if self.text_lines["line_3"]["text"] != '':
            text_height += self.retrieve_font_height(self.text_lines["line_3"]["font"]) + spacer_height

        box_height = self.canvas.height - self.layout.coords("atbat.result.y")
        rendered_height = self.layout.coords("atbat.result.y") + int((box_height - text_height) / 2)
        if self.text_lines["line_1"]["text"] != '':
            font_height = self.retrieve_font_height(self.text_lines["line_1"]["font"])
            self.text_lines["line_1"]["y"] = rendered_height + font_height
            rendered_height += font_height + spacer_height
        if self.text_lines["line_2"]["text"] != '':
            font_height = self.retrieve_font_height(self.text_lines["line_2"]["font"])
            self.text_lines["line_2"]["y"] = rendered_height + font_height
            rendered_height += font_height + spacer_height
        if self.text_lines["line_3"]["text"] != '':
            font_height = self.retrieve_font_height(self.text_lines["line_3"]["font"])
            self.text_lines["line_3"]["y"] = rendered_height + font_height
