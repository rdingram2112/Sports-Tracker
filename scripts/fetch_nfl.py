"""NFL data for the site's NFL views (same file formats as college football, in data/nfl/).

Sources:
  - ESPN's public API: schedule, scores, records, team logos, betting lines before kickoff, total yards
    per game (box scores, downloaded once per game), and player bios and career stats.
  - nflverse (github.com/nflverse): play-by-play with EPA, win probability, air yards and expected
    completion probability; the schedule's ESPN game ids, division-game flags and closing lines; and the
    player list that links nflverse ids to ESPN ids (for photos and bios).

Writes:
  data/nfl/season.json   weeks and games, plus one row per team per completed game with points, yards,
                         EPA (raw and opponent-adjusted) and play-by-play rates, like data/cfb/season.json
  data/nfl/qbs.json, rbs.json, wrs.json      per player per game, like the college files
  data/nfl/qbcareers.json, rbcareers.json, wrcareers.json   bios and season-by-season careers
  data/nfl/pastepa.json  EPA per dropback / rush / target for past seasons (each season computed once)

Garbage time: plays where the offense's win probability is under 5% or over 95% are left out of EPA,
success and the other rate stats (box-score totals count every play). Success uses the same yardage
rule as college: 50% of the yards to go on 1st down, 70% on 2nd, 100% on 3rd/4th.
"""
import csv
import gzip
import io
import json
import sys
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_cfb import (RESULT_COLS, EPA_COLS, ADJ_COLS, PBP_KEYS, PBP_COLS, RATE_SPLITS, ADJ_RATE_COLS,  # noqa: E402
                      adjust_for_opponents, betting_line, calendar_weeks, get_json, load, overall_record)

BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
ATHLETE = "https://site.api.espn.com/apis/common/v3/sports/football/nfl/athletes"
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
SCHEDULE_CSV = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
DIR = Path(__file__).resolve().parent.parent / "data" / "nfl"
FIRST_PAST_SEASON = 2016

DIVISIONS = {
    "afc-east": ("AFC East", ["BUF", "MIA", "NE", "NYJ"]), "afc-north": ("AFC North", ["BAL", "CIN", "CLE", "PIT"]),
    "afc-south": ("AFC South", ["HOU", "IND", "JAX", "TEN"]), "afc-west": ("AFC West", ["DEN", "KC", "LAC", "LV"]),
    "nfc-east": ("NFC East", ["DAL", "NYG", "PHI", "WSH"]), "nfc-north": ("NFC North", ["CHI", "DET", "GB", "MIN"]),
    "nfc-south": ("NFC South", ["ATL", "CAR", "NO", "TB"]), "nfc-west": ("NFC West", ["ARI", "LAR", "SEA", "SF"]),
}
DIV_OF = {abbr: div for div, (_, teams) in DIVISIONS.items() for abbr in teams}
# nflverse abbreviations that differ from ESPN's
NFLVERSE_ABBR = {"LA": "LAR", "WAS": "WSH"}

QB_COLS = ["gameId", "week", "player", "team", "opp", "confGame", "comp", "att", "yds", "td", "int", "db", "epa",
           "cpAtt", "cpComp", "cpExp", "car", "ryds", "rtd"]
RB_COLS = ["gameId", "week", "player", "team", "opp", "confGame", "car", "ryds", "rtd", "rec", "recYds", "recTd",
           "n", "epa", "succ", "expl12", "expl20", "stuff", "hl", "hlOpp", "rz", "gl", "syAtt", "syConv",
           "tgt", "tgtN", "tgtEpa", "teamRush"]
WR_COLS = ["gameId", "week", "player", "team", "opp", "confGame", "rec", "recYds", "recTd", "car", "ryds", "rtd",
           "tgt", "cat", "tyds", "rz", "airN", "air", "yacN", "yac", "n", "epa", "succ", "expl", "teamTgt", "teamAir"]


def download(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sports-tracker"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


def read_csv(url):
    raw = download(url)
    if url.endswith(".gz"):
        raw = gzip.decompress(raw)
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))


