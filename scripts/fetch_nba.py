#!/usr/bin/env python3
"""Fetch NBA team box scores for the current regular season from ESPN's public API.

Writes data/nba/games.json: one record per team per completed game.
Runs incrementally: games already in the file are not downloaded again.

Usage:
    python3 scripts/fetch_nba.py
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
OUT = Path(__file__).resolve().parent.parent / "data" / "nba" / "games.json"
REGULAR_SEASON = 2


def get_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sports-tracker/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception as exc:  # network hiccup: back off and retry
            if attempt == retries - 1:
                raise
            print(f"  retry {url}: {exc}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))


def split_made_att(value):
    made, att = value.split("-")
    return int(made), int(att)


def parse_team(box_team):
    stats = {s["name"]: s["displayValue"] for s in box_team["statistics"]}
    fgm, fga = split_made_att(stats["fieldGoalsMade-fieldGoalsAttempted"])
    tpm, tpa = split_made_att(stats["threePointFieldGoalsMade-threePointFieldGoalsAttempted"])
    ftm, fta = split_made_att(stats["freeThrowsMade-freeThrowsAttempted"])
    tov = stats.get("totalTurnovers", stats.get("turnovers"))
    return {
        "team": box_team["team"]["abbreviation"],
        "fgm": fgm, "fga": fga,
        "tpm": tpm, "tpa": tpa,
        "ftm": ftm, "fta": fta,
        "oreb": int(stats["offensiveRebounds"]),
        "tov": int(tov),
    }


def main():
    existing = json.loads(OUT.read_text()) if OUT.exists() else {"teams": {}, "games": []}
    have = {g["gameId"] for g in existing["games"]}
    teams = existing["teams"]
    games = existing["games"]

    league = get_json(f"{BASE}/scoreboard")["leagues"][0]
    season = league["season"]
    today = datetime.now(timezone.utc).date()
    days = sorted({c[:10] for c in league.get("calendar", [])})
    days = [d for d in days if d <= today.isoformat()]
    # The calendar can lag a day; also check the last few days explicitly.
    for back in range(3):
        d = (today - timedelta(days=back)).isoformat()
        if d not in days and d >= season["startDate"][:10]:
            days.append(d)

    new = 0
    for day in sorted(set(days)):
        sb = get_json(f"{BASE}/scoreboard?dates={day.replace('-', '')}")
        for ev in sb.get("events", []):
            if ev["season"]["type"] != REGULAR_SEASON:
                continue
            if ev["status"]["type"]["name"] != "STATUS_FINAL" or ev["id"] in have:
                continue
            summary = get_json(f"{BASE}/summary?event={ev['id']}")
            box = summary["boxscore"]["teams"]
            if len(box) != 2:
                continue
            rows = [parse_team(t) for t in box]
            for t in box:
                info = t["team"]
                teams[info["abbreviation"]] = {
                    "name": info["displayName"],
                    "short": info.get("shortDisplayName", info["displayName"]),
                    "logo": info.get("logo", ""),
                }
            for i, row in enumerate(rows):
                row["opp"] = rows[1 - i]["team"]
                row["home"] = box[i].get("homeAway") == "home"
                row["gameId"] = ev["id"]
                row["date"] = ev["date"]
                games.append(row)
            have.add(ev["id"])
            new += 1
            time.sleep(0.2)

    games.sort(key=lambda g: (g["date"], g["gameId"]))
    out = {
        "season": season["displayName"],
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "teams": dict(sorted(teams.items())),
        "games": games,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    print(f"Added {new} games; {len(games) // 2} total in {OUT}")


if __name__ == "__main__":
    main()
