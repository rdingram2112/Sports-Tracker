"""College football running back stats for the Players view and RB pages, used by fetch_cfb.py.

Writes data/cfb/rbs.json: one row per running back per game with
  - box score rushing and receiving (carries, yards, TD; catches, yards, TD) from CollegeFootballData
  - from play-by-play, garbage time excluded: EPA per rush, rushing success, explosive runs (12+ and 20+
    yards), stuffs (runs for no gain or a loss), highlight yards, and EPA per target
  - from all play-by-play: red-zone (inside the 20) and goal-line (inside the 5) carries, short-yardage
    carries (3rd/4th and 2 or less) and conversions, targets, and the team's carries (for carry share)

Highlight yards (Bill Connelly) credit the back for yardage beyond what the line is responsible for:
on runs of 4+ yards, half of yards 5-10 and all yards past 10. Highlight yards per opportunity divides
by the number of 4+ yard runs.

Players are matched to plays by team and jersey number ("#17 T.Brown"), checked against the name.
Positions come from CollegeFootballData rosters. Weeks are cached in data/cfb/rbgames.json.

Also writes data/cfb/rbcareers.json: bio and season-by-season rushing/receiving at every school (ESPN),
with past seasons' EPA per rush from CollegeFootballData.
"""
import json
import re
import sys
import time
from datetime import date

from cfb_qb import name_key, plain, fill_games_played, with_gp

RUSH_TYPES = {"Rush", "Rushing Touchdown"}
TARGET_TYPES = {"Pass Reception", "Pass Incompletion", "Passing Touchdown", "Pass Interception Return",
                "Interception", "Interception Return Touchdown", "Pass Completion"}
RB_POSITIONS = {"RB"}
RB_COLS = ["gameId", "week", "player", "team", "opp", "confGame",
           "car", "ryds", "rtd", "rec", "recYds", "recTd",
           "n", "epa", "succ", "expl12", "expl20", "stuff", "hl", "hlOpp",
           "rz", "gl", "syAtt", "syConv", "tgt", "tgtN", "tgtEpa", "teamRush"]
PBP_KEYS = RB_COLS[12:27]  # n ... tgtEpa (teamRush is per team)

RUSHER = re.compile(r"(?:#(\d+)\s+)?([A-Z][\w'.\-]*(?:\s+[A-Z][\w'.\-]*)*?)\s+(?:rush|run)\b")
RUN_TD = re.compile(r"^([A-Z][\w'.\- ]+?)\s+\d+\s+Yd\s+Run", re.I)
TARGET = re.compile(r"\bto\s+(?:#(\d+)\s+)?([A-Z][\w'.\-]*(?:\s+[A-Z][\w'.\-]*)*?)(?=\s+(?:caught|for|at|in|out|thrown)\b|,|\s*$)")
PASS_TD = re.compile(r"^([A-Z][\w'.\- ]+?)\s+\d+\s+Yd\s+pass from", re.I)
YARDLINE = re.compile(r"^[A-Z]{2,5}\d+$")


def highlight(gain):
    return 0.5 * min(gain - 4, 6) + max(gain - 10, 0) if gain >= 4 else 0.0


def success(play, gain):
    down, dist = play.get("down"), play.get("distance") or 0
    if "touchdown" in (play.get("playType") or "").lower():
        return True
    need = dist * (0.5 if down == 1 else 0.7 if down == 2 else 1.0)
    return gain >= need


class Roster:
    """Players by ESPN team id, looked up by jersey number and/or name."""

    def __init__(self, players):
        self.by_jersey, self.by_team = {}, {}
        for pid, p in players.items():
            self.by_team.setdefault(p["team"], []).append((pid, p))
            if p.get("jersey") is not None:
                self.by_jersey.setdefault((p["team"], str(p["jersey"])), []).append((pid, p))

    def find(self, team, jersey, name):
        nk = name_key(name) if name else None
        if jersey:
            cands = self.by_jersey.get((team, str(jersey)), [])
            if len(cands) == 1 and (not nk or name_key(cands[0][1]["name"])[1] == nk[1]):
                return cands[0][0]
            for pid, p in cands:
                if nk and name_key(p["name"]) == nk:
                    return pid
        if nk:
            same = [pid for pid, p in self.by_team.get(team, []) if name_key(p["name"]) == nk]
            if len(same) == 1:
                return same[0]
        return None


