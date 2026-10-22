"""
Handicapping engine: v8 (locked), v9 (shadow), and PP-sheet derived mode.
All scores are on a 0-100 scale.

Usage:
    python handicap.py                    # Belmont S. demo -> JSON (v8)
    python handicap.py race.json          # scores a race from a JSON file -> JSON
    python handicap.py race.json --v9     # same, using the v9 shadow model
    python handicap.py race.json --text   # printed table + confidence instead of JSON

Each output row also carries a race-level confidence score (0-100) and label
(HIGH / MEDIUM / LOW): how much to trust the top pick. It is a heuristic, NOT a
calibrated win probability; backtest the thresholds before relying on the labels.

Race JSON shape:
{
  "name": "Belmont S. G1",
  "distance_f": 10,            # furlongs (8 = 1 mile)
  "surface": "dirt",           # "dirt" or "turf"
  "horses": [
     {"name": "Renegade", "pp": 4, "pl": 2.5, "style": "S", "trainer": "Pletcher",
      "ep1": 75, "lp": 105, "avgspd": 97, "avgdist": 98, "bestspd": 98,
      "primepower": 150, "lastclass": 122, "avgclass": 121}
  ]
}
Ranks are computed automatically from the raw numbers (higher = better, ties share
a rank). Add e.g. "ranks": {"lp": 2} to a horse to override what the app showed.
Optional per-horse flags: "scratched": true, "duel": true/false, "lone": true.
"""
import json
import math
import sys

# --------------------------------------------------------------------------
# Rank -> points tables
# --------------------------------------------------------------------------
def tbl(d, default):
    return lambda r: d.get(r, default)

ROUTE = {  # v8, /100
    "lp":   tbl({1:22,2:19,3:17,4:15,5:13,6:11,7:8,8:5,9:3}, 3),
    "cls":  tbl({1:17,2:16,3:15,4:14,5:13,6:12,7:10,8:8,9:5}, 5),
    "spd":  tbl({1:17,2:15,3:14,4:12,5:10,6:8,7:6,8:5,9:3}, 3),
    "dist": tbl({1:13,2:11,3:10,4:8,5:6,6:5,7:3,8:1,9:0}, 0),
    "form": tbl({1:13,2:12,3:11,4:10,5:9,6:8,7:6,8:4,9:0}, 0),
    "trainer_max": 9, "workout_max": 9,
    "presser": 4, "closer": 3, "lone": 4, "duel": {2: -3, 3: -3},
}
ROUTE_V9 = dict(ROUTE,  # class upweighted, speed/LP trimmed
    lp=tbl({1:20,2:17,3:15,4:13,5:11,6:9,7:7,8:4,9:2}, 2),
    cls=tbl({1:21,2:19,3:18,4:16,5:15,6:13,7:11,8:8,9:5}, 5),
    spd=tbl({1:15,2:13,3:12,4:10,5:8,6:7,7:5,8:4,9:2}, 2),
    dist=tbl({1:12,2:10,3:9,4:7,5:6,6:4,7:3,8:1,9:0}, 0),
    form=tbl({1:12,2:11,3:10,4:9,5:8,6:7,7:5,8:3,9:0}, 0),
    trainer_max=10, workout_max=10)
TURF = dict(ROUTE, duel={2: -5, 3: -5})

SPRINT = {  # v8, /100
    "spd":  tbl({1:19,2:17,3:14,4:12,5:9,6:7}, 5),
    "best": {1: 4, 2: 2, 3: 1},
    "cls":  tbl({1:19,2:17,3:15,4:13,5:10,6:8}, 5),
    "pace": tbl({1:15,2:13,3:11,4:8,5:6,6:4}, 2),
    "post": tbl({1:8,2:8,3:7,4:6,5:5,6:5,7:4,8:4}, 4),
    "drop": tbl({1:8,2:7,3:6,4:5,5:3,6:0}, 0),
    "style_cap": 12, "spd_cap": 19, "trainer_max": 8,
    "presser": 4, "duel": {2: -3, 3: -5},
}
SPRINT_V9 = dict(SPRINT,  # class upweighted
    spd=tbl({1:17,2:15,3:12,4:10,5:8,6:6}, 4),
    best={1: 3, 2: 2, 3: 1},
    cls=tbl({1:23,2:21,3:18,4:15,5:12,6:9}, 6),
    pace=tbl({1:14,2:12,3:10,4:8,5:6,6:4}, 2),
    post=tbl({1:7,2:7,3:6,4:5,5:5,6:4,7:4,8:4}, 4),
    drop=tbl({1:9,2:8,3:7,4:5,5:3,6:0}, 0),
    style_cap=11, spd_cap=17, trainer_max=7)

