#!/usr/bin/env python3
"""
Convert a structured JSON extraction of a horse-racing past-performance PDF
into the race JSON format accepted by handicap.py.

Usage:
    python pp_json_to_handicap.py extracted_pp.json
    python pp_json_to_handicap.py extracted_pp.json -o race.json

Expected input:
{
  "name": "Keeneland Race 1",
  "distance": "Six Furlongs",       # or distance_f: 6
  "surface": "dirt",
  "horses": [
    {
      "name": "Sudden Squall",
      "program_number": 1,
      "morning_line_odds": "10-1",
      "trainer": "Ethan W. West",
      "running_style": "S",
      "primepower": 61.35,
      "pace": {"ep1": 45, "lp": 65},
      "speed_figures": [65, 70, 51],
      "distance_figures": [65, 70, 51],
      "class_figures": [53, 56, 16],
      "scratched": false
    }
  ]
}

The input can use target handicap.py fields or raw past-performance data.
Explicit target fields always take priority. When proprietary figures are absent,
the script derives *proxy indices* from race results, class/purse, finish positions,
lengths beaten, distance/surface, and pace calls. These are relative estimates, not
official Equibase Speed Figures, pace figures, or Prime Power ratings. The output's
optional derived_fields list identifies values created by the proxy calculations.
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path


ALIASES = {
    "name": ("name", "horse_name", "horse"),
    "pp": ("pp", "program_number", "program", "post_position", "post"),
    "pl": ("pl", "odds_decimal", "morning_line_decimal", "morning_line_odds", "morning_line", "odds", "odds_text"),
    "style": ("style", "running_style", "run_style"),
    "trainer": ("trainer", "trainer_name"),
    "ep1": ("ep1", "early_pace", "early_pace_figure", "pace_1"),
    "lp": ("lp", "late_pace", "late_pace_figure", "late_pace_rating"),
    "avgspd": ("avgspd", "average_speed", "avg_speed", "average_speed_figure"),
    "avgdist": ("avgdist", "average_distance", "distance_rating", "avg_distance_figure"),
    "bestspd": ("bestspd", "best_speed", "best_speed_figure"),
    "primepower": ("primepower", "prime_power", "prime_power_rating", "class_rating"),
    "lastclass": ("lastclass", "last_class", "last_class_figure"),
    "avgclass": ("avgclass", "average_class", "avg_class"),
    "scratched": ("scratched", "is_scratched"),
    "duel": ("duel",),
    "lone": ("lone",),
    "ranks": ("ranks",),
}

# Raw past-performance fields accepted in each history entry.
HISTORY_ALIASES = {
    "finish": ("finish_position", "finish", "position", "placing", "place"),
    "field": ("field_size", "starters", "number_of_horses", "field"),
    "lengths": ("lengths_behind", "beaten_lengths", "margin", "lengths_back"),
    "purse": ("purse", "race_purse", "purse_amount", "value"),
    "class": ("class_figure", "class_rating", "class", "race_class", "level", "grade"),
    "distance": ("distance_f", "distance_furlongs", "distance", "race_distance"),
    "surface": ("surface", "track_surface"),
    "early_pos": ("first_call_position", "early_position", "position_1c", "call1_position", "start_position"),
    "stretch_pos": ("stretch_position", "position_stretch", "stretch_call_position", "position_2c"),
    "speed": ("speed", "speed_figure", "equibase_speed", "speed_rating"),
    "early": ("ep1", "early_pace", "early_pace_figure", "pace_1", "first_call_figure"),
    "late": ("lp", "late_pace", "late_pace_figure", "late_pace_rating"),
}



def first(obj, names):
    """Return the first present, non-empty alias from a dictionary."""
    if not isinstance(obj, dict):
        return None
    for key in names:
        if key in obj and obj[key] is not None and obj[key] != "":
            return obj[key]
    return None


def number(value):
    """Convert common numeric strings to floats; return None if not numeric."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text or text.lower() in {"n/a", "na", "none", "null", "-", "--"}:
        return None
    # Do not silently interpret fractional odds (e.g. 10-1) as a number.
    try:
        return float(text)
    except ValueError:
        return None


