#!/usr/bin/env python3
"""Fetch NBA team box scores for the current regular season from ESPN's public API.

Writes data/nba/games.json: one record per team per completed game.
Runs incrementally: games already in the file are not downloaded again.

Also writes data/nba/schedule.json: regular-season games for today and the
next several days (US Eastern dates), for the Matchups view, and
data/nba/players.json: one row per player per game played, for the Players view.

For player pages it also keeps:
  data/nba/shots/YYYY-MM.json  every field goal attempt's court location, one file per month
  data/nba/seasons/YYYY.json   league-wide player totals for each season since 2003-04
                               (past seasons are downloaded once; the current one daily)
  data/nba/bios.json           player bios (birth date, college, draft...), fetched once per player
  data/nba/careers.json        each current player's seasons with position percentiles

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
PLAYER_COLS = ["gameId", "team", "player", "min", "pts", "reb", "oreb", "ast", "stl", "blk", "tov", "fgm", "fga", "tpm", "tpa", "ftm", "fta"]
SHOTS_DIR = OUT.parent / "shots"
SHOT_COLS = ["gameId", "player", "x", "y", "made", "pts"]
SEASONS_DIR = OUT.parent / "seasons"
FIRST_SEASON = 2004  # ESPN's year for 2003-04
SEASON_COLS = ["id", "name", "pos", "team", "gp", "min", "pts", "reb", "ast", "stl", "blk", "tov",
               "fgm", "fga", "tpm", "tpa", "ftm", "fta"]
BIOS_OUT = OUT.parent / "bios.json"
BIO_REFRESH_DAYS = 60
BIO_MAX_PER_RUN = 600
CAREERS_OUT = OUT.parent / "careers.json"
COMMON = "https://site.api.espn.com/apis/common/v3/sports/basketball/nba"
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

    shots_by_month = load_shots()
    have_shots = {r[0] for rows in shots_by_month.values() for r in rows}

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
            need_shots = ev["id"] not in have_shots
            if not need_team and not need_players and not need_shots:
                continue
            summary = get_json(f"{BASE}/summary?event={ev['id']}")
            box = summary["boxscore"]["teams"]
            if len(box) != 2:
                continue
            if need_players:
                for row in parse_players(summary, players):
                    player_rows.append([ev["id"]] + row)
                have_players.add(ev["id"])
            if need_shots:
                month = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(EASTERN).strftime("%Y-%m")
                shots_by_month.setdefault(month, []).extend(parse_shots(ev["id"], summary))
                have_shots.add(ev["id"])
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

    write_shots(shots_by_month)
    write_schedule(season["displayName"])

    season_year = int(season["year"])
    write_seasons(season_year)
    write_bios(players)
    write_careers(players, season_year)


def load_shots():
    months = {}
    for f in sorted(SHOTS_DIR.glob("*.json")) if SHOTS_DIR.exists() else []:
        if f.name == "index.json":
            continue
        data = json.loads(f.read_text())
        if data.get("cols") == SHOT_COLS:
            months[f.stem] = data["rows"]
    return months


def parse_shots(game_id, summary):
    """[gameId, playerId, x, y, made, pts] for every field goal attempt with a court location.

    ESPN puts the hoop at x=25, y=0, in feet; free throws are left out.
    """
    rows = []
    for play in summary.get("plays") or []:
        if not play.get("shootingPlay") or play.get("pointsAttempted") not in (2, 3):
            continue
        c = play.get("coordinate") or {}
        x, y = c.get("x"), c.get("y")
        parts = play.get("participants") or []
        if x is None or y is None or not parts or not (-2 <= x <= 52 and -6 <= y <= 47):
            continue
        rows.append([game_id, str(parts[0]["athlete"]["id"]), round(x, 1), round(y, 1),
                     1 if play.get("scoringPlay") else 0, play["pointsAttempted"]])
    return rows


def write_shots(shots_by_month):
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    for month, rows in shots_by_month.items():
        path = SHOTS_DIR / f"{month}.json"
        body = ('{"cols":' + json.dumps(SHOT_COLS) + ',"rows":[\n'
                + ",\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n]}")
        if not path.exists() or path.read_text() != body:
            path.write_text(body)
    (SHOTS_DIR / "index.json").write_text(json.dumps({"months": sorted(shots_by_month)}))
    print(f"Saved {sum(len(r) for r in shots_by_month.values())} shots in {SHOTS_DIR}")


def write_seasons(current_year):
    """League-wide player totals per season. Past seasons never change, so they're fetched once."""
    SEASONS_DIR.mkdir(parents=True, exist_ok=True)
    for year in range(FIRST_SEASON, current_year + 1):
        path = SEASONS_DIR / f"{year}.json"
        if path.exists() and year != current_year:
            continue
        data = get_json(f"{COMMON}/statistics/byathlete?isqualified=false&limit=1000&season={year}&seasontype=2")
        cats = {c["name"]: c.get("names", []) for c in data.get("categories", [])}
        rows = []
        for entry in data.get("athletes", []):
            ath = entry["athlete"]
            vals = {}
            for c in entry.get("categories", []):
                names = cats.get(c["name"], [])
                for name, v in zip(names, c.get("totals") or c.get("values") or []):
                    vals.setdefault(name, v)  # first occurrence wins (names repeat for avg/total)

            def num(name):
                try:
                    return float(str(vals.get(name, 0)).replace(",", ""))
                except ValueError:
                    return 0.0

            rows.append([str(ath["id"]), ath.get("displayName", ""), (ath.get("position") or {}).get("abbreviation", ""),
                         ath.get("teamShortName", ""), int(num("gamesPlayed")), round(num("minutes")),
                         round(num("points")), round(num("rebounds")), round(num("assists")), round(num("steals")),
                         round(num("blocks")), round(num("turnovers")), round(num("fieldGoalsMade")),
                         round(num("fieldGoalsAttempted")), round(num("threePointFieldGoalsMade")),
                         round(num("threePointFieldGoalsAttempted")), round(num("freeThrowsMade")),
                         round(num("freeThrowsAttempted"))])
        label = (data.get("requestedSeason") or {}).get("displayName") or f"{year - 1}-{str(year)[2:]}"
        path.write_text(json.dumps({"season": label, "cols": SEASON_COLS, "rows": rows}, separators=(",", ":")))
        print(f"Saved {len(rows)} players for {label}")
        time.sleep(0.3)


