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
           "db", "epa", "cpAtt", "cpComp", "cpExp", "car", "ryds", "rtd"]


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


def week_rush_box(rows, match_team, games_by_id, teams):
    """{"gameId|athleteId": [carries, yards, TD]} from CollegeFootballData /games/players rushing box scores."""
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
                if cat.get("name") != "rushing":
                    continue
                for ty in cat.get("types", []):
                    for a in ty.get("athletes", []):
                        per.setdefault(str(a["id"]), {})[ty["name"]] = a.get("stat")
            for aid, st in per.items():
                if aid.startswith("-"):
                    continue
                num = lambda k: int(float(str(st.get(k) or 0).replace(",", "") or 0))
                out[f"{gid}|{aid}"] = [num("CAR"), num("YDS"), num("TD")]
    return out


def build(year, weeks, games_by_id, teams, results_meta, key, cache_path, out_path,
          cfbd_get, is_garbage_time, match_team, load):
    """Update the cache for weeks with newly completed games, then write data/cfb/qbs.json."""
    cache = load(cache_path, {"box": {}, "plays": {}})
    cache.setdefault("rush", {})
    cache.setdefault("rushGames", [])  # games whose rushing box score has been downloaded
    if key:
        for w in weeks:
            done = [g["id"] for g in w["games"] if g["state"] == "post" and g["id"] in games_by_id]
            if not done:
                continue
            need_box = any(not any(k.startswith(gid + "|") for k in cache["box"]) for gid in done)
            need_plays = any(not any(k.startswith(gid + "|") for k in cache["plays"]) for gid in done)
            rush_done = set(cache["rushGames"])
            need_rush = any(gid not in rush_done for gid in done)
            if not need_box and not need_plays and not need_rush:
                continue
            stype = "regular" if w["stype"] == 2 else "postseason"
            wk = w["week"] if stype == "regular" else 1
            try:
                if need_box:
                    cache["box"].update(week_qb_box(cfbd_get(f"/games/players?year={year}&week={wk}&seasonType={stype}&category=passing", key),
                                                    match_team, games_by_id, teams))
                if need_rush:
                    cache["rush"].update(week_rush_box(cfbd_get(f"/games/players?year={year}&week={wk}&seasonType={stype}&category=rushing", key),
                                                       match_team, games_by_id, teams))
                    cache["rushGames"] = sorted(set(cache["rushGames"]) | set(done))
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
                     db, round(epa, 3), cp_att, cp_comp, round(cp_exp, 2)] + (cache["rush"].get(k) or [0, 0, 0]))
        players[aid] = {"name": box["name"], "team": box["team"]}
    rows.sort(key=lambda r: (r[1], r[0], r[2]))
    matched = sum(1 for r in rows if r[11])
    out_path.write_text(json.dumps({"cols": QB_COLS, "players": dict(sorted(players.items())), "rows": rows,
                                    "leagueCompletion": round(overall, 4)}, separators=(",", ":")))
    print(f"Saved {len(rows)} quarterback-games for {len(players)} passers "
          f"({matched} with play-by-play; league completion rate {overall:.1%})")


ESPN_ATHLETE = "https://site.api.espn.com/apis/common/v3/sports/football/college-football/athletes"
CAREER_REFRESH_DAYS = 7
CAREER_MAX_PER_RUN = 400
MIN_ATTEMPTS_FOR_PAGE = 30  # quarterbacks with at least this many attempts this season get a page
FIRST_EPA_SEASON = 2014