def odds_to_decimal(value):
    """Convert US fractional/morning-line odds such as '10-1' to 10.0."""
    direct = number(value)
    if direct is not None:
        return direct
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[-/]\s*(\d+(?:\.\d+)?)\s*", value)
        if match:
            numerator, denominator = map(float, match.groups())
            if denominator:
                return numerator / denominator
    return None


def distance_to_furlongs(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    text = str(value).strip().lower()
    direct = number(text)
    if direct is not None:
        return direct
    word_numbers = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "eleven": 11, "twelve": 12,
    }
    numeric_furlongs = re.search(r"\b(\d+(?:\.\d+)?)\s*furlongs?\b", text)
    if numeric_furlongs:
        return float(numeric_furlongs.group(1))
    match = re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+furlongs?\b", text)
    if match:
        return float(word_numbers[match.group(1)])
    if "mile" in text or re.search(r"\b1m\b", text):
        # Handle common PP notation: 1m = 8f; 1 1/16m = 8.5f.
        if re.search(r"1\s*1/16", text):
            return 8.5
        if re.search(r"1\s*1/8", text):
            return 9.0
        if re.search(r"1\s*1/4", text):
            return 10.0
        if re.search(r"1\s*1/2", text):
            return 12.0
        return 8.0
    # Common race distance shorthand in past-performance sheets.
    if re.search(r"\b6k\b", text):
        return 6.5
    if re.search(r"\b7k\b", text):
        return 7.5
    if re.search(r"\b1b\b", text):
        return 9.5
    return None


def as_numeric_list(value):
    if value is None:
        return []
    if isinstance(value, (int, float, str)):
        n = number(value)
        return [n] if n is not None else []
    if isinstance(value, list):
        return [n for item in value if (n := number(item)) is not None]
    return []


def get_history(horse):
    history = first(horse, ("past_performances", "recent_races", "race_history", "history"))
    return history if isinstance(history, list) else []


def average(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 2) if values else None


def _history_value(race, field):
    return first(race, HISTORY_ALIASES[field])


def _class_strength(race):
    """Make a comparable class proxy from numeric figures, race grades, or purse."""
    raw = _history_value(race, "class")
    n = number(raw)
    if n is not None:
        return n
    text = str(raw or "").upper().replace("GRADE", "G").strip()
    grade_match = re.search(r"\bG([123])\b", text)
    if grade_match:
        return {"1": 100.0, "2": 90.0, "3": 80.0}[grade_match.group(1)]
    if "STAKES" in text or "LISTED" in text:
        return 75.0
    if "ALLOWANCE" in text or re.search(r"\b(ALW|AOC|OC)\b", text):
        return 60.0
    if "CLAIM" in text or re.search(r"\b(CL|CLM|MCL)\b", text):
        return 35.0
    if "MAIDEN" in text or re.search(r"\b(MSW|MCL)\b", text):
        return 40.0 if "MSW" in text or "MAIDEN SPECIAL" in text else 30.0
    purse = number(_history_value(race, "purse"))
    if purse is not None and purse > 0:
        # Log scale avoids letting a single large purse dominate linearly.
        return round(10.0 * math.log10(purse), 2)
    return None


def _finish_score(race):
    """0-100 result proxy: finish position, field size, and beaten lengths."""
    finish = number(_history_value(race, "finish"))
    field = number(_history_value(race, "field"))
    lengths = number(_history_value(race, "lengths"))
    score = None
    if finish is not None and finish >= 1 and field is not None and field >= 2:
        score = 100.0 * (field - finish) / (field - 1)
    elif finish is not None and finish >= 1:
        # Field size absent: position-only proxy, diminishing with each placing.
        score = max(0.0, 100.0 - (finish - 1) * 12.0)
    if lengths is not None and lengths >= 0:
        length_score = max(0.0, 100.0 - min(lengths, 20.0) * 5.0)
        score = length_score if score is None else 0.65 * score + 0.35 * length_score
    return round(score, 2) if score is not None else None