TRAINER_TIER = {  # fraction of max trainer points; edit freely
    "baffert": 1.0, "pletcher": 1.0, "brown": 1.0, "c.brown": 1.0, "cox": 1.0,
    "mott": 0.9, "ward": 0.9, "asmussen": 0.8, "d. ryan": 0.8, "sharp": 0.8,
    # --- added: tiers are judgment calls, not computed from stats ---
    "joseph": 0.9, "casse": 0.9, "mcgaughey": 0.9, "mcpeek": 0.9,
    "motion": 0.8, "clement": 0.8, "romans": 0.8, "amoss": 0.8, "maker": 0.8,
    "sadler": 0.8, "mandella": 0.8, "miller": 0.8, "d'amato": 0.8,
    "hollendorfer": 0.8, "o'neill": 0.8, "devaux": 0.8, "weaver": 0.8,
    "rice": 0.8, "brisset": 0.8, "walsh": 0.8, "mccarthy": 0.8,
}

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def ranks(horses, key):
    """Competition ranking, higher is better. Missing/zero values rank last."""
    vals = [(h.get(key) or 0) for h in horses]
    return [1 + sum(v2 > v for v2 in vals) for v in vals]

def market_pts(pl, top):  # top = max points for market/workout factor
    steps = [(2, 1.0), (3, .85), (5, .75), (8, .6), (12, .45), (20, .3)]
    for lim, frac in steps:
        if pl <= lim:
            return round(top * frac)
    return round(top * .1)

def trainer_pts(name, mx):
    return round(mx * TRAINER_TIER.get((name or "").lower(), .7))

def pace_scenario(horses):
    """Returns (lone_speed_horse, duel_group). Thresholds from this session:
    lone = EP1 lead >= 8 over the next horse; duel = others within 4 of top EP1."""
    ep = sorted(((h.get("ep1") or 0, h["name"]) for h in horses), reverse=True)
    if len(ep) < 2:
        return None, []
    top, second = ep[0][0], ep[1][0]
    if top - second >= 8:
        return ep[0][1], []
    group = [n for v, n in ep if top - v <= 4]
    return None, (group if len(group) >= 2 else [])

# --------------------------------------------------------------------------
# Scorers
# --------------------------------------------------------------------------
def score_route_like(horses, cfg):
    lone, duel = pace_scenario(horses)
    R = {k: ranks(horses, k) for k in
         ("lp", "avgspd", "primepower", "avgdist", "lastclass")}
    out = []
    for i, h in enumerate(horses):
        r = lambda k: h.get("ranks", {}).get(k, R[k][i])
        parts = {
            "lp": cfg["lp"](r("lp")), "class": cfg["cls"](r("primepower")),
            "speed": cfg["spd"](r("avgspd")), "dist": cfg["dist"](r("avgdist")),
            "form": cfg["form"](r("lastclass")),
            "trainer": trainer_pts(h.get("trainer"), cfg["trainer_max"]),
            "workout": market_pts(h.get("pl", 99), cfg["workout_max"]),
        }
        adj, tags = 0, []
        st = h.get("style", "")
        if st == "P":
            adj += cfg["presser"]; tags.append("PRESSER")
        if st == "S":
            adj += cfg["closer"]; tags.append("CLOSER")
        is_lone = h.get("lone", h["name"] == lone)
        if is_lone:
            adj += cfg["lone"]; tags.append("LONE_SPEED")
        in_duel = h.get("duel", h["name"] in duel)
        if in_duel:
            adj += cfg["duel"].get(min(len(duel), 3), -3); tags.append("DUEL")
        parts["adj"] = adj
        out.append((h["name"], sum(parts.values()), parts, tags))
    return out