def build_careers(year, key, qbs_path, cache_path, out_path, get_json, cfbd_get, load):
    """Bio and season-by-season career lines for quarterbacks with real playing time this season.

    Bio and career passing/rushing come from ESPN (all schools, so transfers show up). EPA per pass
    play for past seasons comes from CollegeFootballData (garbage time excluded); this season's EPA per
    dropback comes from our own play-by-play. Each player is fetched once and refreshed weekly.
    """
    from datetime import date
    q = json.loads(qbs_path.read_text())
    c = {k: i for i, k in enumerate(q["cols"])}
    att = {}
    for r in q["rows"]:
        att[r[c["player"]]] = att.get(r[c["player"]], 0) + r[c["att"]]
    wanted = [pid for pid, a in att.items() if a >= MIN_ATTEMPTS_FOR_PAGE]

    cache = load(cache_path, {"players": {}, "epaSeasons": {}})
    today = date.today()
    due = [pid for pid in wanted
           if pid not in cache["players"]
           or (today - date.fromisoformat(cache["players"][pid].get("fetched", "2000-01-01"))).days > CAREER_REFRESH_DAYS]
    for pid in due[:CAREER_MAX_PER_RUN]:
        try:
            a = get_json(f"{ESPN_ATHLETE}/{pid}").get("athlete", {})
            st = get_json(f"{ESPN_ATHLETE}/{pid}/stats")
        except Exception as exc:
            print(f"  QB {pid}: {exc}", file=sys.stderr)
            continue
        seasons = {}
        for cat in st.get("categories", []):
            if cat.get("name") not in ("passing", "rushing"):
                continue
            labels = cat.get("labels", [])
            for row in cat.get("statistics", []):
                season = str((row.get("season") or {}).get("year") or (row.get("season") or {}).get("displayName"))
                team = row.get("teamSlug") or ""
                s = seasons.setdefault((season, team), {"season": season, "teamSlug": team, "teamId": str(row.get("teamId") or "")})
                vals = dict(zip(labels, row.get("stats", [])))
                def num(k):
                    try:
                        return float(str(vals.get(k, "0")).replace(",", ""))
                    except ValueError:  # ESPN shows "-" for some missing values
                        return 0.0
                if cat["name"] == "passing":
                    s.update(cmp=int(num("CMP")), att=int(num("ATT")), yds=int(num("YDS")), td=int(num("TD")),
                             int=int(num("INT")), sacks=int(num("SACK")), rtg=num("RTG"))
                else:
                    s.update(car=int(num("CAR")), ryds=int(num("YDS")), rtd=int(num("TD")))
        cache["players"][pid] = {
            "name": a.get("displayName", ""),
            "height": a.get("displayHeight", ""), "weight": a.get("displayWeight", ""),
            "birthPlace": a.get("displayBirthPlace", ""), "jersey": a.get("displayJersey", ""),
            "position": (a.get("position") or {}).get("abbreviation", ""),
            "seasons": sorted(seasons.values(), key=lambda s: (s["season"], s["teamSlug"])),
            "fetched": today.isoformat(),
        }
        time.sleep(0.1)

    # Past seasons' EPA per pass play, one request per season (only seasons our QBs played)
    if key:
        needed = sorted({s["season"] for p in cache["players"].values() for s in p["seasons"]
                         if s["season"].isdigit() and FIRST_EPA_SEASON <= int(s["season"]) < int(year)})
        for season in needed:
            if season in cache["epaSeasons"]:
                continue
            try:
                rows = cfbd_get(f"/ppa/players/season?year={season}&position=QB&excludeGarbageTime=true", key)
                cache["epaSeasons"][season] = {str(r["id"]): round((r.get("averagePPA") or {}).get("pass") or 0, 3)
                                               for r in rows if (r.get("averagePPA") or {}).get("pass") is not None}
            except Exception as exc:
                print(f"  EPA {season}: {exc}", file=sys.stderr)
            time.sleep(0.3)
    cache_path.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True))

    out = {}
    for pid in wanted:
        p = cache["players"].get(pid)
        if not p:
            continue
        seasons = []
        for s in p["seasons"]:
            row = dict(s)
            row["epa"] = cache["epaSeasons"].get(s["season"], {}).get(pid)
            seasons.append(row)
        out[pid] = {k: p[k] for k in ("name", "height", "weight", "birthPlace", "jersey", "position")}
        out[pid]["seasons"] = seasons
    out_path.write_text(json.dumps(out, separators=(",", ":"), sort_keys=True))
    print(f"Saved careers for {len(out)} quarterbacks ({min(len(due), CAREER_MAX_PER_RUN)} fetched this run)")
