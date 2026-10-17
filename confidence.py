"""
Confidence score for a handicapped race (0-100): how much to trust the top pick.

This is a heuristic, NOT a calibrated win probability. Thresholds below are
starting guesses; backtest against results before relying on the labels.

Usage:
    python confidence.py                  # Belmont demo
    python confidence.py race.json        # your own race

Components (each returns 0.0-1.0; weighted to sum to 100):
    margin     (35)  gap between #1 and #2 totals
    agreement  (25)  v8 and v9 agree on the top pick / top three
    data       (20)  how many key inputs are actually filled in
    field      (10)  smaller fields are more predictable
    pace       (10)  a speed duel makes the race shape less certain
"""
import json
import sys

from handicap import DEMO, pace_scenario, score_race

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------
W_MARGIN, W_AGREEMENT, W_DATA, W_FIELD, W_PACE = 35, 25, 20, 10, 10

MARGIN_FULL = 10.0          # point gap at which margin confidence maxes out
FIELD_SMALL, FIELD_BIG = 6, 14   # field size: <=6 full confidence, >=14 floor
FIELD_FLOOR = 0.4
DUEL2_CONF, DUEL3_CONF = 0.7, 0.4

HIGH_CUTOFF, MEDIUM_CUTOFF = 70, 45

# Inputs the scorers read; a missing one silently counts as 0 or a default.
DATA_FIELDS = ("ep1", "lp", "avgspd", "avgdist", "primepower",
               "lastclass", "pl", "trainer")

# --------------------------------------------------------------------------
# One function per component
# --------------------------------------------------------------------------
def conf_margin(res):
    """Gap between the top two totals, scaled so MARGIN_FULL points = 1.0."""
    if len(res) < 2:
        return 1.0
    gap = res[0][1] - res[1][1]
    return min(gap / MARGIN_FULL, 1.0)

def conf_agreement(res_v8, res_v9):
    """60% weight on same top pick, 40% on top-three overlap between versions."""
    same_top = 1.0 if res_v8[0][0] == res_v9[0][0] else 0.0
    top_v8 = {r[0] for r in res_v8[:3]}
    top_v9 = {r[0] for r in res_v9[:3]}
    overlap = len(top_v8 & top_v9) / min(3, len(res_v8))
    return 0.6 * same_top + 0.4 * overlap

def conf_data(horses):
    """Fraction of DATA_FIELDS present across all running horses."""
    if not horses:
        return 0.0
    total = len(horses) * len(DATA_FIELDS)
    present = sum(1 for h in horses for f in DATA_FIELDS if h.get(f) is not None)
    return present / total

def conf_field(n_horses):
    """1.0 for small fields, sliding linearly down to FIELD_FLOOR for big ones."""
    if n_horses <= FIELD_SMALL:
        return 1.0
    if n_horses >= FIELD_BIG:
        return FIELD_FLOOR
    span = FIELD_BIG - FIELD_SMALL
    return 1.0 - (1.0 - FIELD_FLOOR) * (n_horses - FIELD_SMALL) / span

def conf_pace(horses):
    """Lone speed or no speed = clean pace picture; duels add uncertainty."""
    _, duel = pace_scenario(horses)
    if len(duel) >= 3:
        return DUEL3_CONF
    if len(duel) == 2:
        return DUEL2_CONF
    return 1.0

def label_for(score):
    if score >= HIGH_CUTOFF:
        return "HIGH"
    if score >= MEDIUM_CUTOFF:
        return "MEDIUM"
    return "LOW"

# --------------------------------------------------------------------------
# Combine
# --------------------------------------------------------------------------
def race_confidence(race):
    """Returns dict with the score, label, top pick, and each component (0-1)."""
    horses = [h for h in race["horses"] if not h.get("scratched")]
    if not horses:
        return {"score": 0, "label": "LOW", "top_pick": None, "components": {}}

    _, res_v8 = score_race(race, "v8")
    _, res_v9 = score_race(race, "v9")

    margin = conf_margin(res_v8)
    agreement = conf_agreement(res_v8, res_v9)
    data = conf_data(horses)
    field = conf_field(len(horses))
    pace = conf_pace(horses)

    score = round(W_MARGIN * margin + W_AGREEMENT * agreement + W_DATA * data
                  + W_FIELD * field + W_PACE * pace)
    return {
        "score": score,
        "label": label_for(score),
        "top_pick": res_v8[0][0],
        "components": {"margin": margin, "agreement": agreement,
                       "data": data, "field": field, "pace": pace},
    }

def show_confidence(race):
    c = race_confidence(race)
    print(f"\n{race['name']}  |  CONFIDENCE")
    print("-" * 64)
    print(f"Top pick: {c['top_pick']}   Score: {c['score']}/100 ({c['label']})")
    for name, val in c["components"].items():
        print(f"  {name:<10}{val:>6.2f}")
    return c

if __name__ == "__main__":
    race = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else DEMO
    show_confidence(race)