def score_sprint(horses, cfg):
    lone, duel = pace_scenario(horses)
    R = {k: ranks(horses, k) for k in
         ("lp", "avgspd", "primepower", "lastclass", "bestspd")}
    out = []
    for i, h in enumerate(horses):
        r = lambda k: h.get("ranks", {}).get(k, R[k][i])
        best = cfg["best"].get(r("bestspd"), 0) if h.get("bestspd") else 0
        st = h.get("style", "E/P")
        base = {"E": 12, "E/P": 12, "P": 10, "S": 8}.get(st, 8)
        tags = []
        if st == "P":
            base += cfg["presser"]; tags.append("PRESSER")
        if best:
            tags.append(f"BESTSPD{r('bestspd')}")
        parts = {
            "speed": min(cfg["spd"](r("avgspd")) + best, cfg["spd_cap"]),
            "class": cfg["cls"](r("primepower")),
            "pace": cfg["pace"](r("lp")),
            "post": cfg["post"](h.get("pp", 99)),
            "market": market_pts(h.get("pl", 99), 12),
            "style": min(base, cfg["style_cap"]),
            "drop": cfg["drop"](r("lastclass")),
            "trainer": trainer_pts(h.get("trainer"), cfg["trainer_max"]),
        }
        if h.get("lone", h["name"] == lone):
            parts["lone"] = 4; tags.append("LONE_SPEED")
        if h.get("duel", h["name"] in duel):
            parts["duel"] = cfg["duel"].get(min(len(duel), 3), -3); tags.append("DUEL")
        out.append((h["name"], sum(parts.values()), parts, tags))
    return out

def pick_formula(distance_f, surface):
    if surface == "turf":
        return "turf"
    return "sprint" if distance_f < 8 else "route"

def score_race(race, version="v8"):
    horses = [h for h in race["horses"] if not h.get("scratched")]
    kind = pick_formula(race["distance_f"], race.get("surface", "dirt"))
    v9 = version == "v9"
    if kind == "sprint":
        res = score_sprint(horses, SPRINT_V9 if v9 else SPRINT)
    else:
        res = score_route_like(horses, (ROUTE_V9 if v9 else (TURF if kind == "turf" else ROUTE)))
    res.sort(key=lambda x: -x[1])
    return kind, res

# --------------------------------------------------------------------------
# PP-sheet derived mode (lower confidence; see standing addendum)
# --------------------------------------------------------------------------
FOREIGN_FLOOR = {"G1": 100, "G2": 96, "G3": 92, "LISTED": 88, None: 85}
CLASS_PTS = {"stk750": 21, "stk500": 19, "stk300": 17, "stk225": 16, "stk200": 18,
             "stk170": 14, "stk125": 13, "msw120": 10, "msw100": 9, "msw32": 5}

def score_pp_sheet(h, route_distance=False):
    """h keys: spd (Equibase fig or None), foreign_grade, class (key of CLASS_PTS),
    style, trainer_pts, jockey_pts, odds, life ('5-3-2-0'), sprint_only (bool)."""
    spd = h.get("spd") or FOREIGN_FLOOR.get(h.get("foreign_grade"), 85)
    s = min(spd * .25, 26) + CLASS_PTS.get(h.get("class"), 8)
    s += {"E": 3, "E/P": 4, "P": 2, "S": 2}.get(h.get("style"), 0)
    s += h.get("trainer_pts", 5) + h.get("jockey_pts", 5)
    odds = h.get("odds", 99)
    s += next(p for lim, p in [(2,14),(4,12),(6,10),(9,8),(13,6),(16,4),(1e9,2)] if odds <= lim)
    starts, wins = (int(x) for x in h.get("life", "0-0").split("-")[:2])
    s += min((wins / starts if starts else 0) * 8 + min(starts, 4), 10)
    if route_distance and h.get("sprint_only"):  # standing rule: first route try
        s -= 4
    return round(s, 1)

