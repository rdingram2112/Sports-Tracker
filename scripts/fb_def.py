"""Defensive player stats (defensive linemen and edge rushers for now), college and NFL, one row per player per game.

Columns (DEF_COLS):
  tkl, solo        total and solo tackles
  sacks            sacks (half sacks count 0.5)
  hits             NFL: QB hits that weren't sacks; college: QB hurries (from the box score)
  press            pressures: NFL = plays where he sacked or hit the quarterback; college = sacks + hurries
  tfl, ff, pd, int tackles for loss, forced fumbles, passes defensed, interceptions
  runTkl, runStop, runYds   tackles on designed runs, how many held the offense to an unsuccessful play
                            (the same yardage rule as everywhere else), and the yards the runner had gained
  passTkl, passStop         the same on pass plays (catches and sacks)

Sources: NFL from nflverse play-by-play (every tackler, sack, hit, TFL, forced fumble and breakup is listed per
play). College box-score totals from CollegeFootballData (tackles, sacks, TFL, breakups, hurries, interceptions);
stops and forced fumbles from the play text, which names the tacklers ("(#95 M.Hines; #9 D.Sears)").

Positions: defensive linemen (DE, DT, NT, DL) plus linebackers who rush the passer a lot (at least 0.8
pressures per game played this season, and pressures at least 30% of their tackles, which leaves out off-ball
linebackers who blitz), labeled EDGE, since many 3-4 outside linebackers are listed as LB.
"""
import json
import re
import sys
import time

DEF_COLS = ["gameId", "week", "player", "team", "opp", "confGame", "tkl", "solo", "sacks", "hits", "press",
            "tfl", "ff", "pd", "int", "runTkl", "runStop", "runYds", "passTkl", "passStop"]
STAT_KEYS = DEF_COLS[6:]
DL_POSITIONS = {"DE", "DT", "NT", "DL"}
LB_POSITIONS = {"LB", "OLB", "ILB", "MLB", "EDGE"}
EDGE_PRESS_PER_GAME = 0.8
EDGE_PRESS_PER_TACKLE = 0.3  # off-ball linebackers blitz too, but make far more tackles than pressures


def _blank():
    return dict.fromkeys(STAT_KEYS, 0)


def _rows(stats, meta, players, position_of):
    """stats {(gid, pid, team): {...}} -> {"cols", "players", "rows"} for DL and EDGE players."""
    games, press, tkl = {}, {}, {}
    for (gid, pid, team), v in stats.items():
        games[pid] = games.get(pid, 0) + 1
        press[pid] = press.get(pid, 0) + v["press"]
        tkl[pid] = tkl.get(pid, 0) + v["tkl"]
    rows, used = [], {}
    for (gid, pid, team), v in stats.items():
        pos = position_of(pid)
        if pos in DL_POSITIONS:
            label = "DL"
        elif (pos in LB_POSITIONS and games[pid] and press[pid] / games[pid] >= EDGE_PRESS_PER_GAME
              and press[pid] >= EDGE_PRESS_PER_TACKLE * tkl[pid]):
            label = "EDGE"
        else:
            continue
        if (gid, team) not in meta:
            continue
        week, opp, conf = meta[(gid, team)]
        vals = [round(v[k], 1) if isinstance(v[k], float) and not float(v[k]).is_integer() else int(v[k]) for k in STAT_KEYS]
        rows.append([gid, week, pid, team, opp, conf] + vals)
        used[pid] = {"name": players.get(pid, pid), "team": team, "pos": label}
    rows.sort(key=lambda r: (r[1], r[0], r[2]))
    return {"cols": DEF_COLS, "players": dict(sorted(used.items())), "rows": rows}


# ---------- NFL ----------

