"""College football quarterback stats for the Players view, used by fetch_cfb.py.

Writes data/cfb/qbs.json: one row per quarterback per game with
  - box score passing (completions, attempts, yards, TD, INT) from CollegeFootballData's game box scores
  - EPA per dropback from play-by-play (passes and sacks, garbage time excluded); scrambles can't be
    separated from designed runs, so they aren't counted
  - CPOE inputs: completions on charted attempts and the expected completions for those throws

CPOE (completion percentage over expected) uses a completion-probability model built from every pass
this season: league completion rates by pass depth (short/deep), direction (left/middle/right), down,
yards to go and field position, as recorded in the play-by-play. Sparse combinations borrow from broader
ones (depth and direction, then depth alone). College play-by-play has no air yards, so this is simpler
than the NFL version, but it does account for how often a quarterback throws deep.

Weeks are downloaded once and cached in data/cfb/qbgames.json.
"""
import json
import re
import sys
import time
import unicodedata

PASS_TYPES = {"Pass Reception", "Pass Incompletion", "Passing Touchdown", "Pass Interception Return",
              "Interception", "Interception Return Touchdown", "Pass", "Pass Completion"}
COMPLETE_TYPES = {"Pass Reception", "Passing Touchdown", "Pass Completion"}
SMOOTH = 20  # attempts of weight given to the broader rate when a combination is sparse

QB_COLS = ["gameId", "week", "player", "team", "opp", "confGame", "comp", "att", "yds", "td", "int",
           "db", "epa", "cpAtt", "cpComp", "cpExp"]


