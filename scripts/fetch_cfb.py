#!/usr/bin/env python3
"""Fetch the college football (FBS) season from ESPN's public API.

Writes data/cfb/season.json with:
  - every week of the season (regular season and postseason) with each FBS game:
    kickoff, status and score, TV, venue, records and the betting line
  - the current ranking (College Football Playoff when published, otherwise AP Top 25)
  - FBS conferences and teams
  - one row per team per completed game (points and total yards for and against),
    which the page uses to work out per-game stats for any filter

EPA (expected points added) per play, per dropback and per rush, for offense and defense,
comes from CollegeFootballData.com (garbage time excluded). It needs a free API key in the
CFBD_API_KEY environment variable; without one, the last saved EPA (data/cfb/epa.json) is kept.

Also keeps two caches so each game is only downloaded once:
  data/cfb/boxscores.json  total yards per team for completed games
  data/cfb/lines.json      the last betting line seen before each game (ESPN drops lines after kickoff)

Usage:
    python3 scripts/fetch_cfb.py
"""
import json
import os
import re
import sys
import unicodedata
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
DIR = Path(__file__).resolve().parent.parent / "data" / "cfb"
OUT = DIR / "season.json"
BOX_CACHE = DIR / "boxscores.json"
LINE_CACHE = DIR / "lines.json"
FBS = 80
CFBD = "https://api.collegefootballdata.com"
EPA_OUT = DIR / "epa.json"
# Per team-game EPA columns: total EPA and play counts, offense then defense.
RESULT_COLS = ["gameId", "week", "date", "team", "opp", "confGame", "pf", "pa", "yds", "oppYds", "site"]
# Opponent-adjusted total EPA for each team-game (divide by the matching play count for per-play).
ADJ_COLS = ["adjOffEpa", "adjOffPassEpa", "adjOffRushEpa", "adjDefEpa", "adjDefPassEpa", "adjDefRushEpa"]
# Play-by-play counts per team-game (garbage time excluded): offense, then the same for the opponent's
# offense (what this team's defense allowed). Each rate is (events key) / (plays key).
PBP_KEYS = ["N", "Succ", "EarlyN", "EarlyS", "LateN", "LateS", "StdN", "StdS", "PdN", "PdS",
            "Db", "Sacks", "Press", "Havoc"]
PBP_COLS = ["off" + k for k in PBP_KEYS] + ["def" + k for k in PBP_KEYS]
# (events, plays, shrink) for each rate that gets an opponent adjustment
RATE_SPLITS = [("Succ", "N", 150), ("EarlyS", "EarlyN", 120), ("LateS", "LateN", 80), ("StdS", "StdN", 120),
               ("PdS", "PdN", 80), ("Sacks", "Db", 120), ("Press", "Db", 120), ("Havoc", "N", 150)]
ADJ_RATE_COLS = ["adjOff" + e for e, _, _ in RATE_SPLITS] + ["adjDef" + e for e, _, _ in RATE_SPLITS]
# 3rd downs (from the box score, every play) and red-zone trips/touchdowns, offense then what the defense allowed
SIT_COLS = ["offThirdC", "offThirdA", "offRzTd", "offRzN", "defThirdC", "defThirdA", "defRzTd", "defRzN"]
THIRD_CACHE = DIR / "thirddowns.json"
PLAYS_CACHE = DIR / "playstats.json"
QB_CACHE = DIR / "qbgames.json"
QB_OUT = DIR / "qbs.json"
EPA_COLS = ["offPlays", "offEpa", "offPassPlays", "offPassEpa", "offRushPlays", "offRushEpa",
            "defPlays", "defEpa", "defPassPlays", "defPassEpa", "defRushPlays", "defRushEpa"]