def nfl_rows(plays_by_game, ids, meta, teams_by_abbr, yard_success, flag, num):
    def pid_list(p, cols):
        return [p[c] for c in cols if p.get(c) not in ("", "NA", None)]

    stats, names, pos = {}, {}, {}
    for gid, plays in plays_by_game.items():
        for p in plays:
            if p.get("play_type") not in ("pass", "run"):
                continue
            team = teams_by_abbr.get(p.get("defteam"))
            if not team:
                continue

            def rec(g):
                who = ids.get(g)
                names[who["id"]] = who["name"]
                pos[who["id"]] = who["pos"]
                return stats.setdefault((gid, who["id"], team), _blank())
            solos = pid_list(p, ["solo_tackle_1_player_id", "solo_tackle_2_player_id"])
            assists = pid_list(p, ["assist_tackle_1_player_id", "assist_tackle_2_player_id", "assist_tackle_3_player_id",
                                   "assist_tackle_4_player_id", "tackle_with_assist_1_player_id", "tackle_with_assist_2_player_id"])
            sackers = pid_list(p, ["sack_player_id"])
            halves = pid_list(p, ["half_sack_1_player_id", "half_sack_2_player_id"])
            hitters = pid_list(p, ["qb_hit_1_player_id", "qb_hit_2_player_id"])
            tfls = pid_list(p, ["tackle_for_loss_1_player_id", "tackle_for_loss_2_player_id"])
            for g in solos:
                r = rec(g); r["tkl"] += 1; r["solo"] += 1
            for g in assists:
                rec(g)["tkl"] += 1
            for g in sackers:
                rec(g)["sacks"] += 1
            for g in halves:
                rec(g)["sacks"] += 0.5
            for g in hitters:
                if g not in sackers and g not in halves:  # nflverse also marks the sacker as hitting the QB
                    rec(g)["hits"] += 1
            for g in set(sackers) | set(halves) | set(hitters):
                rec(g)["press"] += 1
            for g in tfls:
                rec(g)["tfl"] += 1
            for g in pid_list(p, ["forced_fumble_player_1_player_id", "forced_fumble_player_2_player_id"]):
                rec(g)["ff"] += 1
            for g in pid_list(p, ["pass_defense_1_player_id", "pass_defense_2_player_id"]):
                rec(g)["pd"] += 1
            for g in pid_list(p, ["interception_player_id"]):
                rec(g)["int"] += 1
            involved = set(solos) | set(assists) | set(tfls) | set(sackers) | set(halves)
            if not involved:
                continue
            stop = not yard_success(p)
            if p.get("play_type") == "run" and flag(p.get("rush")):
                for g in involved:
                    r = rec(g); r["runTkl"] += 1; r["runStop"] += stop; r["runYds"] += num(p.get("yards_gained"))
            elif flag(p.get("qb_dropback")):
                for g in involved:
                    r = rec(g); r["passTkl"] += 1; r["passStop"] += stop
    return _rows(stats, meta, names, lambda pid: pos.get(pid, ""))


# ---------- college ----------

TACKLE_GROUP = re.compile(r"\(\s*(#\d+[^()]*?)\)")
TACKLER = re.compile(r"#(\d+)\s+([A-Z][\w'.\-]*(?:\s+[A-Z][\w'.\-]*)*)")
FORCED = re.compile(r"forced by #(\d+)\s+([A-Z][\w'.\-]*(?:\s+[A-Z][\w'.\-]*)*)")
RUSH_TYPES = {"Rush", "Rushing Touchdown"}
PASS_TYPES = {"Pass Reception", "Passing Touchdown", "Pass Completion", "Sack"}


def tacklers(text):
    """[(jersey, name)] from the tackler list at the end of a play, e.g. "(#95 M.Hines; #9 D.Sears)"."""
    out = []
    for grp in TACKLE_GROUP.findall(text or ""):
        for part in re.split(r"[;,]", grp):
            m = TACKLER.search(part)
            if m:
                out.append((m.group(1), m.group(2).strip()))
    return out


def cfb_week_box(rows, match_team, games_by_id, teams):
    """{"gameId|athleteId": {"team", "name", TOT, SOLO, SACKS, TFL, PD, QB HUR, INT}} from defensive + interception box scores."""
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
                if cat.get("name") not in ("defensive", "interceptions"):
                    continue
                for ty in cat.get("types", []):
                    if cat["name"] == "interceptions" and ty.get("name") != "INT":
                        continue
                    for a in ty.get("athletes", []):
                        if str(a["id"]).startswith("-"):
                            continue
                        r = out.setdefault(f"{gid}|{a['id']}", {"team": tid, "name": a.get("name", "")})
                        try:
                            r[ty["name"]] = float(str(a.get("stat") or 0).replace(",", ""))
                        except ValueError:
                            r[ty["name"]] = 0.0
    return out


