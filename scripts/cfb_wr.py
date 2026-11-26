"""College football wide receiver and tight end stats for the Players view and receiver pages, used by fetch_cfb.py.

Writes data/cfb/wrs.json: one row per WR/TE per game with
  - box score receiving and rushing (catches, yards, TD; carries, yards, TD), taken from the running back
    cache (data/cfb/rbgames.json), which downloads every player's rushing and receiving box scores
  - from play-by-play: targets, catches and yards on those targets, red-zone targets (inside the 20),
    air yards (how far past the line of scrimmage the ball was caught or thrown to) and yards after
    the catch, plus each team's targets and air yards for target share and air-yard share
  - from play-by-play with garbage time excluded: EPA per target, success rate per target, and
    explosive catches (20+ yards)

College play-by-play has no air-yards field, but the stat crew's text says where each pass was caught
("caught at WSU45") or thrown ("thrown to WVU00"). Spots are written with a team abbreviation, so each
game's abbreviations are first matched to teams using the play-ending spots ("to the WSU44"), whose
yard line is known from yardsToGoal and yardsGained. About 85% of targets get air yards this way.

Weeks are cached in data/cfb/wrgames.json. Careers (data/cfb/wrcareers.json) come from cfb_rb.build_careers.
"""
import json
import re
import sys
import time
from collections import Counter, defaultdict

from cfb_rb import TARGET, PASS_TD, YARDLINE, Roster, success

TARGET_TYPES = {"Pass Reception", "Pass Incompletion", "Passing Touchdown", "Pass Completion"}
INT_TYPES = {"Pass Interception Return", "Interception", "Interception Return Touchdown"}
COMPLETE_TYPES = {"Pass Reception", "Passing Touchdown", "Pass Completion"}
WR_POSITIONS = {"WR", "TE"}
WR_COLS = ["gameId", "week", "player", "team", "opp", "confGame",
           "rec", "recYds", "recTd", "car", "ryds", "rtd",
           "tgt", "cat", "tyds", "rz", "airN", "air", "yacN", "yac", "n", "epa", "succ", "expl",
           "teamTgt", "teamAir"]
PBP_KEYS = WR_COLS[12:24]  # tgt ... expl
VERSION = 2

SPOT = r"([A-Z][A-Za-z&.]{1,9}?)\s?(\d{1,2})\b"  # "WSU45", "OhioSt24"
END_SPOT = re.compile(r"\bto the " + SPOT)
CATCH_SPOT = re.compile(r"\bcaught at " + SPOT)
THROW_SPOT = re.compile(r"\bthrown to " + SPOT)


def spot_owners(plays):
    """{(gameId, abbreviation): team name} from play-ending spots whose yard line is known."""
    votes = defaultdict(Counter)
    for p in plays:
        m = END_SPOT.search(p.get("playText") or "")
        ytg, gain = p.get("yardsToGoal"), p.get("yardsGained")
        if not m or ytg is None or gain is None:
            continue
        n, end = int(m.group(2)), ytg - gain
        if n in (0, 50) or not 0 < end < 100:
            continue
        if n == end:
            votes[(p.get("gameId"), m.group(1))][p.get("defense")] += 1
        elif 100 - n == end:
            votes[(p.get("gameId"), m.group(1))][p.get("offense")] += 1
    return {k: c.most_common(1)[0][0] for k, c in votes.items()}


def air_yards(play, owners):
    """Yards past the line of scrimmage the ball was caught or thrown to, or None if the text doesn't say."""
    text = play.get("playText") or ""
    m = CATCH_SPOT.search(text) or THROW_SPOT.search(text)
    ytg = play.get("yardsToGoal")
    if not m or ytg is None:
        return None
    side = owners.get((play.get("gameId"), m.group(1)))
    n = int(m.group(2))
    if side == play.get("defense"):
        spot = n
    elif side == play.get("offense"):
        spot = 100 - n
    else:
        return None
    air = ytg - spot
    return air if -15 <= air <= 75 else None