def num(v, default=0.0):
    try:
        return float(v) if v not in ("", "NA", None) else default
    except ValueError:
        return default


def flag(v):
    return v in ("1", "1.0", "TRUE", "True")


def is_garbage(p):
    wp = num(p.get("wp"), 0.5)
    return wp < 0.05 or wp > 0.95


def yard_success(p):
    if flag(p.get("touchdown")) and p.get("td_team") == p.get("posteam"):
        return True
    if flag(p.get("interception")) or flag(p.get("fumble_lost")):
        return False
    down, dist = int(num(p.get("down"))), num(p.get("ydstogo"))
    need = dist * (0.5 if down == 1 else 0.7 if down == 2 else 1.0)
    return num(p.get("yards_gained")) >= need


def scrimmage_plays(rows):
    """Real snaps: passes (incl. sacks and scrambles) and runs with a down, no two-point tries or no-plays."""
    for p in rows:
        if p.get("season_type") not in ("REG", "POST") or p.get("play_type") not in ("pass", "run", "qb_kneel", "qb_spike"):
            continue
        if flag(p.get("two_point_attempt")) or p.get("down") in ("", "NA") or p.get("epa") in ("", "NA"):
            continue
        yield p


def team_counts(plays):
    """PBP_KEYS counts for one offense in one game (garbage time excluded)."""
    c = dict.fromkeys(PBP_KEYS, 0)
    for p in plays:
        if is_garbage(p) or p.get("play_type") in ("qb_kneel", "qb_spike"):
            continue
        down, dist = int(num(p["down"])), num(p["ydstogo"])
        s = yard_success(p)
        c["N"] += 1
        c["Succ"] += s
        early = down <= 2
        c["EarlyN" if early else "LateN"] += 1
        c["EarlyS" if early else "LateS"] += s
        std = down == 1 or (down == 2 and dist <= 7) or (down >= 3 and dist <= 4)
        c["StdN" if std else "PdN"] += 1
        c["StdS" if std else "PdS"] += s
        sack = flag(p.get("sack"))
        if flag(p.get("qb_dropback")):
            c["Db"] += 1
            c["Sacks"] += sack
            c["Press"] += sack or flag(p.get("qb_hit"))
        c["Havoc"] += (sack or flag(p.get("interception")) or flag(p.get("fumble_forced")) or flag(p.get("fumble"))
                       or flag(p.get("tackled_for_loss")) or p.get("pass_defense_1_player_id") not in ("", "NA", None))
    return [c[k] for k in PBP_KEYS]


def team_epa(plays):
    """EPA_COLS offense half: plays, EPA, dropbacks, dropback EPA, designed runs, run EPA (garbage time excluded)."""
    out = [0, 0.0, 0, 0.0, 0, 0.0]
    for p in plays:
        if is_garbage(p) or p.get("play_type") in ("qb_kneel", "qb_spike"):
            continue
        e = num(p["epa"])
        out[0] += 1
        out[1] += e
        if flag(p.get("qb_dropback")):
            out[2] += 1
            out[3] += e
        elif flag(p.get("rush")):
            out[4] += 1
            out[5] += e
    return [round(v, 3) if isinstance(v, float) else v for v in out]


def highlight(gain):
    return 0.5 * min(gain - 4, 6) + max(gain - 10, 0) if gain >= 4 else 0.0


class Ids:
    """nflverse player ids -> our ids (ESPN's when known, so photos and bios work), names and positions."""

    def __init__(self, players):
        self.by_gsis = {}
        for p in players:
            g = p.get("gsis_id")
            if g:
                self.by_gsis[g] = {"id": p.get("espn_id") or g, "name": p.get("display_name") or "",
                                   "pos": p.get("position") or "", "espn": p.get("espn_id") or ""}

    def get(self, gsis):
        return self.by_gsis.get(gsis) or {"id": gsis, "name": gsis, "pos": "", "espn": ""}