def write_bios(players):
    """Bio for every player in this season's data; each is fetched once and refreshed every couple of months."""
    bios = json.loads(BIOS_OUT.read_text()) if BIOS_OUT.exists() else {}
    today = datetime.now(timezone.utc).date()
    due = [pid for pid in players
           if pid not in bios or (today - datetime.fromisoformat(bios[pid].get("fetched", "2000-01-01")).date()).days > BIO_REFRESH_DAYS]
    for pid in due[:BIO_MAX_PER_RUN]:
        try:
            a = get_json(f"{COMMON}/athletes/{pid}").get("athlete", {})
        except Exception as exc:
            print(f"  bio {pid}: {exc}", file=sys.stderr)
            continue
        dob = a.get("displayDOB") or ""  # day/month/year
        try:
            d, m, y = (int(p) for p in dob.split("/"))
            dob = f"{y:04d}-{m:02d}-{d:02d}"
        except ValueError:
            dob = ""
        bios[pid] = {
            "name": a.get("displayName", ""),
            "dob": dob,
            "college": (a.get("college") or {}).get("name", ""),
            "birthPlace": a.get("displayBirthPlace", ""),
            "height": a.get("displayHeight", ""),
            "weight": a.get("displayWeight", ""),
            "draft": a.get("displayDraft", ""),
            "debut": a.get("debutYear"),
            "jersey": a.get("displayJersey", ""),
            "position": (a.get("position") or {}).get("displayName", ""),
            "fetched": today.isoformat(),
        }
        time.sleep(0.15)
    BIOS_OUT.write_text(json.dumps(dict(sorted(bios.items())), separators=(",", ":")))
    print(f"Saved {len(bios)} bios ({min(len(due), BIO_MAX_PER_RUN)} fetched this run)")