# --------------------------------------------------------------------------
# Race confidence (0-100): how much to trust the top pick
#   margin     (35)  gap between #1 and #2 totals
#   agreement  (25)  v8 and v9 agree on the top pick / top three
#   data       (20)  how many key inputs are actually filled in
#   field      (10)  smaller fields are more predictable
#   pace       (10)  a speed duel makes the race shape less certain
# Each component returns 0.0-1.0 and is weighted so the weights sum to 100.
# Thresholds are starting guesses.
# --------------------------------------------------------------------------
W_MARGIN, W_AGREEMENT, W_DATA, W_FIELD, W_PACE = 35, 25, 20, 10, 10

MARGIN_FULL = 10.0                # point gap at which margin confidence maxes out
FIELD_SMALL, FIELD_BIG = 6, 14    # field size: <=6 full confidence, >=14 floor
FIELD_FLOOR = 0.4
DUEL2_CONF, DUEL3_CONF = 0.7, 0.4
HIGH_CUTOFF, MEDIUM_CUTOFF = 70, 45

# Inputs the scorers read; a missing one silently counts as 0 or a default.
DATA_FIELDS = ("ep1", "lp", "avgspd", "avgdist", "primepower",
               "lastclass", "pl", "trainer")

def conf_margin(res):
    """Gap between the top two totals, scaled so MARGIN_FULL points = 1.0."""
    if len(res) < 2:
        return 1.0
    return min((res[0][1] - res[1][1]) / MARGIN_FULL, 1.0)

def conf_agreement(res, res_other):
    """60% weight on same top pick, 40% on top-three overlap between versions."""
    same_top = 1.0 if res[0][0] == res_other[0][0] else 0.0
    overlap = len({r[0] for r in res[:3]} & {r[0] for r in res_other[:3]}) / min(3, len(res))
    return 0.6 * same_top + 0.4 * overlap

def conf_data(horses):
    """Fraction of DATA_FIELDS present across all running horses."""
    present = sum(1 for h in horses for f in DATA_FIELDS if h.get(f) is not None)
    return present / (len(horses) * len(DATA_FIELDS))

def conf_field(n_horses):
    """1.0 for small fields, sliding linearly down to FIELD_FLOOR for big ones."""
    if n_horses <= FIELD_SMALL:
        return 1.0
    if n_horses >= FIELD_BIG:
        return FIELD_FLOOR
    slide = (n_horses - FIELD_SMALL) / (FIELD_BIG - FIELD_SMALL)
    return 1.0 - (1.0 - FIELD_FLOOR) * slide

def conf_pace(horses):
    """Lone speed or no speed = clean pace picture; duels add uncertainty."""
    _, duel = pace_scenario(horses)
    if len(duel) >= 3:
        return DUEL3_CONF
    if len(duel) == 2:
        return DUEL2_CONF
    return 1.0

def confidence_label(score):
    if score >= HIGH_CUTOFF:
        return "HIGH"
    if score >= MEDIUM_CUTOFF:
        return "MEDIUM"
    return "LOW"

def race_confidence(race, res, res_other):
    """res = ranked results for the version being reported, res_other = the other
    version (for the agreement check). Returns score, label and each component."""
    horses = [h for h in race["horses"] if not h.get("scratched")]
    parts = {
        "margin": conf_margin(res),
        "agreement": conf_agreement(res, res_other),
        "data": conf_data(horses),
        "field": conf_field(len(horses)),
        "pace": conf_pace(horses),
    }
    score = round(W_MARGIN * parts["margin"] + W_AGREEMENT * parts["agreement"]
                  + W_DATA * parts["data"] + W_FIELD * parts["field"]
                  + W_PACE * parts["pace"])
    return {"score": score, "label": confidence_label(score), "components": parts}

def analyze(race, version="v8"):
    """Score the race under `version` and the other version; returns
    (formula kind, ranked results, confidence dict)."""
    other = "v9" if version == "v8" else "v8"
    kind, res = score_race(race, version)
    _, res_other = score_race(race, other)
    return kind, res, race_confidence(race, res, res_other)

# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def show(race, version="v8"):
    kind, res, conf = analyze(race, version)
    print(f"\n{race['name']}  |  {kind.upper()} {version}  |  /100")
    print("-" * 64)
    for i, (name, tot, parts, tags) in enumerate(res, 1):
        print(f"{i:>2}. {name:<18}{tot:>5}   {' '.join(tags)}")
    print(f"Confidence: {conf['score']}/100 ({conf['label']})")
    return res

DEMO = {
    "name": "Belmont S. G1", "distance_f": 10, "surface": "dirt",
    "horses": [
        {"name":"Renegade","pp":4,"pl":2.5,"style":"S","trainer":"Pletcher","ep1":75,"lp":105,"avgspd":97,"avgdist":98,"primepower":150,"lastclass":122},
        {"name":"Chief Wallabee","pp":3,"pl":4,"style":"P","trainer":"Mott","ep1":77,"lp":103,"avgspd":99,"avgdist":98,"primepower":147,"lastclass":121},
        {"name":"Golden Tempo","pp":9,"pl":5,"style":"S","trainer":"DeVaux","ep1":75,"lp":103,"avgspd":96,"avgdist":99,"primepower":147,"lastclass":122},
        {"name":"Commandment","pp":7,"pl":9,"style":"P","trainer":"Cox","ep1":74,"lp":102,"avgspd":98,"avgdist":96,"primepower":148,"lastclass":120},
        {"name":"Emerging Market","pp":8,"pl":8,"style":"S","trainer":"C.Brown","ep1":95,"lp":87,"avgspd":97,"avgdist":96,"primepower":149,"lastclass":119},
        {"name":"Vitruvian Man","pp":1,"pl":50,"style":"S","trainer":"O'Neill","ep1":95,"lp":86,"avgspd":84,"avgdist":92,"primepower":130,"lastclass":116},
    ],
}

# --------------------------------------------------------------------------
# SQL-friendly JSON output: winner + win probability per horse
# One flat row per horse. Same keys on every row.
#
# win_probability is a softmax over the 0-100 scores:
#     p_i = exp(score_i / T) / sum_j exp(score_j / T)
# T = PROB_TEMPERATURE. Smaller T -> favorite gets a bigger share.
# NOTE: this is a heuristic conversion, NOT a calibrated probability. To make it
# honest, fit T against past results (pick T that minimizes log-loss).
# --------------------------------------------------------------------------
PROB_TEMPERATURE = 8.0

def win_probabilities(scores, temp=PROB_TEMPERATURE):
    top = max(scores)  # subtract max for numeric stability
    exps = [math.exp((s - top) / temp) for s in scores]
    total = sum(exps)
    return [e / total for e in exps]

def col_win_probability(prob):         return round(prob, 4)
def col_is_predicted_winner(rank):     return rank == 1
def col_predicted_rank(rank):          return rank
def col_confidence_score(conf):        return conf["score"]
def col_confidence_label(conf):        return conf["label"]

def build_row(race, version, kind, rank, name, score, prob, conf):
    return {
        "race_name": race["name"],
        "distance_f": race["distance_f"],
        "surface": race.get("surface", "dirt"),
        "formula": kind,
        "version": version,
        "horse": name,
        "predicted_rank": col_predicted_rank(rank),
        "is_predicted_winner": col_is_predicted_winner(rank),
        "score": round(score, 1),
        "win_probability": col_win_probability(prob),
        "confidence_score": col_confidence_score(conf),
        "confidence_label": col_confidence_label(conf),
    }

def race_rows(race, version="v8"):
    kind, res, conf = analyze(race, version)
    if not res:
        return []
    probs = win_probabilities([tot for _, tot, _, _ in res])
    return [build_row(race, version, kind, i, name, tot, prob, conf)
            for i, ((name, tot, _parts, _tags), prob) in enumerate(zip(res, probs), 1)]

def to_json(race, version="v8", indent=2):
    return json.dumps(race_rows(race, version), indent=indent)

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    race = json.load(open(args[0])) if args else DEMO
    version = "v9" if "--v9" in sys.argv else "v8"
    if "--text" in sys.argv:      # old human-readable table
        show(race, version)
    else:                         # default: JSON rows for SQL
        print(to_json(race, version))