def player_games(plays_by_game, ids, meta, teams_by_abbr):
    """qbs/rbs/wrs rows from every snap of every completed game."""
    qb, rb, wr, players = {}, {}, {}, {}
    team_rush, team_tgt, team_air = {}, {}, {}

    def row(store, gid, pid, team, cols):
        return store.setdefault((gid, pid, team), dict.fromkeys(cols[6:], 0))

    for gid, plays in plays_by_game.items():
        for p in plays:
            team = teams_by_abbr.get(p["posteam"])
            if not team:
                continue
            garbage = is_garbage(p)
            e = num(p["epa"])
            gain = num(p.get("yards_gained"))
            ytg = num(p.get("yardline_100"), 100)
            down, dist = int(num(p["down"])), num(p["ydstogo"])
            if p.get("play_type") == "qb_spike":  # an official incompletion, but not a real play
                qid = p.get("passer_player_id") or p.get("passer_id")
                if qid not in ("", "NA", None):
                    who = ids.get(qid)
                    players[who["id"]] = who
                    row(qb, gid, who["id"], team, QB_COLS)["att"] += 1
                continue
            dropback = flag(p.get("qb_dropback"))
            attempt = p.get("play_type") == "pass" and not flag(p.get("sack")) and p.get("passer_player_id") not in ("", "NA")
            # Quarterback: every dropback (incl. sacks and scrambles) for EPA; official passing for the box score
            qid = p.get("passer_id") or p.get("passer_player_id")
            if dropback and qid not in ("", "NA", None):
                who = ids.get(qid)
                r = row(qb, gid, who["id"], team, QB_COLS)
                players[who["id"]] = who
                if attempt:
                    r["att"] += 1
                    r["comp"] += flag(p.get("complete_pass"))
                    r["yds"] += num(p.get("passing_yards"))
                    r["td"] += flag(p.get("pass_touchdown"))
                    r["int"] += flag(p.get("interception"))
                if not garbage:
                    r["db"] += 1
                    r["epa"] += e
                    if attempt and p.get("cp") not in ("", "NA"):
                        r["cpAtt"] += 1
                        r["cpComp"] += flag(p.get("complete_pass"))
                        r["cpExp"] += num(p.get("cp"))
            # Rushing (designed runs and scrambles count as carries, like the official stats)
            rid = p.get("rusher_player_id")
            if p.get("play_type") in ("run", "qb_kneel") and rid not in ("", "NA", None):
                who = ids.get(rid)
                players[who["id"]] = who
                ryds, rtd = num(p.get("rushing_yards")), flag(p.get("rush_touchdown")) and p.get("td_player_id") in (rid, "", "NA", None)
                if flag(p.get("rush")):
                    team_rush[(gid, team)] = team_rush.get((gid, team), 0) + 1
                for store, cols in ((qb, QB_COLS), (rb, RB_COLS), (wr, WR_COLS)):
                    if store is qb and who["pos"] != "QB":
                        continue
                    if store is not qb and who["pos"] == "QB":
                        continue
                    r = row(store, gid, who["id"], team, cols)
                    r["car"] += 1
                    r["ryds"] += ryds
                    r["rtd"] += rtd
                if who["pos"] != "QB":
                    r = row(rb, gid, who["id"], team, RB_COLS)
                    r["rz"] += ytg <= 20
                    r["gl"] += ytg <= 5
                    if down >= 3 and dist <= 2:
                        r["syAtt"] += 1
                        r["syConv"] += gain >= dist or rtd
                    if not garbage and flag(p.get("rush")) and p.get("play_type") == "run":
                        r["n"] += 1
                        r["epa"] += e
                        r["succ"] += yard_success(p)
                        r["expl12"] += gain >= 12
                        r["expl20"] += gain >= 20
                        r["stuff"] += gain <= 0
                        if gain >= 4:
                            r["hlOpp"] += 1
                            r["hl"] += highlight(gain)
            # Targets
            if attempt:
                team_tgt[(gid, team)] = team_tgt.get((gid, team), 0) + 1
                air = p.get("air_yards")
                if air not in ("", "NA"):
                    team_air[(gid, team)] = team_air.get((gid, team), 0) + num(air)
                tid = p.get("receiver_player_id")
                if tid in ("", "NA", None):
                    continue
                who = ids.get(tid)
                players[who["id"]] = who
                caught = flag(p.get("complete_pass"))
                recyds = num(p.get("receiving_yards"))
                # After a catch-and-lateral, the yards past the lateral (and the TD) go to whoever got the ball
                lat = p.get("lateral_receiver_player_id")
                if caught and lat not in ("", "NA", None):
                    lw = ids.get(lat)
                    players[lw["id"]] = lw
                    for store, cols in ((rb, RB_COLS), (wr, WR_COLS)):
                        lr = row(store, gid, lw["id"], team, cols)
                        lr["recYds"] += num(p.get("lateral_receiving_yards"))
                        lr["recTd"] += flag(p.get("pass_touchdown")) and p.get("td_player_id") == lat
                for store, cols in ((rb, RB_COLS), (wr, WR_COLS)):
                    r = row(store, gid, who["id"], team, cols)
                    r["tgt"] += 1
                    r["rec"] += caught
                    r["recYds"] += recyds
                    r["recTd"] += caught and flag(p.get("pass_touchdown")) and p.get("td_player_id") in (tid, "", "NA", None)
                    if store is rb:
                        if not garbage:
                            r["tgtN"] += 1
                            r["tgtEpa"] += e
                    else:
                        r["cat"] += caught
                        r["tyds"] += recyds
                        r["rz"] += ytg <= 20
                        if air not in ("", "NA"):
                            r["airN"] += 1
                            r["air"] += num(air)
                            if caught:
                                r["yacN"] += 1
                                r["yac"] += num(p.get("yards_after_catch"))
                        if not garbage:
                            r["n"] += 1
                            r["epa"] += e
                            r["succ"] += caught and yard_success(p)
                            r["expl"] += caught and gain >= 20

    def out(store, cols, positions, team_cols):
        rows, used = [], {}
        for (gid, pid, team), v in store.items():
            who = players.get(pid, {})
            if who.get("pos") not in positions or (gid, team) not in meta:
                continue
            week, opp, conf = meta[(gid, team)]
            vals = [round(v[k], 3) if isinstance(v[k], float) else int(v[k]) if isinstance(v[k], bool) else v[k] for k in cols[6:]]
            vals = [int(x) if isinstance(x, float) and x.is_integer() and k not in ("epa", "tgtEpa", "hl", "cpExp") else x
                    for k, x in zip(cols[6:], vals)]
            extra = team_cols(gid, team)
            if extra:
                vals = vals[:len(vals) - len(extra)] + extra
            rows.append([gid, week, pid, team, opp, conf] + vals)
            used[pid] = {"name": who.get("name") or pid, "team": team, "pos": who.get("pos")}
        rows.sort(key=lambda r: (r[1], r[0], r[2]))
        return {"cols": cols, "players": dict(sorted(used.items())), "rows": rows}

    qbs = out(qb, QB_COLS, {"QB"}, lambda g, t: None)
    rbs = out(rb, RB_COLS, {"RB", "FB"}, lambda g, t: [team_rush.get((g, t), 0)])
    wrs = out(wr, WR_COLS, {"WR", "TE"}, lambda g, t: [team_tgt.get((g, t), 0), round(team_air.get((g, t), 0), 1)])
    # RB/FB show as RB, positions besides WR/TE never reach the receiver file
    for pid in rbs["players"]:
        rbs["players"][pid]["pos"] = "RB"
    for d in (qbs, rbs, wrs):
        for p in d["players"].values():
            p.pop("pos", None) if d is not wrs else None
    return qbs, rbs, wrs