def fetch_roster(year, key, cfbd_get, teams, norm_name):
    """{athleteId: {team (ESPN id), pos, jersey, name}} from CollegeFootballData's season roster."""
    by_name = {norm_name(t["name"]): tid for tid, t in teams.items()}
    out = {}
    for r in cfbd_get(f"/roster?year={year}", key):
        tid = by_name.get(norm_name(r.get("team")))
        if not tid or r.get("id") is None:
            continue
        out[str(r["id"])] = {"team": tid, "pos": r.get("position"), "jersey": r.get("jersey"),
                             "name": f"{r.get('firstName') or ''} {r.get('lastName') or ''}".strip()}
    return out


def week_box(rows, category, match_team, games_by_id, teams):
    """{"gameId|athleteId": {"team", "name", stat: value}} for one box-score category."""
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
            for cat in t.get("categories", []):
                if cat.get("name") != category:
                    continue
                for ty in cat.get("types", []):
                    for a in ty.get("athletes", []):
                        if str(a["id"]).startswith("-"):
                            continue
                        rec = out.setdefault(f"{gid}|{a['id']}", {"team": tid, "name": a.get("name", "")})
                        try:
                            rec[ty["name"]] = int(float(str(a.get("stat") or 0).replace(",", "")))
                        except ValueError:
                            rec[ty["name"]] = 0
    return out


def week_pbp(plays, roster, is_garbage_time, match_team, games_by_id, teams):
    """({"gameId|athleteId": [PBP_KEYS...]}, {"gameId|teamId": team carries}) for one week."""
    stats, team_rush = {}, {}

    def rec(gid, pid):
        return stats.setdefault(f"{gid}|{pid}", dict.fromkeys(PBP_KEYS, 0))

    for p in plays:
        ptype = p.get("playType")
        if ptype not in RUSH_TYPES and ptype not in TARGET_TYPES:
            continue
        if p.get("down") not in (1, 2, 3, 4):
            continue
        gid = str(p.get("gameId"))
        tid = match_team(gid, p.get("offense"), p.get("defense"), games_by_id, teams)
        if not tid:
            continue
        text = p.get("playText") or ""
        gain = p.get("yardsGained") or 0
        garbage = is_garbage_time(p)
        ppa = p.get("ppa")
        if ptype in RUSH_TYPES:
            team_rush[f"{gid}|{tid}"] = team_rush.get(f"{gid}|{tid}", 0) + 1
            m = RUSHER.search(text)
            jersey, name = (m.group(1), m.group(2)) if m else (None, None)
            if not m:
                t = RUN_TD.search(text)
                name = t.group(1).strip() if t else None
            pid = roster.find(tid, jersey, name)
            if not pid:
                continue
            r = rec(gid, pid)
            ytg = p.get("yardsToGoal") or 100
            r["rz"] += ytg <= 20
            r["gl"] += ytg <= 5
            if p.get("down") in (3, 4) and (p.get("distance") or 99) <= 2:
                r["syAtt"] += 1
                r["syConv"] += gain >= (p.get("distance") or 0) or "touchdown" in ptype.lower()
            if garbage:
                continue
            r["n"] += 1
            r["epa"] += ppa or 0
            r["succ"] += success(p, gain)
            r["expl12"] += gain >= 12
            r["expl20"] += gain >= 20
            r["stuff"] += gain <= 0
            if gain >= 4:
                r["hlOpp"] += 1
                r["hl"] += highlight(gain)
        else:
            m = TARGET.search(text)
            jersey, name = (m.group(1), m.group(2)) if m else (None, None)
            if name and YARDLINE.match(name):
                jersey, name = None, None
            if not name:
                t = PASS_TD.search(text)
                name = t.group(1).strip() if t else None
            if not name:
                continue
            pid = roster.find(tid, jersey, name)
            if not pid:
                continue
            r = rec(gid, pid)
            r["tgt"] += 1
            if not garbage and ppa is not None:
                r["tgtN"] += 1
                r["tgtEpa"] += ppa
    return ({k: [round(v[x], 3) if isinstance(v[x], float) else v[x] for x in PBP_KEYS] for k, v in stats.items()},
            team_rush)


