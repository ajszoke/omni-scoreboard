# A list of mlb pitch types appearing in statcast
# https://www.daktronics.com/en-us/support/kb/DD3312647
# https://developer.sportradar.com/docs/read/baseball/MLB_v7_with_Statcast#frequently-asked-questions
# Dont change the index, but feel free to change 
# the descriptions

PITCH_LONG = {
    "AB": "Auto Ball",  #MLB default is "Automatic Ball"
    "AS": "Auto Strike",#MLB default is "Automatic Strike"
    "CH": "Change-up",
    "CU": "Curveball",
    "CS": "Slow Curve",
    "EP": "Eephus",
    "FC": "Cutter",
    "FA": "Fastball",
    "FF": "Fastball",    #MLB default is "Four-Seam Fastball"
    "FL": "Slutter",
    "FO": "Forkball",
    "FS": "Splitter",
    "FT": "2 Seamer",    #MLB default is "Two-Seam Fastball"
    "GY": "Gyroball",
    "IN": "Int Ball",    #MLB default is "Intentional Ball"
    "KC": "Knuckle Curve",
    "KN": "Knuckleball",
    "NP": "No Pitch",
    "PO": "Pitchout",
    "SC": "Screwball",
    "SI": "Sinker",
    "SL": "Slider",
    "SU": "Slurve",
    "UN": "Unknown"
}

PITCH_SHORT = {
    "AB": "AB",
    "AS": "AK",
    "CH": "CHGP",
    "CU": "CURV",
    "CS": "SCRV",
    "EP": "EPHS",
    "FC": "CUTR",
    "FA": "FB",
    "FF": "4SFB",
    "FL": "SLTR",
    "FO": "FORK",
    "FS": "SPLT",
    "FT": "2SFB",
    "GY": "GYRO",
    "IN": "INTB",
    "KC": "KCRV",
    "KN": "KNUK",
    "NP": "NO P",
    "PO": "POUT",
    "SC": "SCRW",
    "SI": "SNKR",
    "SL": "SLDR",
    "SU": "SLRV",
    "UN": "UKWN"
}

def fetch_long(value):
    return PITCH_LONG.get(value, PITCH_LONG["UN"])

def fetch_short(value):
    return PITCH_SHORT.get(value, PITCH_SHORT["UN"])