def past_epa(ids, cache_path, current_year):
    """{season: {player id: [EPA per dropback, EPA per designed run, EPA per target]}} for past seasons.

    Each season's play-by-play (about 15 MB) is downloaded once; garbage time is excluded like this season's."""
    cache = load(cache_path, {})
    for season in range(FIRST_PAST_SEASON, int(current_year)):
        if str(season) in cache:
            continue
        try:
            rows = read_csv(f"{NFLVERSE}/pbp/play_by_play_{season}.csv.gz")
        except Exception as exc:
            print(f"  past play-by-play {season}: {exc}", file=sys.stderr)
            continue
        acc = {}
        for p in scrimmage_plays(rows):
            if p.get("season_type") != "REG" or is_garbage(p):
                continue
            e = num(p["epa"])
            for col, slot, ok in (("passer_id", 0, flag(p.get("qb_dropback"))),
                                  ("rusher_player_id", 1, flag(p.get("rush"))),
                                  ("receiver_player_id", 2, p.get("play_type") == "pass" and not flag(p.get("sack")))):
                g = p.get(col)
                if ok and g not in ("", "NA", None):
                    a = acc.setdefault(ids.get(g)["id"], [0.0, 0, 0.0, 0, 0.0, 0])
                    a[slot * 2] += e
                    a[slot * 2 + 1] += 1
        cache[str(season)] = {pid: [round(a[0] / a[1], 3) if a[1] >= 20 else None,
                                    round(a[2] / a[3], 3) if a[3] >= 20 else None,
                                    round(a[4] / a[5], 3) if a[5] >= 10 else None] for pid, a in acc.items()}
        print(f"Past EPA {season}: {len(cache[str(season)])} players")
        cache_path.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True))
    return cache