def get_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sports-tracker/1.0"})
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.load(resp)
        except Exception as exc:  # network hiccup: back off and retry
            if attempt == retries - 1:
                raise
            print(f"  retry {url}: {exc}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def overall_record(competitor):
    recs = competitor.get("records") or []
    for r in recs:
        if r.get("type") == "total" or r.get("name", "").lower() == "overall":
            return r.get("summary")
    return recs[0].get("summary") if recs else None


def betting_line(comp):
    odds = comp.get("odds") or []
    if not odds:
        return None
    o = odds[0]

    def moneyline(side):
        block = (o.get("moneyline") or {}).get(side) or {}
        for when in ("close", "current", "open"):
            value = (block.get(when) or {}).get("odds")
            if value:
                return value
        return None

    line = {
        "book": (o.get("provider") or {}).get("name"),
        "spread": o.get("details"),
        "total": o.get("overUnder"),
        "mlAway": moneyline("away"),
        "mlHome": moneyline("home"),
    }
    return line if any(line[k] for k in ("spread", "total", "mlAway", "mlHome")) else None


LINE_BOOKS = ("DraftKings", "ESPN Bet", "Bovada", "consensus")


def backfill_lines(year, out_weeks, teams, line_cache):
    """Completed games with no saved line (before we started saving them) get CollegeFootballData's
    closing line, in the same shape as ESPN's. Each game is only looked up once."""
    key = os.environ.get("CFBD_API_KEY")
    tried = set(line_cache.get("_tried", []))
    missing = {}
    for w in out_weeks:
        for g in w["games"]:
            if g["state"] == "post" and not g.get("line") and g["id"] not in tried:
                missing.setdefault("regular" if w["stype"] == 2 else "postseason", []).append(g)
    if not key or not missing:
        return
    filled = 0
    for stype, games in missing.items():
        try:
            rows = {str(r["id"]): r for r in cfbd_get(f"/lines?year={year}&seasonType={stype}", key)}
        except Exception as exc:
            print(f"  betting lines ({stype}): {exc}", file=sys.stderr)
            continue
        for g in games:
            tried.add(g["id"])
            r = rows.get(g["id"])
            books = {l.get("provider"): l for l in (r or {}).get("lines", [])}
            l = next((books[b] for b in LINE_BOOKS if b in books), None) or next(iter(books.values()), None)
            if not l:
                continue
            sp = l.get("spread")
            abbr = lambda side: teams.get((g.get(side) or {}).get("id"), {}).get("abbr", "")
            spread = None
            if sp is not None:
                spread = "EVEN" if sp == 0 else f"{abbr('home')} {sp:g}" if sp < 0 else f"{abbr('away')} -{sp:g}"
            ml = lambda v: None if v is None else f"{v:+d}"
            line = {"book": l.get("provider"), "spread": spread, "total": l.get("overUnder"),
                    "mlAway": ml(l.get("awayMoneyline")), "mlHome": ml(l.get("homeMoneyline"))}
            if any(line[k] for k in ("spread", "total", "mlAway", "mlHome")):
                g["line"] = line_cache[g["id"]] = line
                filled += 1
    line_cache["_tried"] = sorted(tried)
    print(f"Betting lines: filled in {filled} past games from CollegeFootballData")


def calendar_weeks(scoreboard):
    """Every week of the season in order: dicts with seasontype, week, label, dates, start, end."""
    weeks = []
    for part in scoreboard["leagues"][0].get("calendar", []):
        stype = str(part.get("value"))
        if stype not in ("2", "3"):  # regular season and postseason
            continue
        for e in part.get("entries", []):
            weeks.append({"stype": int(stype), "week": int(e["value"]), "label": e.get("label") or e.get("alternateLabel"),
                          "dates": e.get("detail", ""), "start": e.get("startDate"), "end": e.get("endDate")})
    return weeks


def fit_ratings(games, shrink, rounds=200):
    """Rate every offense and defense at once from per-game EPA per play.

    Each game says: offense EPA/play = league average + offense rating + opponent defense rating
    + home field. Ratings are solved by repeated weighted averaging (each game counts by its plays),
    shrunk toward zero by `shrink` plays so teams with few games (mostly FCS) don't swing wildly.
    games: list of (offense team, defense team, site, epa_per_play, plays); site is +1 home, -1 away, 0 neutral.
    Returns (avg, off, defn, hfa).
    """
    games = [g for g in games if g[4] > 0]
    total = sum(g[4] for g in games)
    avg = sum(g[3] * g[4] for g in games) / total
    off, defn, hfa = {}, {}, 0.0
    by_off, by_def = {}, {}
    for g in games:
        by_off.setdefault(g[0], []).append(g)
        by_def.setdefault(g[1], []).append(g)
    for _ in range(rounds):
        for t, gs in by_off.items():
            off[t] = sum((y - avg - defn.get(d, 0) - hfa * site) * w for _, d, site, y, w in gs) / (sum(w for *_, w in gs) + shrink)
        for t, gs in by_def.items():
            defn[t] = sum((y - avg - off.get(o, 0) - hfa * site) * w for o, _, site, y, w in gs) / (sum(w for *_, w in gs) + shrink)
        home = [g for g in games if g[2]]
        if home:
            hfa = sum((y - avg - off.get(o, 0) - defn.get(d, 0)) * site * w for o, d, site, y, w in home) / sum(w for *_, w in home)
    return avg, off, defn, hfa


def adjust_for_opponents(results, fbs, col, splits):
    """Opponent-adjusted totals for each team-game, appended to each row in place.

    `splits` is a list of (off events key, off plays key, shrink). For each, every offense and defense
    is rated at once from per-game rates (see fit_ratings). A game's adjusted offense = its rate minus
    how much that opponent's defense usually gives up (relative to the average FBS defense) minus home
    field; adjusted defense works the same way against the opponent's offense. Opponent strength comes
    from every game so far; the page then picks which games count. Ratings are measured against the
    average FBS team, so an adjusted number reads as "what this team did against an average FBS opponent".
    Appends, per split, the adjusted offensive total, then per split the adjusted defensive total
    (adjusted rate x plays, so the page can add games up and divide by plays).
    """
    fits = []
    for total_key, plays_key, shrink in splits:
        games = []
        for r in results:
            plays, total = r[col[plays_key]], r[col[total_key]]
            if plays:
                games.append((r[col["team"]], r[col["opp"]], r[col["site"]], total / plays, plays))
        fit = fit_ratings(games, shrink) if games else None
        if fit:
            avg, off, defn, hfa = fit
            fo = [off[t] for t in off if t in fbs]
            fd = [defn[t] for t in defn if t in fbs]
            mo, md = (sum(fo) / len(fo) if fo else 0), (sum(fd) / len(fd) if fd else 0)
            fit = (avg + mo + md, {t: v - mo for t, v in off.items()}, {t: v - md for t, v in defn.items()}, hfa)
        fits.append(fit)
    for r in results:
        adj = []
        for side in ("off", "def"):
            for (total_key, plays_key, _), fit in zip(splits, fits):
                tk = side + total_key[3:]
                pk = side + plays_key[3:]
                plays, total = r[col[pk]], r[col[tk]]
                if not fit or not plays:
                    adj.append(None)
                    continue
                avg, off, defn, hfa = fit
                opp, site = r[col["opp"]], r[col["site"]]
                if side == "off":   # my offense vs their defense; I'm at `site`
                    rate = total / plays - defn.get(opp, 0) - hfa * site
                else:               # their offense vs my defense; they're at -site
                    rate = total / plays - off.get(opp, 0) - hfa * -site
                adj.append(round(rate * plays, 2))
        r.extend(adj)
    return fits


def is_garbage_time(play):
    """Bill Connelly's definition: a lead of more than 38 in the 2nd quarter, 28 in the 3rd, 22 in the 4th."""
    try:
        margin = abs(int(play.get("offenseScore") or 0) - int(play.get("defenseScore") or 0))
    except (TypeError, ValueError):
        return False
    return {2: margin > 38, 3: margin > 28, 4: margin > 22}.get(play.get("period"), False)


RUSH_TYPES = {"Rush", "Rushing Touchdown"}
PASS_TYPES = {"Pass Reception", "Pass Incompletion", "Passing Touchdown", "Sack", "Pass Interception Return",
              "Interception", "Interception Return Touchdown", "Pass", "Pass Completion"}
FUMBLE_TYPES = {"Fumble Recovery (Opponent)", "Fumble Recovery (Own)", "Fumble", "Fumble Return Touchdown"}
TURNOVER_TYPES = {"Pass Interception Return", "Interception", "Interception Return Touchdown",
                  "Fumble Recovery (Opponent)", "Fumble Return Touchdown"}
HURRY = re.compile(r"\bhurr|\bqb hit\b|\bpressured\b", re.I)


def play_counts(plays):
    """Counts in PBP_KEYS order for one offense's plays in one game (garbage time excluded).

    Success: at least 50% of the yards to go on 1st down, 70% on 2nd, 100% on 3rd/4th (touchdowns
    always, turnovers never). Standard downs: 1st, 2nd & 7 or less, 3rd/4th & 4 or less; the rest are
    passing downs. Dropbacks are passes, sacks and interceptions. Pressure = sacks plus dropbacks the
    stat crew noted as hurried or hit. Havoc = tackles for loss (incl. sacks), interceptions, fumbles and
    passes broken up.
    """
    c = dict.fromkeys(PBP_KEYS, 0)
    for p in plays:
        ptype = p.get("playType")
        down, dist = p.get("down"), p.get("distance")
        if ptype not in RUSH_TYPES | PASS_TYPES | FUMBLE_TYPES or down not in (1, 2, 3, 4) or dist is None:
            continue
        if is_garbage_time(p):
            continue
        text = (p.get("playText") or "").lower()
        gained = p.get("yardsGained") or 0
        if ptype in TURNOVER_TYPES:
            success = False
        elif "touchdown" in ptype.lower() or (p.get("scoring") and "touchdown" in text):
            success = True
        else:
            need = dist * (0.5 if down == 1 else 0.7 if down == 2 else 1.0)
            success = gained >= need
        c["N"] += 1
        c["Succ"] += success
        early = down <= 2
        c["EarlyN" if early else "LateN"] += 1
        c["EarlyS" if early else "LateS"] += success
        standard = down == 1 or (down == 2 and dist <= 7) or (down >= 3 and dist <= 4)
        c["StdN" if standard else "PdN"] += 1
        c["StdS" if standard else "PdS"] += success
        dropback = ptype in PASS_TYPES
        sack = ptype == "Sack"
        if dropback:
            c["Db"] += 1
            c["Sacks"] += sack
            c["Press"] += sack or bool(HURRY.search(text))
        havoc = (sack or ptype in TURNOVER_TYPES or ptype in FUMBLE_TYPES or "fumble" in text
                 or "broken up" in text or (ptype in RUSH_TYPES and gained < 0))
        c["Havoc"] += havoc
    return [c[k] for k in PBP_KEYS]


def cfbd_get(path, key):
    req = urllib.request.Request(f"{CFBD}{path}", headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                                                            "User-Agent": "sports-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.load(resp)


def match_team(game_id, name, opponent, games_by_id, teams):
    """ESPN team id for a CollegeFootballData team name in a given game, or None."""
    ids = games_by_id.get(str(game_id))
    if not ids:
        return None
    match = [i for i in ids if norm_name(teams[i]["name"]) == norm_name(name)]
    if not match:
        other = [i for i in ids if norm_name(teams[i]["name"]) == norm_name(opponent)]
        match = [i for i in ids if i not in other] if len(other) == 1 else []
    return match[0] if len(match) == 1 else None


def fetch_play_stats(year, weeks, games_by_id, teams):
    """{"gameId|teamId": offense counts} from CollegeFootballData play-by-play, one request per week.

    Weeks whose completed games are all cached aren't downloaded again.
    """
    cache = load(PLAYS_CACHE, {})
    key = os.environ.get("CFBD_API_KEY")
    if not key:
        print("No CFBD_API_KEY set; keeping the saved play-by-play stats.")
        return cache
    wanted = {}
    for w in weeks:
        done = [g["id"] for g in w["games"] if g["state"] == "post" and g["id"] in games_by_id]
        if not done or all(f"{gid}|{tid}" in cache and f"rz:{gid}|{tid}" in cache for gid in done for tid in games_by_id[gid]):
            continue
        stype = "regular" if w["stype"] == 2 else "postseason"
        wanted[(stype, w["week"] if stype == "regular" else 1)] = True
    for stype, week in wanted:
        try:
            plays = cfbd_get(f"/plays?year={year}&week={week}&seasonType={stype}", key)
        except Exception as exc:
            print(f"  plays {stype} week {week}: {exc}", file=sys.stderr)
            continue
        groups = {}
        for p in plays:
            groups.setdefault((str(p.get("gameId")), p.get("offense"), p.get("defense")), []).append(p)
        for (gid, offense, defense), ps in groups.items():
            tid = match_team(gid, offense, defense, games_by_id, teams)
            if tid:
                cache[f"{gid}|{tid}"] = play_counts(ps)
                cache[f"rz:{gid}|{tid}"] = red_zone(ps)
        print(f"Play-by-play {stype} week {week}: {len(plays)} plays")
        time.sleep(0.5)
    PLAYS_CACHE.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True))
    return cache