def build(year, weeks, games_by_id, teams, results_meta, key, cache_path, out_path,
          cfbd_get, is_garbage_time, match_team, load, norm_name):
    cache = load(cache_path, {})
    for k, v in (("roster", {}), ("rosterFetched", ""), ("box", {}), ("boxGames", []), ("pbp", {}),
                 ("teamRush", {}), ("pbpGames", [])):
        cache.setdefault(k, v)
    if key:
        stale = not cache["rosterFetched"] or (date.today() - date.fromisoformat(cache["rosterFetched"])).days > 7
        if stale:
            try:
                cache["roster"] = fetch_roster(year, key, cfbd_get, teams, norm_name)
                cache["rosterFetched"] = date.today().isoformat()
                print(f"Roster: {len(cache['roster'])} players")
            except Exception as exc:
                print(f"  roster: {exc}", file=sys.stderr)
        roster = Roster(cache["roster"])
        box_done, pbp_done = set(cache["boxGames"]), set(cache["pbpGames"])
        for w in weeks:
            done = [g["id"] for g in w["games"] if g["state"] == "post" and g["id"] in games_by_id]
            need_box = any(g not in box_done for g in done)
            need_pbp = any(g not in pbp_done for g in done)
            if not need_box and not need_pbp:
                continue
            stype = "regular" if w["stype"] == 2 else "postseason"
            wk = w["week"] if stype == "regular" else 1
            try:
                if need_box:
                    rush = week_box(cfbd_get(f"/games/players?year={year}&week={wk}&seasonType={stype}&category=rushing", key),
                                    "rushing", match_team, games_by_id, teams)
                    recv = week_box(cfbd_get(f"/games/players?year={year}&week={wk}&seasonType={stype}&category=receiving", key),
                                    "receiving", match_team, games_by_id, teams)
                    for k in set(rush) | set(recv):
                        a, b = rush.get(k, {}), recv.get(k, {})
                        base = a or b
                        cache["box"][k] = {"team": base["team"], "name": base["name"],
                                           "car": a.get("CAR", 0), "ryds": a.get("YDS", 0), "rtd": a.get("TD", 0),
                                           "rec": b.get("REC", 0), "recYds": b.get("YDS", 0), "recTd": b.get("TD", 0)}
                    box_done |= set(done)
                if need_pbp:
                    stats, team_rush = week_pbp(cfbd_get(f"/plays?year={year}&week={wk}&seasonType={stype}", key),
                                                roster, is_garbage_time, match_team, games_by_id, teams)
                    cache["pbp"].update(stats)
                    cache["teamRush"].update(team_rush)
                    pbp_done |= set(done)
                print(f"RB stats {stype} week {wk} downloaded")
            except Exception as exc:
                print(f"  RB stats {stype} week {wk}: {exc}", file=sys.stderr)
            time.sleep(0.5)
        cache["boxGames"], cache["pbpGames"] = sorted(box_done), sorted(pbp_done)
        cache_path.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True))
    else:
        print("No CFBD_API_KEY set; running back stats use the saved data.")

    rows, players = [], {}
    for k, b in cache["box"].items():
        gid, pid = k.split("|")
        if (cache["roster"].get(pid) or {}).get("pos") not in RB_POSITIONS or not (b["car"] or b["rec"]):
            continue
        meta = results_meta.get((gid, b["team"]))
        if not meta:
            continue
        week, opp, conf = meta
        pbp = cache["pbp"].get(k) or [0] * len(PBP_KEYS)
        rows.append([gid, week, pid, b["team"], opp, conf, b["car"], b["ryds"], b["rtd"], b["rec"], b["recYds"], b["recTd"]]
                    + pbp + [cache["teamRush"].get(f"{gid}|{b['team']}", 0)])
        players[pid] = {"name": b["name"], "team": b["team"]}
    rows.sort(key=lambda r: (r[1], r[0], r[2]))
    matched = sum(r[6] for r in rows if r[12])
    total = sum(r[6] for r in rows)
    out_path.write_text(json.dumps({"cols": RB_COLS, "players": dict(sorted(players.items())), "rows": rows},
                                   separators=(",", ":")))
    print(f"Saved {len(rows)} running back-games for {len(players)} backs "
          f"({matched / total:.1%} of carries matched to play-by-play)" if total else "No running back games")


ESPN_ATHLETE = "https://site.api.espn.com/apis/common/v3/sports/football/college-football/athletes"
MIN_TOUCHES_FOR_PAGE = 25
FIRST_EPA_SEASON = 2014


CAREER_KINDS = {
    # touches counted toward a page, CFBD positions and PPA field for past seasons' EPA, label
    "rb": {"touch": ("car", "rec"), "min": MIN_TOUCHES_FOR_PAGE, "positions": ("RB",), "ppa": "rush", "label": "running backs"},
    "wr": {"touch": ("tgt",), "min": 20, "positions": ("WR", "TE"), "ppa": "pass", "label": "receivers"},
    "dl": {"touch": ("tkl", "press"), "min": 10, "positions": (), "ppa": None, "label": "defensive linemen and edge rushers",
           "cats": ("defensive",)},
}