CAREER_KINDS = {
    "qb": {"file": "qbs.json", "touch": ("att",), "min": 40, "cats": ("passing", "rushing"), "epa": 0},
    "rb": {"file": "rbs.json", "touch": ("car", "rec"), "min": 25, "cats": ("rushing", "receiving"), "epa": 1},
    "wr": {"file": "wrs.json", "touch": ("tgt",), "min": 15, "cats": ("rushing", "receiving"), "epa": 2},
}


def build_careers(kind, past, extra_bio, year):
    """ESPN bio and career lines (refreshed weekly) plus our past-season EPA, like data/cfb/*careers.json."""
    cfg = CAREER_KINDS[kind]
    q = json.loads((DIR / cfg["file"]).read_text())
    c = {k: i for i, k in enumerate(q["cols"])}
    touches = {}
    for r in q["rows"]:
        touches[r[c["player"]]] = touches.get(r[c["player"]], 0) + sum(r[c[k]] for k in cfg["touch"])
    wanted = [pid for pid, t in touches.items() if t >= cfg["min"] and pid.isdigit()]  # ESPN ids only
    cache_path = DIR / f"{kind}careercache.json"
    cache = load(cache_path, {"players": {}})
    today = date.today()
    due = [pid for pid in wanted if pid not in cache["players"]
           or (today - date.fromisoformat(cache["players"][pid].get("fetched", "2000-01-01"))).days > 7]
    for pid in due[:300]:
        try:
            a = get_json(f"{ATHLETE}/{pid}").get("athlete", {})
            st = get_json(f"{ATHLETE}/{pid}/stats")
        except Exception as exc:
            print(f"  {kind} {pid}: {exc}", file=sys.stderr)
            continue
        seasons = {}
        for cat in st.get("categories", []):
            if cat.get("name") not in cfg["cats"]:
                continue
            labels = cat.get("labels", [])
            for row in cat.get("statistics", []):
                season = str((row.get("season") or {}).get("year") or "")
                team = row.get("teamSlug") or ""
                if not row.get("teamId") or "total" in team.lower():
                    continue  # a traded player's combined line; each team has its own
                s = seasons.setdefault((season, team), {"season": season, "teamSlug": team, "teamId": str(row.get("teamId") or "")})
                vals = dict(zip(labels, row.get("stats", [])))

                def n(k):
                    try:
                        return int(float(str(vals.get(k, "0")).replace(",", "")))
                    except ValueError:
                        return 0
                if cat["name"] == "passing":
                    s.update(cmp=n("CMP"), att=n("ATT"), yds=n("YDS"), td=n("TD"), int=n("INT"), sacks=n("SACK"))
                elif cat["name"] == "rushing":
                    s.update(car=n("CAR"), ryds=n("YDS"), rtd=n("TD"), rlng=n("LNG"))
                else:
                    s.update(rec=n("REC"), recYds=n("YDS"), recTd=n("TD"), tgt=n("TGTS"))
        cache["players"][pid] = {
            "name": a.get("displayName", ""), "height": a.get("displayHeight", ""), "weight": a.get("displayWeight", ""),
            "birthPlace": a.get("displayBirthPlace", ""), "jersey": a.get("displayJersey", ""),
            "position": (a.get("position") or {}).get("abbreviation", ""),
            "college": (a.get("college") or {}).get("name", ""), "draft": a.get("displayDraft", ""),
            "age": a.get("age"), "experience": a.get("displayExperience", ""),
            "seasons": sorted(seasons.values(), key=lambda s: (s["season"], s["teamSlug"])),
            "fetched": today.isoformat(),
        }
        time.sleep(0.1)
    cache_path.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True))
    out = {}
    for pid in wanted:
        p = cache["players"].get(pid)
        if not p:
            continue
        out[pid] = {k: p.get(k) for k in ("name", "height", "weight", "birthPlace", "jersey", "position",
                                          "college", "draft", "age", "experience")}
        out[pid]["seasons"] = [dict(s, epa=(past.get(s["season"], {}).get(pid) or [None] * 3)[cfg["epa"]])
                               for s in p["seasons"] if s["season"] != str(year)] + \
                              [dict(s, epa=None) for s in p["seasons"] if s["season"] == str(year)]
        out[pid]["seasons"].sort(key=lambda s: (s["season"], s["teamSlug"]))
    (DIR / f"{kind}careers.json").write_text(json.dumps(out, separators=(",", ":"), sort_keys=True))
    print(f"Saved careers for {len(out)} {kind.upper()}s ({min(len(due), 300)} fetched this run)")


