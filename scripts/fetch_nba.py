#!/usr/bin/env python3
"""Fetch NBA team box scores for the current regular season from ESPN's public API.

Writes data/nba/games.json: one record per team per completed game.
Runs incrementally: games already in the file are not downloaded again.

Also writes data/nba/schedule.json: regular-season games for today and the
next several days (US Eastern dates), for the Matchups view, and
data/nba/players.json: one row per player per game played, for the Players view.

Usage:
    python3 scripts/fetch_nba.py
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
OUT = Path(__file__).resolve().parent.parent / "data" / "nba" / "games.json"
SCHEDULE_OUT = OUT.parent / "schedule.json"
PLAYERS_OUT = OUT.parent / "players.json"
PLAYER_COLS = ["gameId", "team", "player", "min", "pts", "reb", "oreb", "ast", "stl", "blk", "tov", "fgm", "fga", "tpm", "tpa"]
REGULAR_SEASON = 2
SCHEDULE_DAYS = 7
REQUIRED_FIELDS = ("pts", "stl", "blk")
EASTERN = ZoneInfo("America/New_York")


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
        "stl": int(stats["steals"]),
        "blk": int(stats["blocks"]),
    }


def main():
    existing = json.loads(OUT.read_text()) if OUT.exists() else {"teams": {}, "games": []}
    teams = existing["teams"]
    # Games saved before a field was added are dropped and downloaded again.
    games = [g for g in existing["games"] if all(k in g for k in REQUIRED_FIELDS)]
    have = {g["gameId"] for g in games}

    pdata = json.loads(PLAYERS_OUT.read_text()) if PLAYERS_OUT.exists() else {}
    players = pdata.get("players", {})
    player_rows = pdata.get("rows", []) if pdata.get("cols") == PLAYER_COLS else []
    have_players = {r[0] for r in player_rows}

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
            if ev["status"]["type"]["name"] != "STATUS_FINAL":
                continue
            need_team = ev["id"] not in have
            need_players = ev["id"] not in have_players
            if not need_team and not need_players:
                continue
            summary = get_json(f"{BASE}/summary?event={ev['id']}")
            box = summary["boxscore"]["teams"]
            if len(box) != 2:
                continue
            if need_players:
                for row in parse_players(summary, players):
                    player_rows.append([ev["id"]] + row)
                have_players.add(ev["id"])
            if not need_team:
                time.sleep(0.2)
                continue
            rows = [parse_team(t) for t in box]
            scores = {
                c["team"]["abbreviation"]: int(c["score"])
                for c in summary["header"]["competitions"][0]["competitors"]
            }
            for row in rows:
                row["pts"] = scores[row["team"]]
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

    game_order = {g["gameId"]: i for i, g in enumerate(games[::2])}
    player_rows.sort(key=lambda r: (game_order.get(r[0], 1e9), r[1], r[2]))
    PLAYERS_OUT.write_text(
        "{" + f'"cols":{json.dumps(PLAYER_COLS)},'
        + f'"players":{json.dumps(dict(sorted(players.items())), separators=(",", ":"))},'
        + '"rows":[\n' + ",\n".join(json.dumps(r, separators=(",", ":")) for r in player_rows) + "\n]}"
    )
    print(f"Saved {len(player_rows)} player game lines for {len(players)} players in {PLAYERS_OUT}")

    write_schedule(season["displayName"])


def parse_players(summary, players):
    """Return [team, playerId, min, pts, reb, oreb, ast, stl, blk, tov, fgm, fga, tpm, tpa] for everyone who played.

    Also records each player's name, headshot and most recent team in `players`.
    """
    rows = []
    for team_block in summary["boxscore"].get("players", []):
        team = team_block["team"]["abbreviation"]
        for table in team_block.get("statistics", []):
            keys = table.get("keys") or table.get("names") or []
            labels = table.get("labels") or keys
            idx = {name: i for i, name in enumerate(labels)}
            for a in table.get("athletes", []):
                stats = a.get("stats") or []
                if a.get("didNotPlay") or not stats:
                    continue

                def num(label):
                    try:
                        return int(stats[idx[label]])
                    except (KeyError, ValueError, IndexError):
                        return 0

                def made_att(label):  # "7-15" -> (7, 15)
                    try:
                        made, att = stats[idx[label]].split("-")
                        return int(made), int(att)
                    except (KeyError, ValueError, IndexError):
                        return 0, 0

                ath = a["athlete"]
                pid = str(ath["id"])
                players[pid] = {
                    "name": ath.get("displayName", ""),
                    "short": ath.get("shortName", ath.get("displayName", "")),
                    "team": team,
                    "pos": (ath.get("position") or {}).get("abbreviation", ""),
                }
                rows.append([team, pid, num("MIN"), num("PTS"), num("REB"), num("OREB"), num("AST"),
                             num("STL"), num("BLK"), num("TO"), *made_att("FG"), *made_att("3PT")])
    return rows


def write_schedule(season_name):
    """Save upcoming regular-season games, grouped by US Eastern date."""
    today = datetime.now(EASTERN).date()
    days = []
    for offset in range(SCHEDULE_DAYS):
        day = today + timedelta(days=offset)
        sb = get_json(f"{BASE}/scoreboard?dates={day.strftime('%Y%m%d')}")
        matchups = []
        for ev in sb.get("events", []):
            if ev["season"]["type"] != REGULAR_SEASON:
                continue
            comp = ev["competitions"][0]
            sides = {c["homeAway"]: c["team"]["abbreviation"] for c in comp["competitors"]}
            records = {c["homeAway"]: overall_record(c) for c in comp["competitors"]}
            matchups.append({
                "gameId": ev["id"],
                "start": ev["date"],
                "away": sides.get("away"),
                "home": sides.get("home"),
                "awayRecord": records.get("away"),
                "homeRecord": records.get("home"),
                "state": ev["status"]["type"]["state"],  # pre, in, post
                "detail": ev["status"]["type"].get("shortDetail", ""),
                "line": betting_line(comp),
            })
        matchups.sort(key=lambda m: (m["start"], m["gameId"]))
        days.append({"date": day.isoformat(), "games": matchups})
        time.sleep(0.2)
    SCHEDULE_OUT.write_text(json.dumps({"season": season_name, "days": days}, separators=(",", ":")))
    print(f"Saved schedule for {len(days)} days in {SCHEDULE_OUT}")


def overall_record(competitor):
    """The team's season record, like "10-7"."""
    recs = competitor.get("records") or []
    for r in recs:
        if r.get("type") == "total" or r.get("name", "").lower() == "overall":
            return r.get("summary")
    return recs[0].get("summary") if recs else None


def betting_line(comp):
    """Spread, over/under and moneylines from the first sportsbook ESPN lists, or None if not posted."""
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
        "spread": o.get("details"),  # e.g. "MEM -2.5" or "EVEN"
        "total": o.get("overUnder"),
        "mlAway": moneyline("away"),
        "mlHome": moneyline("home"),
    }
    return line if any(line[k] for k in ("spread", "total", "mlAway", "mlHome")) else None


if __name__ == "__main__":
    main()