def cfb_week_pbp(plays, roster, match_team, games_by_id, teams, success):
    """{"gameId|athleteId": [runTkl, runStop, runYds, passTkl, passStop, ff]} from play text."""
    out = {}
    for p in plays:
        ptype = p.get("playType")
        text = p.get("playText") or ""
        gid = str(p.get("gameId"))
        tid = match_team(gid, p.get("defense"), p.get("offense"), games_by_id, teams)
        if not tid:
            continue
        for m in FORCED.finditer(text):
            pid = roster.find(tid, m.group(1), m.group(2))
            if pid:
                out.setdefault(f"{gid}|{pid}", [0, 0, 0, 0, 0, 0])[5] += 1
        if ptype not in RUSH_TYPES and ptype not in PASS_TYPES or p.get("down") not in (1, 2, 3, 4):
            continue
        gain = p.get("yardsGained") or 0
        stop = not success(p, gain)
        seen = set()
        for jersey, name in tacklers(text):
            pid = roster.find(tid, jersey, name)
            if not pid or pid in seen:
                continue
            seen.add(pid)
            r = out.setdefault(f"{gid}|{pid}", [0, 0, 0, 0, 0, 0])
            if ptype in RUSH_TYPES:
                r[0] += 1; r[1] += stop; r[2] += gain
            else:
                r[3] += 1; r[4] += stop
    return out


def cfb_build(year, weeks, games_by_id, teams, results_meta, key, rb_cache_path, cache_path, out_path,
              cfbd_get, match_team, load):
    from cfb_rb import Roster, success
    rb = load(rb_cache_path, {})
    roster_data = rb.get("roster", {})
    cache = load(cache_path, {})
    for k, v in (("box", {}), ("boxGames", []), ("pbp", {}), ("pbpGames", [])):
        cache.setdefault(k, v)
    if key and roster_data:
        roster = Roster(roster_data)
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
                    for cat in ("defensive", "interceptions"):
                        rows = cfbd_get(f"/games/players?year={year}&week={wk}&seasonType={stype}&category={cat}", key)
                        for k2, v in cfb_week_box(rows, match_team, games_by_id, teams).items():
                            cache["box"].setdefault(k2, {}).update(v)
                    box_done |= set(done)
                if need_pbp:
                    plays = cfbd_get(f"/plays?year={year}&week={wk}&seasonType={stype}", key)
                    cache["pbp"].update(cfb_week_pbp(plays, roster, match_team, games_by_id, teams, success))
                    pbp_done |= set(done)
                print(f"Defense {stype} week {wk} downloaded")
            except Exception as exc:
                print(f"  defense {stype} week {wk}: {exc}", file=sys.stderr)
            time.sleep(0.5)
        cache["boxGames"], cache["pbpGames"] = sorted(box_done), sorted(pbp_done)
        cache_path.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True))
    elif not key:
        print("No CFBD_API_KEY set; defensive stats use the saved data.")

    stats, names = {}, {}
    for k, b in cache["box"].items():
        gid, pid = k.split("|")
        pb = cache["pbp"].get(k) or [0, 0, 0, 0, 0, 0]
        sacks, hurries = b.get("SACKS", 0), b.get("QB HUR", 0)
        stats[(gid, pid, b["team"])] = {
            "tkl": b.get("TOT", 0), "solo": b.get("SOLO", 0), "sacks": sacks, "hits": hurries, "press": sacks + hurries,
            "tfl": b.get("TFL", 0), "ff": pb[5], "pd": b.get("PD", 0), "int": b.get("INT", 0),
            "runTkl": pb[0], "runStop": pb[1], "runYds": pb[2], "passTkl": pb[3], "passStop": pb[4]}
        names[pid] = b.get("name") or pid
    pos = {pid: (r.get("pos") or "").upper() for pid, r in roster_data.items()}
    out = _rows(stats, results_meta, names, lambda pid: pos.get(pid, ""))
    out_path.write_text(json.dumps(out, separators=(",", ":")))
    print(f"Saved {len(out['rows'])} defensive line games for {len(out['players'])} DL and edge rushers")
    return out
