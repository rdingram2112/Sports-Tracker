#!/usr/bin/env python3
"""Fetch the college football (FBS) season from ESPN's public API.

Writes data/cfb/season.json with:
  - every week of the season (regular season and postseason) with each FBS game:
    kickoff, status and score, TV, venue, records and the betting line
  - the current ranking (College Football Playoff when published, otherwise AP Top 25)
  - FBS conferences and teams
  - one row per team per completed game (points and total yards for and against),
    which the page uses to work out per-game stats for any filter

Also keeps two caches so each game is only downloaded once:
  data/cfb/boxscores.json  total yards per team for completed games
  data/cfb/lines.json      the last betting line seen before each game (ESPN drops lines after kickoff)

Usage:
    python3 scripts/fetch_cfb.py
"""
import json
import sys
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
                for me, opp, pf, pa in ((a, h, ap, hp), (h, a, hp, ap)):
                    results.append([ev["id"], wi, ev["date"], me["id"], opp["id"], conf_game, pf, pa,
                                    yards.get(me["id"]), yards.get(opp["id"])])
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
        "resultCols": ["gameId", "week", "date", "team", "opp", "confGame", "pf", "pa", "yds", "oppYds"],
        "results": results,
    }, separators=(",", ":")))
    print(f"Saved {OUT}: {len(out_weeks)} weeks, {len(results) // 2} completed games "
          f"({fetched} box scores downloaded this run), poll: {poll_info}")


if __name__ == "__main__":
    main()