def main():
    DIR.mkdir(parents=True, exist_ok=True)
    first = get_json(f"{BASE}/scoreboard")
    year = first.get("season", {}).get("year")
    weeks = [w for w in calendar_weeks(first) if w["label"] != "Pro Bowl"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    current = next((i for i, w in enumerate(weeks) if (w["end"] or "") >= now), len(weeks) - 1)

    box_cache = load(DIR / "boxscores.json", {})
    line_cache = load(DIR / "lines.json", {})
    sched = {g["espn"]: g for g in read_csv(SCHEDULE_CSV) if g.get("season") == str(year) and g.get("espn")}
    teams, results, out_weeks = {}, [], []
    confs = {div: {"name": name, "short": name} for div, (name, _) in DIVISIONS.items()}

    for wi, w in enumerate(weeks):
        events = first["events"] if (w["stype"], w["week"]) == (first["season"]["type"], first["week"]["number"]) else \
            get_json(f"{BASE}/scoreboard?seasontype={w['stype']}&week={w['week']}&dates={year}").get("events", [])
        games = []
        for ev in events:
            if not ev.get("competitions"):
                continue
            comp = ev["competitions"][0]
            st = ev["status"]["type"]
            sides = {}
            for c in comp["competitors"]:
                t = c["team"]
                tid, abbr = str(t["id"]), t.get("abbreviation", "")
                if abbr not in DIV_OF:
                    continue
                teams[tid] = {"name": t.get("shortDisplayName") or t.get("name", ""), "full": t.get("displayName", ""),
                              "abbr": abbr, "mascot": t.get("name", ""),
                              "logo": f"https://a.espncdn.com/i/teamlogos/nfl/500/{abbr.lower()}.png", "conf": DIV_OF[abbr]}
                sides[c["homeAway"]] = {"id": tid, "score": c.get("score"), "record": overall_record(c), "rank": None}
            if len(sides) != 2:
                continue
            line = betting_line(comp)
            if line and st.get("state") == "pre":
                line_cache[ev["id"]] = line
            line = line or line_cache.get(ev["id"])
            s = sched.get(ev["id"])
            if not line and s and s.get("spread_line") not in ("", "NA"):
                # nflverse closing line: spread_line is how many points the home team is favored by
                sp = num(s["spread_line"])
                home, away = teams[sides["home"]["id"]]["abbr"], teams[sides["away"]["id"]]["abbr"]
                ml = lambda v: None if v in ("", "NA", None) else f"{int(num(v)):+d}"
                line = {"book": "Closing line", "spread": "EVEN" if sp == 0 else f"{home} -{sp:g}" if sp > 0 else f"{away} -{-sp:g}",
                        "total": num(s.get("total_line"), None), "mlAway": ml(s.get("away_moneyline")), "mlHome": ml(s.get("home_moneyline"))}
            a, h = sides["away"], sides["home"]
            div_game = int(DIV_OF[teams[a["id"]]["abbr"]] == DIV_OF[teams[h["id"]]["abbr"]])
            games.append({
                "id": ev["id"], "start": ev["date"], "state": st.get("state"), "detail": st.get("shortDetail", ""),
                "away": a, "home": h, "neutral": comp.get("neutralSite", False), "confGame": bool(div_game),
                "venue": (comp.get("venue") or {}).get("fullName", ""),
                "tv": comp.get("broadcast") or ", ".join(n for b in comp.get("broadcasts", []) for n in b.get("names", [])),
                "note": ((comp.get("notes") or [{}])[0] or {}).get("headline", ""), "line": line,
            })
            if st.get("state") == "post":
                if ev["id"] not in box_cache:
                    try:
                        summary = get_json(f"{BASE}/summary?event={ev['id']}")
                        yards = {}
                        for t in (summary.get("boxscore") or {}).get("teams", []):
                            stats = {x.get("name"): x.get("displayValue") for x in t.get("statistics", [])}
                            try:
                                yards[str(t["team"]["id"])] = int(str(stats.get("totalYards", "")).replace(",", ""))
                            except ValueError:
                                pass
                        box_cache[ev["id"]] = yards
                        time.sleep(0.2)
                    except Exception as exc:
                        print(f"  box score {ev['id']}: {exc}", file=sys.stderr)
                yards = box_cache.get(ev["id"], {})
                neutral = comp.get("neutralSite", False)
                ap, hp = int(num(a["score"])), int(num(h["score"]))
                for me, opp, pf, pa, site in ((a, h, ap, hp, -1), (h, a, hp, ap, 1)):
                    results.append([ev["id"], wi, ev["date"], me["id"], opp["id"], div_game, pf, pa,
                                    yards.get(me["id"]), yards.get(opp["id"]), 0 if neutral else site])
        games.sort(key=lambda g: (g["start"], g["id"]))
        out_weeks.append({**{k: w[k] for k in ("label", "dates", "stype", "week", "start", "end")}, "games": games})
        print(f"{w['label']}: {len(games)} games")
        time.sleep(0.2)
    results.sort(key=lambda r: (r[2], r[0], r[3]))

    # Play-by-play: nflverse game ids -> ESPN ids, nflverse team abbreviations -> ESPN team ids
    by_abbr = {t["abbr"]: tid for tid, t in teams.items()}
    teams_by_abbr = {k: by_abbr[NFLVERSE_ABBR.get(k, k)] for k in set(DIV_OF) | set(NFLVERSE_ABBR) if NFLVERSE_ABBR.get(k, k) in by_abbr}
    gid_map = {g["game_id"]: e for e, g in sched.items()}
    try:
        pbp = read_csv(f"{NFLVERSE}/pbp/play_by_play_{year}.csv.gz")
    except Exception as exc:
        print(f"  play-by-play: {exc}", file=sys.stderr)
        pbp = []
    done = {r[0] for r in results}
    plays_by_game = {}
    for p in scrimmage_plays(pbp):
        gid = gid_map.get(p.get("game_id"))
        if gid in done:
            plays_by_game.setdefault(gid, []).append(p)
    print(f"Play-by-play: {sum(len(v) for v in plays_by_game.values())} snaps in {len(plays_by_game)} of {len(done)} completed games")

    by_offense = {}
    for gid, plays in plays_by_game.items():
        for p in plays:
            by_offense.setdefault((gid, teams_by_abbr.get(p["posteam"])), []).append(p)
    for r in results:
        mine, theirs = by_offense.get((r[0], r[3])), by_offense.get((r[0], r[4]))
        r.extend((team_epa(mine) if mine else [None] * 6) + (team_epa(theirs) if theirs else [None] * 6))
    col = {k: i for i, k in enumerate(RESULT_COLS + EPA_COLS)}
    fbs = set(teams)
    adjust_for_opponents(results, fbs, col, [("offEpa", "offPlays", 250), ("offPassEpa", "offPassPlays", 120),
                                             ("offRushEpa", "offRushPlays", 120)])
    for r in results:
        mine, theirs = by_offense.get((r[0], r[3])), by_offense.get((r[0], r[4]))
        r.extend((team_counts(mine) if mine else [None] * len(PBP_KEYS)) + (team_counts(theirs) if theirs else [None] * len(PBP_KEYS)))
    col = {k: i for i, k in enumerate(RESULT_COLS + EPA_COLS + ADJ_COLS + PBP_COLS)}
    adjust_for_opponents(results, fbs, col, [("off" + e, "off" + n, sh) for e, n, sh in RATE_SPLITS])

    # Players
    players_path = DIR / "nflverse_players.json"
    plist = load(players_path, {})
    if not plist.get("fetched") or (date.today() - date.fromisoformat(plist["fetched"])).days > 6:
        try:
            rows = read_csv(f"{NFLVERSE}/players/players.csv")
            plist = {"fetched": date.today().isoformat(),
                     "players": [{k: p.get(k, "") for k in ("gsis_id", "espn_id", "display_name", "position")}
                                 for p in rows if p.get("gsis_id") and (p.get("last_season") or "0") >= str(FIRST_PAST_SEASON)]}
            players_path.write_text(json.dumps(plist, separators=(",", ":")))
        except Exception as exc:
            print(f"  players: {exc}", file=sys.stderr)
    ids = Ids(plist.get("players", []))
    meta = {(r[0], r[3]): (r[1], r[4], r[5]) for r in results}
    qbs, rbs, wrs = player_games(plays_by_game, ids, meta, teams_by_abbr)
    for name, d in (("qbs", qbs), ("rbs", rbs), ("wrs", wrs)):
        (DIR / f"{name}.json").write_text(json.dumps(d, separators=(",", ":")))
        print(f"Saved {len(d['rows'])} {name[:-1].upper()}-games for {len(d['players'])} players")
    past = past_epa(ids, DIR / "pastepa.json", year)
    for kind in ("qb", "rb", "wr"):
        try:
            build_careers(kind, past, None, year)
        except Exception as exc:
            print(f"  {kind} careers failed: {exc}", file=sys.stderr)

    (DIR / "boxscores.json").write_text(json.dumps(box_cache, separators=(",", ":"), sort_keys=True))
    (DIR / "lines.json").write_text(json.dumps(line_cache, separators=(",", ":"), sort_keys=True))
    (DIR / "season.json").write_text(json.dumps({
        "season": year, "updated": now, "current": current, "poll": None, "ranks": {},
        "conferences": confs, "teams": dict(sorted(teams.items())), "weeks": out_weeks,
        "resultCols": RESULT_COLS + EPA_COLS + ADJ_COLS + PBP_COLS + ADJ_RATE_COLS, "results": results,
    }, separators=(",", ":")))
    print(f"Saved data/nfl/season.json: {len(out_weeks)} weeks, {len(done)} completed games")


if __name__ == "__main__":
    main()