def _race_pace_positions(race):
    early = number(_history_value(race, "early_pos"))
    stretch = number(_history_value(race, "stretch_pos"))
    finish = number(_history_value(race, "finish"))
    return early, stretch, finish


def derive_raw_pp_proxies(out, horse, current_distance=None, current_surface=None):
    """Fill absent handicapping fields with transparent proxies from raw PP facts.

    Exact labeled figures always win. Proxies are on relative scales; they are not
    official Equibase figures. The handicapper mainly ranks these fields within a race.
    """
    history = get_history(horse)
    if not history:
        return
    records = [r for r in history if isinstance(r, dict)]
    if not records:
        return

    performances = [_finish_score(r) for r in records]
    performances = [x for x in performances if x is not None]
    class_scores = [_class_strength(r) for r in records]
    class_scores = [x for x in class_scores if x is not None]

    # Result-based proxies for missing speed figures. These are deliberately
    # separated from official speed figures and preserve the relative form signal.
    if "avgspd" not in out and performances:
        out["avgspd"] = average(performances)
    if "bestspd" not in out and performances:
        out["bestspd"] = max(performances)

    # Distance aptitude: favor performances at today's distance (within 1 furlong),
    # otherwise use the horse's overall result history rather than inventing a rating.
    if "avgdist" not in out:
        matching = []
        for race, score in zip(records, [_finish_score(r) for r in records]):
            rd = distance_to_furlongs(_history_value(race, "distance"))
            rs = str(_history_value(race, "surface") or "").lower()
            surface_ok = not current_surface or not rs or ("turf" in current_surface) == ("turf" in rs or "grass" in rs)
            if score is not None and rd is not None and current_distance is not None and abs(rd - current_distance) <= 1.0 and surface_ok:
                matching.append(score)
        chosen = matching or performances
        if chosen:
            out["avgdist"] = average(chosen)

    if "lastclass" not in out and class_scores:
        # Preserve latest-race order: first record is assumed most recent.
        latest_class = _class_strength(records[0])
        if latest_class is not None:
            out["lastclass"] = latest_class
    if "avgclass" not in out and class_scores:
        out["avgclass"] = average(class_scores)

    # Prime Power proxy combines class strength and demonstrated results.
    # Exact Prime Power always takes priority when supplied in the JSON.
    if "primepower" not in out:
        parts = []
        if performances:
            parts.append((average(performances), 0.65))
        if class_scores:
            # Normalize class strength to the same 0-100 scale for the blend.
            c = average(class_scores)
            if c > 100:
                c = min(100.0, 50.0 + (c - 20.0) * 2.0)
            parts.append((c, 0.35))
        if parts:
            total_weight = sum(w for _, w in parts)
            out["primepower"] = round(sum(v * w for v, w in parts) / total_weight, 2)

    # Pace proxies from position at first call / stretch / finish. Better (lower)
    # position at the first call indicates early speed; improvement late indicates closing.
    early_scores, late_scores = [], []
    styles = []
    for race in records:
        early, stretch, finish = _race_pace_positions(race)
        field = number(_history_value(race, "field"))
        if early is not None:
            denominator = max((field or early) - 1.0, 1.0)
            early_scores.append(100.0 * max(0.0, denominator - (early - 1.0)) / denominator)
        if stretch is not None and finish is not None:
            late_scores.append(max(-50.0, min(50.0, (stretch - finish) * 8.0)) + 50.0)
        if early is not None and finish is not None:
            if early <= 2 and finish <= 3:
                styles.append("E")
            elif early <= 4 and finish <= 4:
                styles.append("E/P")
            elif finish < early:
                styles.append("S")
            else:
                styles.append("P")
    if "ep1" not in out and early_scores:
        out["ep1"] = average(early_scores)
    if "lp" not in out:
        if late_scores:
            out["lp"] = average(late_scores)
        elif performances:
            # No calls in the extraction: result proxy keeps the field rank usable.
            out["lp"] = average(performances)
    if "style" not in out and styles:
        # Most frequent inferred style; ties follow first occurrence (recent race).
        out["style"] = max(dict.fromkeys(styles), key=styles.count)

    # If no call-position data exists, use running style as a coarse early-pace
    # proxy. This is intentionally less precise than an actual pace figure.
    if "ep1" not in out and out.get("style") in {"E", "E/P", "P", "S"}:
        out["ep1"] = {"E": 90.0, "E/P": 75.0, "P": 55.0, "S": 30.0}[out["style"]]