# Stats shown on player pages, with how each is worked out from season totals.
# Shooting stats need some volume (per game) to be compared, so tiny samples don't skew percentiles.
CAREER_STATS = {
    "min": (lambda t: t["min"] / t["gp"], None),
    "pts": (lambda t: t["pts"] / t["gp"], None),
    "reb": (lambda t: t["reb"] / t["gp"], None),
    "ast": (lambda t: t["ast"] / t["gp"], None),
    "stl": (lambda t: t["stl"] / t["gp"], None),
    "blk": (lambda t: t["blk"] / t["gp"], None),
    "tov": (lambda t: t["tov"] / t["gp"], None),
    "efg": (lambda t: (t["fgm"] + 0.5 * t["tpm"]) / t["fga"] if t["fga"] else None, ("fga", 5)),
    "tpr": (lambda t: t["tpa"] / t["fga"] if t["fga"] else None, ("fga", 5)),
    "tp": (lambda t: t["tpm"] / t["tpa"] if t["tpa"] else None, ("tpa", 2)),
    "ftr": (lambda t: t["fta"] / t["fga"] if t["fga"] else None, ("fga", 5)),
    "ftp": (lambda t: t["ftm"] / t["fta"] if t["fta"] else None, ("fta", 2)),
}
MIN_GP_SHARE = 0.25  # players need a quarter of the season's games to be compared
# Older seasons list five positions; compare within three groups.
POSITION_GROUP = {"PG": "G", "SG": "G", "G": "G", "SF": "F", "PF": "F", "F": "F", "GF": "F", "FC": "F", "C": "C"}


def write_careers(players, current_year):
    """Each current player's seasons, with percentiles among players at the same position that season."""
    wanted = set(players)
    careers = {pid: [] for pid in wanted}
    for year in range(FIRST_SEASON, current_year + 1):
        path = SEASONS_DIR / f"{year}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        cols = data["cols"]
        rows = [dict(zip(cols, r)) for r in data["rows"]]
        rows = [r for r in rows if r["gp"] > 0]
        for r in rows:
            r["pos"] = POSITION_GROUP.get(r["pos"], "?")
        if not rows:
            continue
        min_gp = max(1, round(MIN_GP_SHARE * max(r["gp"] for r in rows)))
        values = {}
        for r in rows:
            values[r["id"]] = {k: f(r) for k, (f, _) in CAREER_STATS.items()}

        def qualifies(r, stat):
            vol = CAREER_STATS[stat][1]
            return r["gp"] >= min_gp and values[r["id"]][stat] is not None and (
                vol is None or r[vol[0]] / r["gp"] >= vol[1])

        pools = {}
        for stat in CAREER_STATS:
            for r in rows:
                if qualifies(r, stat):
                    pools.setdefault((stat, r["pos"]), []).append(values[r["id"]][stat])
        for r in rows:
            if r["id"] not in wanted:
                continue
            pct = {}
            for stat in CAREER_STATS:
                pool = pools.get((stat, r["pos"]), [])
                if not qualifies(r, stat) or len(pool) < 2:
                    continue
                v = values[r["id"]][stat]
                below = sum(1 for o in pool if (o > v if stat == "tov" else o < v))
                equal = sum(1 for o in pool if abs(o - v) < 1e-9) - 1
                pct[stat] = round((below + equal / 2) / (len(pool) - 1) * 100)
            careers[r["id"]].append({
                "season": data["season"], "team": r["team"], "pos": r["pos"], "gp": r["gp"],
                "tot": [r[k] for k in ("min", "pts", "reb", "ast", "stl", "blk", "tov",
                                        "fgm", "fga", "tpm", "tpa", "ftm", "fta")],
                "pct": pct,
            })
    CAREERS_OUT.write_text(json.dumps({
        "tot": ["min", "pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga", "tpm", "tpa", "ftm", "fta"],
        "players": {k: v for k, v in sorted(careers.items()) if v},
    }, separators=(",", ":")))
    print(f"Saved careers for {sum(1 for v in careers.values() if v)} players")


def parse_players(summary, players):
    """Return [team, playerId, min, pts, reb, oreb, ast, stl, blk, tov, fgm, fga, tpm, tpa, ftm, fta] for everyone who played.

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
                             num("STL"), num("BLK"), num("TO"), *made_att("FG"), *made_att("3PT"), *made_att("FT")])
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