def week_pbp(plays, roster, is_garbage_time, match_team, games_by_id, teams):
    """({"gameId|athleteId": [PBP_KEYS...]}, {"gameId|teamId": [team targets, team air yards]}) for one week."""
    owners = spot_owners(plays)
    stats, team = {}, {}
    for p in plays:
        ptype = p.get("playType")
        if ptype not in TARGET_TYPES and ptype not in INT_TYPES:
            continue
        if p.get("down") not in (1, 2, 3, 4):
            continue
        gid = str(p.get("gameId"))
        tid = match_team(gid, p.get("offense"), p.get("defense"), games_by_id, teams)
        if not tid:
            continue
        text = p.get("playText") or ""
        gain = p.get("yardsGained") or 0
        air = air_yards(p, owners) if ptype in TARGET_TYPES else None
        t = team.setdefault(f"{gid}|{tid}", [0, 0])
        t[0] += 1
        if air is not None:
            t[1] += air
        m = TARGET.search(text)
        jersey, name = (m.group(1), m.group(2)) if m else (None, None)
        if name and YARDLINE.match(name):
            jersey, name = None, None
        if not name:
            td = PASS_TD.search(text)
            name = td.group(1).strip() if td else None
        if not name:
            continue
        pid = roster.find(tid, jersey, name)
        if not pid:
            continue
        r = stats.setdefault(f"{gid}|{pid}", dict.fromkeys(PBP_KEYS, 0))
        caught = ptype in COMPLETE_TYPES
        r["tgt"] += 1
        r["cat"] += caught
        r["tyds"] += gain if caught else 0
        r["rz"] += (p.get("yardsToGoal") or 100) <= 20
        if air is not None:
            r["airN"] += 1
            r["air"] += air
            if caught:
                r["yacN"] += 1
                r["yac"] += gain - air
        if is_garbage_time(p) or p.get("ppa") is None:
            continue
        r["n"] += 1
        r["epa"] += p["ppa"]
        r["succ"] += caught and success(p, gain)
        r["expl"] += caught and gain >= 20
    return ({k: [round(v[x], 3) if isinstance(v[x], float) else v[x] for x in PBP_KEYS] for k, v in stats.items()},
            team)


def build(year, weeks, games_by_id, teams, results_meta, key, rb_cache_path, cache_path, out_path,
          cfbd_get, is_garbage_time, match_team, load):
    rb = load(rb_cache_path, {})
    box, roster_data = rb.get("box", {}), rb.get("roster", {})
    cache = load(cache_path, {})
    if cache.get("version") != VERSION:
        cache = {"version": VERSION}
    for k, v in (("pbp", {}), ("team", {}), ("pbpGames", [])):
        cache.setdefault(k, v)
    if key and roster_data:
        roster = Roster(roster_data)
        done_games = set(cache["pbpGames"])
        for w in weeks:
            done = [g["id"] for g in w["games"] if g["state"] == "post" and g["id"] in games_by_id]
            if all(g in done_games for g in done):
                continue
            stype = "regular" if w["stype"] == 2 else "postseason"
            wk = w["week"] if stype == "regular" else 1
            try:
                stats, team = week_pbp(cfbd_get(f"/plays?year={year}&week={wk}&seasonType={stype}", key),
                                       roster, is_garbage_time, match_team, games_by_id, teams)
                cache["pbp"].update(stats)
                cache["team"].update(team)
                done_games |= set(done)
                print(f"Receiver stats {stype} week {wk} downloaded")
            except Exception as exc:
                print(f"  receiver stats {stype} week {wk}: {exc}", file=sys.stderr)
            time.sleep(0.5)
        cache["pbpGames"] = sorted(done_games)
        cache_path.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True))
    elif not key:
        print("No CFBD_API_KEY set; receiver stats use the saved data.")

    rows, players = [], {}
    for k, b in box.items():
        gid, pid = k.split("|")
        pos = (roster_data.get(pid) or {}).get("pos")
        pbp = cache["pbp"].get(k)
        if pos not in WR_POSITIONS or not (b["rec"] or b["car"] or pbp):
            continue
        meta = results_meta.get((gid, b["team"]))
        if not meta:
            continue
        week, opp, conf = meta
        team = cache["team"].get(f"{gid}|{b['team']}", [0, 0])
        rows.append([gid, week, pid, b["team"], opp, conf, b["rec"], b["recYds"], b["recTd"], b["car"], b["ryds"], b["rtd"]]
                    + (pbp or [0] * len(PBP_KEYS)) + team)
        players[pid] = {"name": b["name"], "team": b["team"], "pos": pos}
    rows.sort(key=lambda r: (r[1], r[0], r[2]))
    caught = sum(r[6] for r in rows)
    matched = sum(min(r[6], r[13]) for r in rows)
    with_air = sum(r[16] for r in rows)
    targets = sum(r[12] for r in rows)
    out_path.write_text(json.dumps({"cols": WR_COLS, "players": dict(sorted(players.items())), "rows": rows},
                                   separators=(",", ":")))
    if rows and caught and targets:
        print(f"Saved {len(rows)} receiver-games for {len(players)} WRs and TEs "
              f"({matched / caught:.1%} of catches matched to play-by-play, {with_air / targets:.1%} of targets with air yards)")
    else:
        print("No receiver games")
