#!/usr/bin/env python3
"""Fetch college football (FBS) matchups, rankings and betting lines from ESPN's public API.

Writes data/cfb/matchups.json with:
  - this week's and next week's FBS games (kickoff, TV, venue, records, conference, line)
  - the current ranking (College Football Playoff when published, otherwise AP Top 25)
  - FBS conferences
  - each FBS team's points scored and allowed per game this season, with national ranks

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
OUT = Path(__file__).resolve().parent.parent / "data" / "cfb" / "matchups.json"
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
    """Every week of the season in order: (seasontype, week value, label, start, end)."""
    weeks = []
    for part in scoreboard["leagues"][0].get("calendar", []):
        stype = part.get("value")
        if str(stype) not in ("2", "3"):  # regular season and postseason
            continue
        for entry in part.get("entries", []):
            weeks.append((int(stype), int(entry["value"]), entry.get("label") or entry.get("alternateLabel"),
                          entry.get("startDate"), entry.get("endDate")))
    return weeks


def week_games(stype, week):
    return get_json(f"{BASE}/scoreboard?groups={FBS}&limit=400&seasontype={stype}&week={week}").get("events", [])


def team_side(c):
    t = c["team"]
    return {
        "id": t["id"],
        "abbr": t.get("abbreviation", ""),
        "name": t.get("location") or t.get("displayName", ""),
        "mascot": t.get("name", ""),
        "logo": t.get("logo", ""),
        "conf": t.get("conferenceId"),
        "record": overall_record(c),
        "score": c.get("score"),
    }


def main():
    current = get_json(f"{BASE}/scoreboard?groups={FBS}&limit=400")
    weeks = calendar_weeks(current)
    now = datetime.now(timezone.utc).isoformat()
    # The week ESPN shows by default is the current one; the next calendar entry is "next week".
    cur_type = current.get("season", {}).get("type", 2)
    cur_week = (current.get("week") or {}).get("number")
    idx = next((i for i, w in enumerate(weeks) if w[0] == cur_type and w[1] == cur_week), None)
    if idx is None:
        idx = next((i for i, w in enumerate(weeks) if (w[4] or "") >= now), len(weeks) - 1)
    upcoming = weeks[idx: idx + 2]

    out_weeks = []
    for stype, week, label, start, end in upcoming:
        events = current["events"] if (stype, week) == (cur_type, cur_week) else week_games(stype, week)
        games = []
        for ev in events:
            comp = ev["competitions"][0]
            sides = {c["homeAway"]: team_side(c) for c in comp["competitors"]}
            st = ev["status"]["type"]
            games.append({
                "id": ev["id"],
                "start": ev["date"],
                "state": st.get("state"),
                "detail": st.get("shortDetail", ""),
                "away": sides.get("away"),
                "home": sides.get("home"),
                "neutral": comp.get("neutralSite", False),
                "confGame": comp.get("conferenceCompetition", False),
                "venue": (comp.get("venue") or {}).get("fullName", ""),
                "tv": comp.get("broadcast") or ", ".join(n for b in comp.get("broadcasts", []) for n in b.get("names", [])),
                "note": ((comp.get("notes") or [{}])[0] or {}).get("headline", ""),
                "line": betting_line(comp),
            })
        games.sort(key=lambda g: (g["start"], g["id"]))
        out_weeks.append({"label": label, "seasontype": stype, "week": week, "start": start, "end": end, "games": games})
        print(f"{label}: {len(games)} games")

    # Rankings: College Football Playoff when it's out, otherwise AP Top 25.
    polls = get_json(f"{BASE}/rankings").get("rankings", [])
    poll = next((p for p in polls if "playoff" in (p.get("name", "") + p.get("shortName", "")).lower()), None) \
        or next((p for p in polls if p.get("name", "").startswith("AP")), None)
    ranks = {}
    poll_info = None
    if poll:
        for r in poll.get("ranks", []):
            ranks[r["team"]["id"]] = r["current"]
        poll_info = {"name": poll.get("name"), "week": (poll.get("occurrence") or {}).get("displayValue", "")}

    confs = {}
    for c in get_json(f"{BASE}/scoreboard/conferences").get("conferences", []):
        if str(c.get("parentGroupId")) == str(FBS):
            confs[str(c["groupId"])] = {"name": c.get("name"), "short": c.get("shortName")}

    # Season scoring for FBS teams from every completed week so far.
    totals = {}
    team_conf = {}
    for stype, week, label, start, end in weeks:
        if (start or "") > now:
            break
        for ev in week_games(stype, week):
            comp = ev["competitions"][0]
            if ev["status"]["type"].get("state") != "post":
                continue
            cs = comp["competitors"]
            if len(cs) != 2:
                continue
            for i, c in enumerate(cs):
                team_conf[c["team"]["id"]] = str(c["team"].get("conferenceId"))
                try:
                    pf, pa = int(c["score"]), int(cs[1 - i]["score"])
                except (KeyError, ValueError, TypeError):
                    continue
                t = totals.setdefault(c["team"]["id"], {"gp": 0, "pf": 0, "pa": 0})
                t["gp"] += 1
                t["pf"] += pf
                t["pa"] += pa
        time.sleep(0.2)
    stats = {tid: {"gp": t["gp"], "ppg": round(t["pf"] / t["gp"], 1), "oppg": round(t["pa"] / t["gp"], 1)}
             for tid, t in totals.items() if t["gp"]}
    # National ranks among FBS teams (those in an FBS conference). Ties share the better rank.
    fbs_teams = [tid for tid in stats if team_conf.get(tid) in confs]
    for key, higher_is_better in (("ppg", True), ("oppg", False)):
        for tid in fbs_teams:
            v = stats[tid][key]
            stats[tid][key + "Rank"] = 1 + sum(
                1 for o in fbs_teams if (stats[o][key] > v if higher_is_better else stats[o][key] < v))
    for tid in stats:
        stats[tid]["fbs"] = tid in fbs_teams

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "season": current.get("season", {}).get("year"),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "poll": poll_info,
        "ranks": ranks,
        "conferences": confs,
        "fbsTeams": len(fbs_teams),
        "weeks": out_weeks,
        "teamStats": {tid: stats[tid] for tid in sorted(stats) if stats[tid]["fbs"]},
    }, separators=(",", ":")))
    print(f"Saved {OUT} ({len(fbs_teams)} FBS teams with stats, poll: {poll_info})")


if __name__ == "__main__":
    main()