def derive_from_history(out, horse):
    """Use explicitly labelled per-race stats only; do not infer class/speed from prose."""
    history = get_history(horse)
    if not history:
        return

    speed_values, distance_values, class_values, early_values, late_values = [], [], [], [], []
    for race in history:
        if not isinstance(race, dict):
            continue
        speed = number(first(race, ("speed", "speed_figure", "equibase_speed", "speed_rating")))
        if speed is not None:
            speed_values.append(speed)
        dist = number(first(race, ("distance_figure", "distance_rating", "dist_figure")))
        if dist is not None:
            distance_values.append(dist)
        cls = number(first(race, ("class_figure", "class_rating", "class")))
        if cls is not None:
            class_values.append(cls)
        early = number(first(race, ("ep1", "early_pace", "early_pace_figure", "pace_1")))
        late = number(first(race, ("lp", "late_pace", "late_pace_figure")))
        if early is not None:
            early_values.append(early)
        if late is not None:
            late_values.append(late)

    if "avgspd" not in out and speed_values:
        out["avgspd"] = average(speed_values)
    if "bestspd" not in out and speed_values:
        out["bestspd"] = max(speed_values)
    if "avgdist" not in out and distance_values:
        out["avgdist"] = average(distance_values)
    if "lastclass" not in out and class_values:
        out["lastclass"] = class_values[0]
    if "avgclass" not in out and class_values:
        out["avgclass"] = average(class_values)
    if "ep1" not in out and early_values:
        out["ep1"] = early_values[0]
    if "lp" not in out and late_values:
        out["lp"] = average(late_values)


