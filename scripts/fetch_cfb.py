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


def adjust_for_opponents(results, fbs):
    """Add opponent-adjusted EPA to each team-game row (ADJ_COLS), in place.

    A game's adjusted offense = its EPA/play minus how much its opponent's defense usually gives up
    (relative to average) minus home field; adjusted defense works the same way against the opponent's
    offense. Opponent strength comes from every game so far; the page then picks which games count.
    Ratings are measured against the average FBS team, so an adjusted number reads as
    "what this team did, against an average FBS opponent".
    """
    col = {k: i for i, k in enumerate(RESULT_COLS + EPA_COLS)}
    splits = [("offEpa", "offPlays", 250), ("offPassEpa", "offPassPlays", 120), ("offRushEpa", "offRushPlays", 120)]
    fits = {}
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
        fits[total_key] = fit
    for r in results:
        adj = []
        for side in ("off", "def"):
            for (total_key, plays_key, _) in splits:
                fit = fits[total_key]
                tk = total_key.replace("off", side, 1)
                pk = plays_key.replace("off", side, 1)
                plays, total = r[col[pk]], r[col[tk]]
                if not fit or not plays:
                    adj.append(None)
                    continue
                avg, off, defn, hfa = fit
                me, opp, site = r[col["team"]], r[col["opp"]], r[col["site"]]
                if side == "off":   # my offense vs their defense; I'm at `site`
                    per_play = total / plays - defn.get(opp, 0) - hfa * site
                else:               # their offense vs my defense; they're at -site
                    per_play = total / plays - off.get(opp, 0) - hfa * -site
                adj.append(round(per_play * plays, 2))
        r.extend(adj)
    a = fits.get("offEpa")
    if a:
        print(f"Opponent adjustment: league average {a[0]:+.3f} EPA/play, home field {a[3]:+.3f}")


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
        ids = games_by_id.get(str(r.get("gameId")))
        if not ids:
            continue  # not an FBS game we track
        name = norm_name(r.get("team"))
        match = [i for i in ids if norm_name(teams[i]["name"]) == name]
        if not match:
            # Fall back to the opponent's name to pick the other side
            opp = norm_name(r.get("opponent"))
            other = [i for i in ids if norm_name(teams[i]["name"]) == opp]
            match = [i for i in ids if i not in other] if len(other) == 1 else []
        if len(match) != 1:
            unmatched += 1
            continue
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


def total_yards(game_id):
    """{teamId: total yards} from the game's box score."""
    summary = get_json(f"{BASE}/summary?event={game_id}")
    out = {}
    for t in (summary.get("boxscore") or {}).get("teams", []):
        stats = {s.get("name"): s.get("displayValue") for s in t.get("statistics", [])}
        try:
            out[str(t["team"]["id"])] = int(str(stats.get("totalYards", "")).replace(",", ""))
        except ValueError:
            pass
    return out


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
    line_cache = load(LINE_CACHE, {})
    teams, results, out_weeks = {}, [], []
    fetched = 0

    for wi, w in enumerate(weeks):
        events = first["events"] if wi == current else \
            get_json(f"{BASE}/scoreboard?groups={FBS}&limit=400&seasontype={w['stype']}&week={w['week']}").get("events", [])
        games = []
        for ev in events:
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
                if ev["id"] not in box_cache:
                    try:
                        box_cache[ev["id"]] = total_yards(ev["id"])
                    except Exception as exc:
                        print(f"  box score {ev['id']}: {exc}", file=sys.stderr)
                        box_cache[ev["id"]] = {}
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
    adjust_for_opponents(results, {t for t, v in teams.items() if v["conf"] in confs})
    DIR.mkdir(parents=True, exist_ok=True)
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
        "resultCols": RESULT_COLS + EPA_COLS + ADJ_COLS,
        "results": results,
    }, separators=(",", ":")))
    print(f"Saved {OUT}: {len(out_weeks)} weeks, {len(results) // 2} completed games "
          f"({fetched} box scores downloaded this run), poll: {poll_info}")


if __name__ == "__main__":
    main()
