"""
Handicapping engine: v8 (locked), v9 (shadow), and PP-sheet derived mode.
All scores are on a 0-100 scale.

Usage:
    python handicap.py                    # runs the Belmont S. demo
    python handicap.py race.json          # scores a race from a JSON file

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
# Output
# --------------------------------------------------------------------------
def show(race, version="v8"):
    kind, res = score_race(race, version)
    print(f"\n{race['name']}  |  {kind.upper()} {version}  |  /100")
    print("-" * 64)
    for i, (name, tot, parts, tags) in enumerate(res, 1):
        print(f"{i:>2}. {name:<18}{tot:>5}   {' '.join(tags)}")
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

if __name__ == "__main__":
    race = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else DEMO
    show(race, "v8")
    show(race, "v9")  # shadow comparison