def convert_horse(horse, index, warnings, current_distance=None, current_surface=None):
    if not isinstance(horse, dict):
        warnings.append(f"Horse entry #{index} is not an object; skipped.")
        return None

    out = {}
    for target, aliases in ALIASES.items():
        value = first(horse, aliases)
        if value is None:
            continue
        if target in {"pp", "ep1", "lp", "avgspd", "avgdist", "bestspd",
                      "primepower", "lastclass", "avgclass"}:
            parsed = number(value)
            if parsed is not None:
                out[target] = int(parsed) if target == "pp" and parsed.is_integer() else parsed
        elif target == "pl":
            parsed = odds_to_decimal(value)
            if parsed is not None:
                out[target] = parsed
        elif target in {"scratched", "duel", "lone"}:
            out[target] = bool(value)
        elif target == "ranks" and isinstance(value, dict):
            out[target] = value
        elif target in {"name", "style", "trainer"}:
            out[target] = str(value).strip()

    # Some extractors group values under "pace" and figures under "figures".
    pace = horse.get("pace")
    if isinstance(pace, dict):
        for target, aliases in {
            "ep1": ("ep1", "early", "early_pace"),
            "lp": ("lp", "late", "late_pace"),
        }.items():
            if target not in out:
                parsed = number(first(pace, aliases))
                if parsed is not None:
                    out[target] = parsed

    figures = horse.get("figures", {})
    if isinstance(figures, dict):
        nested_map = {
            "avgspd": ("speed", "speed_figures", "speed_rating"),
            "avgdist": ("distance", "distance_figures", "distance_rating"),
            "primepower": ("primepower", "prime_power"),
            "lastclass": ("lastclass", "last_class"),
            "avgclass": ("avgclass", "average_class"),
        }
        for target, aliases in nested_map.items():
            if target not in out:
                vals = as_numeric_list(first(figures, aliases))
                if vals:
                    out[target] = max(vals) if target == "bestspd" else (
                        vals[0] if target == "lastclass" else average(vals)
                    )
        if "bestspd" not in out:
            vals = as_numeric_list(first(figures, ("speed", "speed_figures", "speed_rating")))
            if vals:
                out["bestspd"] = max(vals)

    # A plain list of speed figures is a common extraction shape.
    for target, aliases, mode in (
        ("avgspd", ("speed_figures", "speed_ratings"), "avg"),
        ("avgdist", ("distance_figures", "distance_ratings"), "avg"),
        ("lastclass", ("class_figures", "class_ratings"), "first"),
        ("avgclass", ("class_figures", "class_ratings"), "avg"),
    ):
        if target not in out:
            vals = as_numeric_list(first(horse, aliases))
            if vals:
                out[target] = vals[0] if mode == "first" else average(vals)
    if "bestspd" not in out:
        vals = as_numeric_list(first(horse, ("speed_figures", "speed_ratings")))
        if vals:
            out["bestspd"] = max(vals)

    derive_from_history(out, horse)
    before_proxies = set(out)
    derive_raw_pp_proxies(out, horse, current_distance, current_surface)
    derived_fields = sorted(set(out) - before_proxies)
    if derived_fields:
        # Extra metadata is ignored by handicap.py but makes estimated values auditable.
        out["derived_fields"] = derived_fields

    if not out.get("name"):
        warnings.append(f"Horse entry #{index} has no name; skipped.")
        return None
    # handicap.py's formulas use these keys; missing values are intentionally not fabricated.
    if "pl" not in out:
        warnings.append(f"{out['name']}: no usable odds found; using 99 (longshot default).")
        out["pl"] = 99
    if "pp" not in out:
        warnings.append(f"{out['name']}: no program/post number found; omitting pp.")
    if "trainer" not in out:
        warnings.append(f"{out['name']}: trainer missing; handicap.py will use its default trainer tier.")
    if "style" not in out:
        warnings.append(f"{out['name']}: running style missing; provide E, E/P, P, or S for better scoring.")
    for field in ("ep1", "lp", "avgspd", "avgdist", "bestspd", "primepower", "lastclass"):
        if field not in out:
            warnings.append(f"{out['name']}: '{field}' could not be derived from the available past-performance data; handicap.py will use its default/last rank.")
    return out


def convert_race(data):
    if not isinstance(data, dict):
        raise ValueError("Input JSON root must be an object.")
    race_name = first(data, ("name", "race_name", "title")) or "Race"
    distance = first(data, ("distance_f", "distance_furlongs", "distance"))
    distance_f = distance_to_furlongs(distance)
    if distance_f is None:
        raise ValueError("Race distance is missing or unrecognized. Supply distance_f, e.g. 6 or 8.5.")
    surface = str(first(data, ("surface", "track_surface")) or "dirt").strip().lower()
    if "turf" in surface or surface in {"grass", "fm", "gd turf"}:
        surface = "turf"
    else:
        surface = "dirt"

    horses_in = first(data, ("horses", "entries", "runners"))
    if not isinstance(horses_in, list):
        raise ValueError("Input JSON must contain a 'horses' (or 'entries'/'runners') list.")
    warnings = []
    horses_out = []
    for i, horse in enumerate(horses_in, 1):
        converted = convert_horse(horse, i, warnings, distance_f, surface)
        if converted is not None:
            horses_out.append(converted)
    if not horses_out:
        raise ValueError("No valid named horses were found.")
    return {"name": str(race_name), "distance_f": distance_f, "surface": surface,
            "horses": horses_out}, warnings


def main():
    parser = argparse.ArgumentParser(description="Convert past-performance JSON to handicap.py race JSON.")
    parser.add_argument("input_json", help="JSON extracted from the past-performance PDF")
    parser.add_argument("-o", "--output", help="Output race JSON path (default: stdout)")
    args = parser.parse_args()

    try:
        with open(args.input_json, "r", encoding="utf-8") as source:
            data = json.load(source)
        race, warnings = convert_race(data)
        rendered = json.dumps(race, indent=2, ensure_ascii=False)
        if args.output:
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
            print(f"Wrote handicap-compatible JSON to {args.output}")
        else:
            print(rendered)
        for warning in warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