def red_zone(plays):
    """[touchdowns, trips] for one offense in one game: a trip is a drive that ran a play from inside the
    opponent's 20; it counts as a touchdown if the drive scored a rushing or passing touchdown."""
    drives = {}
    for p in plays:
        if p.get("playType") not in RUSH_TYPES | PASS_TYPES:
            continue
        d = drives.setdefault(p.get("driveId"), [False, False])
        if (p.get("yardsToGoal") or 100) <= 20:
            d[0] = True
        if p.get("playType") in ("Rushing Touchdown", "Passing Touchdown"):
            d[1] = True
    trips = [d for d in drives.values() if d[0]]
    return [sum(1 for d in trips if d[1]), len(trips)]


def norm_name(name):
    """Loose school-name key so ESPN and CollegeFootballData names line up (accents, punctuation)."""
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", name)


def fetch_epa(year, games_by_id, teams):
    """{"gameId|espnTeamId": [EPA_COLS...]} from CollegeFootballData's advanced game stats.

    Passing plays include sacks (so they're dropbacks); QB scrambles can't be told apart from
    designed runs in public play-by-play, so they count as rushes.
    """
    key = os.environ.get("CFBD_API_KEY")
    if not key:
        print("No CFBD_API_KEY set; keeping the saved EPA.")
        return None
    rows = []
    for stype in ("regular", "postseason"):
        req = urllib.request.Request(f"{CFBD}/stats/game/advanced?year={year}&seasonType={stype}&excludeGarbageTime=true",
                                     headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                                              "User-Agent": "sports-tracker/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                rows += json.load(resp)
        except Exception as exc:
            print(f"  EPA ({stype}): {exc}", file=sys.stderr)
            if stype == "regular":
                return None

    def count(block):
        ppa, total = block.get("ppa"), block.get("totalPPA")
        if not ppa or total is None:
            return 0
        return round(total / ppa)

    out = {}
    unmatched = 0
    for r in rows:
        if str(r.get("gameId")) not in games_by_id:
            continue  # not an FBS game we track
        tid = match_team(r.get("gameId"), r.get("team"), r.get("opponent"), games_by_id, teams)
        if not tid:
            unmatched += 1
            continue
        match = [tid]
        o, d = r.get("offense") or {}, r.get("defense") or {}
        vals = []
        for side in (o, d):
            pas, rus = side.get("passingPlays") or {}, side.get("rushingPlays") or {}
            vals += [side.get("plays") or 0, round(side.get("totalPPA") or 0, 2),
                     count(pas), round(pas.get("totalPPA") or 0, 2),
                     count(rus), round(rus.get("totalPPA") or 0, 2)]
        out[f"{r['gameId']}|{match[0]}"] = vals
    print(f"EPA for {len(out)} team-games ({unmatched} couldn't be matched to a team)")
    return out


def made_att(v):
    """'6-10' -> [6, 10]"""
    try:
        a, b = str(v).split("-")
        return [int(a), int(b)]
    except (ValueError, AttributeError):
        return None


def box_numbers(game_id, base=None):
    """({teamId: total yards}, {teamId: [3rd downs converted, attempted]}, {teamId: [red-zone TDs, trips]})
    from the game's ESPN box score. College box scores have no red-zone line (that comes from play-by-play)."""
    summary = get_json(f"{base or BASE}/summary?event={game_id}")
    yards, third, rz = {}, {}, {}
    for t in (summary.get("boxscore") or {}).get("teams", []):
        tid = str(t["team"]["id"])
        stats = {s.get("name"): s.get("displayValue") for s in t.get("statistics", [])}
        labels = {s.get("label"): s.get("displayValue") for s in t.get("statistics", [])}
        try:
            yards[tid] = int(str(stats.get("totalYards", "")).replace(",", ""))
        except ValueError:
            pass
        if made_att(stats.get("thirdDownEff")):
            third[tid] = made_att(stats.get("thirdDownEff"))
        r = made_att(stats.get("redZoneAttempts") or labels.get("Red Zone (Made-Att)"))
        if r:
            rz[tid] = r
    return yards, third, rz


def total_yards(game_id):
    """{teamId: total yards} from the game's box score."""
    return box_numbers(game_id)[0]


def main():
    first = get_json(f"{BASE}/scoreboard?groups={FBS}&limit=400")
    weeks = calendar_weeks(first)
    now = datetime.now(timezone.utc).isoformat()
    cur_type = first.get("season", {}).get("type", 2)
    cur_week = (first.get("week") or {}).get("number")
    current = next((i for i, w in enumerate(weeks) if w["stype"] == cur_type and w["week"] == cur_week), None)
    if current is None:
        current = next((i for i, w in enumerate(weeks) if (w["end"] or "") >= now), len(weeks) - 1)

    confs = {}
    for c in get_json(f"{BASE}/scoreboard/conferences").get("conferences", []):
        if str(c.get("parentGroupId")) == str(FBS):
            confs[str(c["groupId"])] = {"name": c.get("name"), "short": c.get("shortName")}

    box_cache = load(BOX_CACHE, {})
    third_cache = load(THIRD_CACHE, {})
    line_cache = load(LINE_CACHE, {})
    teams, results, out_weeks = {}, [], []
    fetched = 0

    for wi, w in enumerate(weeks):
        events = first["events"] if wi == current else \
            get_json(f"{BASE}/scoreboard?groups={FBS}&limit=400&seasontype={w['stype']}&week={w['week']}").get("events", [])
        games = []
        for ev in events:
            if not ev.get("competitions"):
                continue  # ESPN occasionally sends an event without its game details
            comp = ev["competitions"][0]
            st = ev["status"]["type"]
            sides = {}
            for c in comp["competitors"]:
                t = c["team"]
                tid = str(t["id"])
                teams[tid] = {"name": t.get("location") or t.get("displayName", ""), "abbr": t.get("abbreviation", ""),
                              "mascot": t.get("name", ""), "logo": t.get("logo", ""), "conf": str(t.get("conferenceId"))}
                rank = (c.get("curatedRank") or {}).get("current")
                sides[c["homeAway"]] = {"id": tid, "score": c.get("score"), "record": overall_record(c),
                                        "rank": rank if rank and rank <= 25 else None}  # ranking at the time of the game
            line = betting_line(comp)
            if line and st.get("state") == "pre":
                line_cache[ev["id"]] = line  # remember it; ESPN drops lines once the game starts
            line = line or line_cache.get(ev["id"])
            games.append({
                "id": ev["id"], "start": ev["date"], "state": st.get("state"), "detail": st.get("shortDetail", ""),
                "away": sides.get("away"), "home": sides.get("home"),
                "neutral": comp.get("neutralSite", False), "confGame": comp.get("conferenceCompetition", False),
                "venue": (comp.get("venue") or {}).get("fullName", ""),
                "tv": comp.get("broadcast") or ", ".join(n for b in comp.get("broadcasts", []) for n in b.get("names", [])),
                "note": ((comp.get("notes") or [{}])[0] or {}).get("headline", ""),
                "line": line,
            })
            # Completed games: points and yards for both teams
            if st.get("state") == "post" and sides.get("away") and sides.get("home"):
                if ev["id"] not in box_cache or ev["id"] not in third_cache:
                    try:
                        y, third, _ = box_numbers(ev["id"])
                        box_cache[ev["id"]], third_cache[ev["id"]] = y, third
                    except Exception as exc:
                        print(f"  box score {ev['id']}: {exc}", file=sys.stderr)
                        box_cache.setdefault(ev["id"], {})
                    fetched += 1
                    time.sleep(0.1)
                yards = box_cache[ev["id"]]
                a, h = sides["away"], sides["home"]
                try:
                    ap, hp = int(a["score"]), int(h["score"])
                except (TypeError, ValueError):
                    continue
                conf_game = 1 if comp.get("conferenceCompetition") else 0
                neutral = bool(comp.get("neutralSite"))
                for me, opp, pf, pa, site in ((a, h, ap, hp, -1), (h, a, hp, ap, 1)):
                    results.append([ev["id"], wi, ev["date"], me["id"], opp["id"], conf_game, pf, pa,
                                    yards.get(me["id"]), yards.get(opp["id"]), 0 if neutral else site])
        games.sort(key=lambda g: (g["start"], g["id"]))
        out_weeks.append({**{k: w[k] for k in ("label", "dates", "stype", "week", "start", "end")}, "games": games})
        print(f"{w['label']}: {len(games)} games")
        time.sleep(0.2)

    backfill_lines(first.get("season", {}).get("year"), out_weeks, teams, line_cache)

    # Rankings: College Football Playoff when it's out, otherwise AP Top 25.
    polls = get_json(f"{BASE}/rankings").get("rankings", [])
    poll = next((p for p in polls if "playoff" in (p.get("name", "") + p.get("shortName", "")).lower()), None) \
        or next((p for p in polls if p.get("name", "").startswith("AP")), None)
    ranks, poll_info = {}, None
    if poll:
        ranks = {str(r["team"]["id"]): r["current"] for r in poll.get("ranks", [])}
        poll_info = {"name": poll.get("name"), "week": (poll.get("occurrence") or {}).get("displayValue", "")}

    results.sort(key=lambda r: (r[2], r[0], r[3]))

    games_by_id = {}
    for r in results:
        games_by_id.setdefault(r[0], []).append(r[3])
    epa = fetch_epa(first.get("season", {}).get("year"), games_by_id, teams)
    if epa is None:
        epa = load(EPA_OUT, {})
    else:
        EPA_OUT.write_text(json.dumps(epa, separators=(",", ":"), sort_keys=True))
    for r in results:
        r.extend(epa.get(f"{r[0]}|{r[3]}") or [None] * len(EPA_COLS))
    fbs = {t for t, v in teams.items() if v["conf"] in confs}
    col = {k: i for i, k in enumerate(RESULT_COLS + EPA_COLS)}
    fits = adjust_for_opponents(results, fbs, col, [("offEpa", "offPlays", 250), ("offPassEpa", "offPassPlays", 120),
                                                    ("offRushEpa", "offRushPlays", 120)])
    if fits[0]:
        print(f"Opponent adjustment: league average {fits[0][0]:+.3f} EPA/play, home field {fits[0][3]:+.3f}")

    # Success, sack, pressure and havoc rates from play-by-play; defense = the opponent's offense that game
    pbp = fetch_play_stats(first.get("season", {}).get("year"), out_weeks, games_by_id, teams)
    for r in results:
        mine, theirs = pbp.get(f"{r[0]}|{r[3]}"), pbp.get(f"{r[0]}|{r[4]}")
        r.extend((mine or [None] * len(PBP_KEYS)) + (theirs or [None] * len(PBP_KEYS)))
    col = {k: i for i, k in enumerate(RESULT_COLS + EPA_COLS + ADJ_COLS + PBP_COLS)}
    adjust_for_opponents(results, fbs, col, [("off" + e, "off" + n, sh) for e, n, sh in RATE_SPLITS])
    print(f"Play-by-play stats for {sum(1 for r in results if r[col['offN']] is not None)} of {len(results)} team-games")

    # Quarterbacks for the Players view
    import cfb_qb
    results_meta = {(r[0], r[3]): (r[1], r[4], r[5]) for r in results}
    try:
        cfb_qb.build(first.get("season", {}).get("year"), out_weeks, games_by_id, teams, results_meta,
                     os.environ.get("CFBD_API_KEY"), QB_CACHE, QB_OUT, cfbd_get, is_garbage_time, match_team, load)
    except Exception as exc:
        print(f"  quarterback stats failed: {exc}", file=sys.stderr)
    try:
        cfb_qb.build_careers(first.get("season", {}).get("year"), os.environ.get("CFBD_API_KEY"), QB_OUT,
                             DIR / "qbcareercache.json", DIR / "qbcareers.json", get_json, cfbd_get, load)
    except Exception as exc:
        print(f"  quarterback careers failed: {exc}", file=sys.stderr)

    # Running backs for the Players view and RB pages
    import cfb_rb
    try:
        cfb_rb.build(first.get("season", {}).get("year"), out_weeks, games_by_id, teams, results_meta,
                     os.environ.get("CFBD_API_KEY"), DIR / "rbgames.json", DIR / "rbs.json",
                     cfbd_get, is_garbage_time, match_team, load, norm_name)
        cfb_rb.build_careers(first.get("season", {}).get("year"), os.environ.get("CFBD_API_KEY"), DIR / "rbs.json",
                             DIR / "rbcareercache.json", DIR / "rbcareers.json", get_json, cfbd_get, load)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"  running back stats failed: {exc}", file=sys.stderr)

    # Wide receivers and tight ends (after the backs: they share the backs' roster and box scores)
    import cfb_wr
    try:
        cfb_wr.build(first.get("season", {}).get("year"), out_weeks, games_by_id, teams, results_meta,
                     os.environ.get("CFBD_API_KEY"), DIR / "rbgames.json", DIR / "wrgames.json", DIR / "wrs.json",
                     cfbd_get, is_garbage_time, match_team, load)
        cfb_rb.build_careers(first.get("season", {}).get("year"), os.environ.get("CFBD_API_KEY"), DIR / "wrs.json",
                             DIR / "wrcareercache.json", DIR / "wrcareers.json", get_json, cfbd_get, load, kind="wr")
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"  receiver stats failed: {exc}", file=sys.stderr)

    # Defensive linemen and edge rushers
    import fb_def
    try:
        fb_def.cfb_build(first.get("season", {}).get("year"), out_weeks, games_by_id, teams, results_meta,
                         os.environ.get("CFBD_API_KEY"), DIR / "rbgames.json", DIR / "defgames.json", DIR / "dls.json",
                         cfbd_get, match_team, load)
        cfb_rb.build_careers(first.get("season", {}).get("year"), os.environ.get("CFBD_API_KEY"), DIR / "dls.json",
                             DIR / "dlcareercache.json", DIR / "dlcareers.json", get_json, cfbd_get, load, kind="dl")
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"  defensive stats failed: {exc}", file=sys.stderr)
    for r in results:
        gid, me, opp = r[0], r[3], r[4]
        t3 = third_cache.get(gid) or {}
        rz_me, rz_opp = pbp.get(f"rz:{gid}|{me}"), pbp.get(f"rz:{gid}|{opp}")
        r.extend((t3.get(me) or [None, None]) + (rz_me or [None, None]) + (t3.get(opp) or [None, None]) + (rz_opp or [None, None]))
    DIR.mkdir(parents=True, exist_ok=True)
    THIRD_CACHE.write_text(json.dumps(third_cache, separators=(",", ":"), sort_keys=True))
    BOX_CACHE.write_text(json.dumps(box_cache, separators=(",", ":"), sort_keys=True))
    LINE_CACHE.write_text(json.dumps(line_cache, separators=(",", ":"), sort_keys=True))
    OUT.write_text(json.dumps({
        "season": first.get("season", {}).get("year"),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "current": current,
        "poll": poll_info,
        "ranks": ranks,
        "conferences": confs,
        "teams": dict(sorted(teams.items())),
        "weeks": out_weeks,
        "resultCols": RESULT_COLS + EPA_COLS + ADJ_COLS + PBP_COLS + ADJ_RATE_COLS + SIT_COLS,
        "results": results,
    }, separators=(",", ":")))
    print(f"Saved {OUT}: {len(out_weeks)} weeks, {len(results) // 2} completed games "
          f"({fetched} box scores downloaded this run), poll: {poll_info}")


if __name__ == "__main__":
    main()