def plain(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def name_key(name):
    """('k', 'russell') from 'K.Russell', 'Keelon Russell' or 'Keelon Russell Jr.'"""
    parts = [p for p in re.split(r"[\s.]+", plain(name).lower()) if p and p.strip("'-") and p not in SUFFIXES]
    if not parts:
        return None
    return (parts[0][0], re.sub(r"[^a-z]", "", parts[-1]))


PASSER = re.compile(r"(?:#\d+\s+)?([A-Z][\w'.\-]*(?:\s+[A-Z][\w'.\-]*)*?)\s+(?:pass\s+(?:complete|incomplete|intercepted)|sacked)\b")
PASS_FROM = re.compile(r"pass from ([A-Z][\w'.\- ]+?)(?:\s*\(|$)")


def passer_of(text):
    m = PASS_FROM.search(text or "")
    if m:
        return m.group(1).strip()
    m = PASSER.search(text or "")
    return m.group(1).strip() if m else None


def bucket(play):
    """Completion-model features for one pass attempt."""
    t = " " + (play.get("playText") or "").lower() + " "
    depth = "deep" if " deep " in t else "short" if " short " in t else "unk"
    side = "left" if " left" in t else "right" if " right" in t else "middle" if " middle" in t else "unk"
    down = min(play.get("down") or 1, 3)
    dist = play.get("distance") or 10
    distb = "1-3" if dist <= 3 else "4-6" if dist <= 6 else "7-10" if dist <= 10 else "11+"
    ytg = play.get("yardsToGoal") or 50
    zone = "rz" if ytg <= 20 else "open"
    return f"{depth}|{side}|{down}|{distb}|{zone}"


def week_qb_plays(plays, is_garbage_time, match_team, games_by_id, teams):
    """{"gameId|teamId": {passer name: {"db", "epa", "buckets": {bucket: [att, comp]}}}} for one week."""
    out = {}
    for p in plays:
        ptype = p.get("playType")
        if ptype not in PASS_TYPES and ptype != "Sack":
            continue
        if p.get("down") not in (1, 2, 3, 4) or is_garbage_time(p):
            continue
        name = passer_of(p.get("playText"))
        if not name:
            continue
        tid = match_team(p.get("gameId"), p.get("offense"), p.get("defense"), games_by_id, teams)
        if not tid:
            continue
        rec = out.setdefault(f"{p['gameId']}|{tid}", {}).setdefault(name, {"db": 0, "epa": 0.0, "buckets": {}})
        if p.get("ppa") is not None:
            rec["db"] += 1
            rec["epa"] += p["ppa"]
        if ptype in PASS_TYPES:
            b = rec["buckets"].setdefault(bucket(p), [0, 0])
            b[0] += 1
            b[1] += ptype in COMPLETE_TYPES
    for recs in out.values():
        for r in recs.values():
            r["epa"] = round(r["epa"], 3)
    return out


def week_qb_box(rows, match_team, games_by_id, teams):
    """{"gameId|athleteId": {...}} from CollegeFootballData /games/players passing box scores."""
    out = {}
    for game in rows:
        gid = str(game.get("id"))
        if gid not in games_by_id:
            continue
        sides = game.get("teams") or []
        for i, t in enumerate(sides):
            opp_name = sides[1 - i].get("team") if len(sides) == 2 else None
            tid = match_team(gid, t.get("team"), opp_name, games_by_id, teams)
            if not tid:
                continue
            per = {}
            for cat in t.get("categories", []):
                if cat.get("name") != "passing":
                    continue
                for ty in cat.get("types", []):
                    for a in ty.get("athletes", []):
                        per.setdefault(str(a["id"]), {"name": a.get("name", "")})[ty["name"]] = a.get("stat")
            for aid, s in per.items():
                try:
                    comp, att = (int(x) for x in str(s.get("C/ATT", "0/0")).split("/"))
                except ValueError:
                    continue
                if att <= 0 or aid.startswith("-"):
                    continue
                num = lambda k: int(float(str(s.get(k) or 0).replace(",", "") or 0))
                out[f"{gid}|{aid}"] = {"team": tid, "name": s["name"], "comp": comp, "att": att,
                                       "yds": num("YDS"), "td": num("TD"), "int": num("INT")}
    return out


def build(year, weeks, games_by_id, teams, results_meta, key, cache_path, out_path,
          cfbd_get, is_garbage_time, match_team, load):
    """Update the cache for weeks with newly completed games, then write data/cfb/qbs.json."""
    cache = load(cache_path, {"box": {}, "plays": {}})
    if key:
        for w in weeks:
            done = [g["id"] for g in w["games"] if g["state"] == "post" and g["id"] in games_by_id]
            if not done:
                continue
            need_box = any(not any(k.startswith(gid + "|") for k in cache["box"]) for gid in done)
            need_plays = any(not any(k.startswith(gid + "|") for k in cache["plays"]) for gid in done)
            if not need_box and not need_plays:
                continue
            stype = "regular" if w["stype"] == 2 else "postseason"
            wk = w["week"] if stype == "regular" else 1
            try:
                if need_box:
                    cache["box"].update(week_qb_box(cfbd_get(f"/games/players?year={year}&week={wk}&seasonType={stype}&category=passing", key),
                                                    match_team, games_by_id, teams))
                if need_plays:
                    cache["plays"].update(week_qb_plays(cfbd_get(f"/plays?year={year}&week={wk}&seasonType={stype}", key),
                                                        is_garbage_time, match_team, games_by_id, teams))
                print(f"QB stats {stype} week {wk} downloaded")
            except Exception as exc:
                print(f"  QB stats {stype} week {wk}: {exc}", file=sys.stderr)
            time.sleep(0.5)
        cache_path.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True))
    else:
        print("No CFBD_API_KEY set; quarterback stats use the saved data.")

    # League completion model from every charted attempt
    full, by_dir, by_depth, total = {}, {}, {}, [0, 0]
    for recs in cache["plays"].values():
        for r in recs.values():
            for b, (a, c) in r["buckets"].items():
                depth, side = b.split("|")[:2]
                for table, k in ((full, b), (by_dir, f"{depth}|{side}"), (by_depth, depth)):
                    x = table.setdefault(k, [0, 0])
                    x[0] += a
                    x[1] += c
                total[0] += a
                total[1] += c
    overall = total[1] / total[0] if total[0] else 0.6

    def rate(b):
        depth, side = b.split("|")[:2]
        d = by_depth.get(depth, [0, 0])
        r_depth = (d[1] + SMOOTH * overall) / (d[0] + SMOOTH)
        s = by_dir.get(f"{depth}|{side}", [0, 0])
        r_dir = (s[1] + SMOOTH * r_depth) / (s[0] + SMOOTH)
        f = full.get(b, [0, 0])
        return (f[1] + SMOOTH * r_dir) / (f[0] + SMOOTH)

    rows, players = [], {}
    for k, box in cache["box"].items():
        gid, aid = k.split("|")
        meta = results_meta.get((gid, box["team"]))
        if not meta:
            continue
        week, opp, conf = meta
        # Play-by-play lines for this passer in this game: every spelling with the same first initial and
        # last name ("K.Russell", "Keelon Russell"); if none, the same last name when it's unique among
        # the team's passers that game (nicknames: "Goose" for Will Crowder).
        recs = cache["plays"].get(f"{gid}|{box['team']}") or {}
        nk = name_key(box["name"])
        parts = [rec for pname, rec in recs.items() if name_key(pname) == nk]
        if not parts and nk:
            same_last = [k2 for k2, b2 in cache["box"].items() if k2.startswith(gid + "|") and b2["team"] == box["team"]
                         and (name_key(b2["name"]) or ("", ""))[1] == nk[1]]
            if len(same_last) == 1:
                parts = [rec for pname, rec in recs.items() if (name_key(pname) or ("", ""))[1] == nk[1]]
        mine = None
        if parts:
            mine = {"db": sum(r["db"] for r in parts), "epa": sum(r["epa"] for r in parts), "buckets": {}}
            for r in parts:
                for b, (a, c) in r["buckets"].items():
                    x = mine["buckets"].setdefault(b, [0, 0])
                    x[0] += a
                    x[1] += c
        db = mine["db"] if mine else 0
        epa = mine["epa"] if mine else 0.0
        cp_att = sum(a for a, _ in mine["buckets"].values()) if mine else 0
        cp_comp = sum(c for _, c in mine["buckets"].values()) if mine else 0
        cp_exp = sum(a * rate(b) for b, (a, _) in mine["buckets"].items()) if mine else 0.0
        rows.append([gid, week, aid, box["team"], opp, conf, box["comp"], box["att"], box["yds"], box["td"], box["int"],
                     db, round(epa, 3), cp_att, cp_comp, round(cp_exp, 2)])
        players[aid] = {"name": box["name"], "team": box["team"]}
    rows.sort(key=lambda r: (r[1], r[0], r[2]))
    matched = sum(1 for r in rows if r[11])
    out_path.write_text(json.dumps({"cols": QB_COLS, "players": dict(sorted(players.items())), "rows": rows,
                                    "leagueCompletion": round(overall, 4)}, separators=(",", ":")))
    print(f"Saved {len(rows)} quarterback-games for {len(players)} passers "
          f"({matched} with play-by-play; league completion rate {overall:.1%})")