def build_careers(year, key, rbs_path, cache_path, out_path, get_json, cfbd_get, load, kind="rb"):
    """Bio and season-by-season rushing/receiving for players with real playing time; refreshed weekly.
    Past seasons' EPA is CollegeFootballData's per rush (backs) or per target (receivers)."""
    cfg = CAREER_KINDS[kind]
    q = json.loads(rbs_path.read_text())
    c = {k: i for i, k in enumerate(q["cols"])}
    touches = {}
    for r in q["rows"]:
        touches[r[c["player"]]] = touches.get(r[c["player"]], 0) + sum(r[c[k]] for k in cfg["touch"])
    wanted = [pid for pid, t in touches.items() if t >= cfg["min"]]
    cache = load(cache_path, {"players": {}, "epaSeasons": {}})
    today = date.today()
    due = [pid for pid in wanted if pid not in cache["players"]
           or (today - date.fromisoformat(cache["players"][pid].get("fetched", "2000-01-01"))).days > 7]
    for pid in due[:400]:
        try:
            a = get_json(f"{ESPN_ATHLETE}/{pid}").get("athlete", {})
            st = get_json(f"{ESPN_ATHLETE}/{pid}/stats")
        except Exception as exc:
            print(f"  RB {pid}: {exc}", file=sys.stderr)
            continue
        seasons = {}
        for cat in st.get("categories", []):
            if cat.get("name") not in cfg.get("cats", ("rushing", "receiving")):
                continue
            labels = cat.get("labels", [])
            for row in cat.get("statistics", []):
                season = str((row.get("season") or {}).get("year") or (row.get("season") or {}).get("displayName"))
                team = row.get("teamSlug") or ""
                s = seasons.setdefault((season, team), {"season": season, "teamSlug": team, "teamId": str(row.get("teamId") or "")})
                vals = dict(zip(labels, row.get("stats", [])))

                def num(k):
                    try:
                        return int(float(str(vals.get(k, "0")).replace(",", "")))
                    except ValueError:
                        return 0
                if cat["name"] == "defensive":
                    try:
                        sk = float(str(vals.get("SACK", "0")).replace(",", ""))
                    except ValueError:
                        sk = 0.0
                    s.update(tkl=num("TOT"), solo=num("SOLO"), sacks=sk, ff=num("FF"), pd=num("PD"), int=num("INT"))
                elif cat["name"] == "rushing":
                    s.update(car=num("CAR"), ryds=num("YDS"), rtd=num("TD"), rlng=num("LNG"))
                else:
                    s.update(rec=num("REC"), recYds=num("YDS"), recTd=num("TD"))
        cache["players"][pid] = {
            "name": a.get("displayName", ""), "height": a.get("displayHeight", ""), "weight": a.get("displayWeight", ""),
            "birthPlace": a.get("displayBirthPlace", ""), "jersey": a.get("displayJersey", ""),
            "position": (a.get("position") or {}).get("abbreviation", ""),
            "seasons": sorted(seasons.values(), key=lambda s: (s["season"], s["teamSlug"])),
            "fetched": today.isoformat(),
        }
        time.sleep(0.1)
    if key and cfg["ppa"]:
        needed = sorted({s["season"] for p in cache["players"].values() for s in p["seasons"]
                         if s["season"].isdigit() and FIRST_EPA_SEASON <= int(s["season"]) < int(year)})
        for season in needed:
            if season in cache["epaSeasons"]:
                continue
            try:
                got = {}
                for pos in cfg["positions"]:
                    rows = cfbd_get(f"/ppa/players/season?year={season}&position={pos}&excludeGarbageTime=true", key)
                    got.update({str(r["id"]): round((r.get("averagePPA") or {}).get(cfg["ppa"]) or 0, 3)
                                                        for r in rows if (r.get("averagePPA") or {}).get(cfg["ppa"]) is not None})
                cache["epaSeasons"][season] = got
            except Exception as exc:
                print(f"  RB EPA {season}: {exc}", file=sys.stderr)
            time.sleep(0.3)
    fill_games_played(cache, wanted, year, get_json, ESPN_ATHLETE)
    cache_path.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True))
    out = {}
    for pid in wanted:
        p = cache["players"].get(pid)
        if not p:
            continue
        out[pid] = {k: p[k] for k in ("name", "height", "weight", "birthPlace", "jersey", "position")}
        out[pid]["seasons"] = [dict(s, epa=cache["epaSeasons"].get(s["season"], {}).get(pid), gp=with_gp(p, s)) for s in p["seasons"]]
    out_path.write_text(json.dumps(out, separators=(",", ":"), sort_keys=True))
    print(f"Saved careers for {len(out)} {cfg['label']} ({min(len(due), 400)} fetched this run)")
