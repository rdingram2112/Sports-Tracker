# Sports Tracker

Pick the sport with the menu at the top right: **NBA** or **College Football**.

## NBA trailing stats

`index.html` shows every NBA team's stats over its last X games (5, 10, 15, the full season, or any number you type), each with the team's league rank for that window:

- **eFG%**: (FGM + 0.5 × 3PM) ÷ FGA
- **3P%**: 3PM ÷ 3PA
- **OREB**: offensive rebounds per game
- **FTA**: free throw attempts per game
- **TOV**: turnovers per game (rank 1 = fewest)

Percentages are totaled across the window rather than averaged game by game. Regular season only.

## Views

Use the menu at the top left to switch views (each has its own link):

- **Team stats** (`/`): the table above. Click a team to see its games in the window. On phones the columns are split into Ratings, Shooting and Hustle groups so each fits the screen.
- **Matchups** (`#matchups`): each upcoming game with both teams' records, the betting line (spread, over/under, moneyline) and stats side by side.
- **Player pages** (`#player-<ESPN id>`): open from **View profile** in a player's chart on the Players view, or by tapping a name in a box score. Shows the player's photo and bio, every NBA season with position percentiles for that season, and a shot chart by court zone (FG% and share of shots, colored against the league average) for this season, the last month or the last 2 weeks.
- **Live scores** (`#live`): today's and yesterday's games with quarter-by-quarter scores and team leaders. Click a game for its box score (MIN, FG, 3PT, FT, PTS, REB, AST for every player; "Show all stats" adds STL, BLK, TOV and PF), which also refreshes every 30 seconds during a live game. The page fetches ESPN's scoreboard directly in the browser and refreshes every 30 seconds while the view is open.
- **Players** (`#players`): player leaders in MIN, eFG%, 3P rate, 3P%, FT rate, FT%, PTS, REB, OREB, AST, STL, BLK and TOV over the last week, 2 weeks, month or season, as totals or per game, with each stat's percentile among players at the same position (G, F, C) underneath. Per game lists players who played at least half of their team's games in that period, so injured players drop off. On phones the columns are split into Basics, Efficiency and Hustle groups. Click a player for a game-by-game bar chart of any stat with season and recent averages. Headshots load from ESPN.

### Updating the data

A GitHub Action (`.github/workflows/update-nba.yml`) runs the fetch script every morning at 6:23am Eastern (and again at 3:23pm Eastern to refresh betting lines), commits any new games, and asks GitHub Pages to rebuild the site. Pages publishes straight from the `main` branch (Settings → Pages → Deploy from a branch → `main` / root), so any push to `main` also updates the site. You can also run it by hand from the repository's **Actions** tab ("Update NBA stats" → **Run workflow**).

To run it yourself:

```bash
python3 scripts/fetch_nba.py
```

This pulls final box scores from ESPN's public API into `data/nba/games.json`. It only downloads games it doesn't already have. It also keeps the data behind player pages: shot locations (`data/nba/shots/`, one file per month), league-wide stats for every season since 2003-04 (`data/nba/seasons/`, past seasons downloaded once), player bios (`data/nba/bios.json`) and each current player's career with percentiles (`data/nba/careers.json`).

Team logos live in `assets/logos/nba/` (a light and a dark version per team). To re-download them:

```bash
python3 scripts/fetch_logos.py
```

### Viewing locally

```bash
python3 -m http.server 8000
```

Then open http://localhost:8000.

## College football

**Team stats** (`#cfbteams`): every FBS team ranked nationally, switching between Points & yards (PTS/G, OPP/G, YDS/G, OPP YDS), EPA offense and EPA defense (per play, per dropback, per rush). Sort by any column and filter to one conference. "Stats from" (season, last 3, last 5 or any number of games, and conference games only) is shared with Matchups.

**Matchups** (`#cfb`): pick any week of the season (‹ › or the week menu). Past weeks show final scores, the current week shows upcoming games with the betting line (spread, over/under, moneyline), and future weeks fill in as ESPN posts them. Rankings are ESPN's for each game (CFP once released, AP before). Filter to Top 25 games or any conference.

Each card compares the two teams' points and total yards per game, scored and allowed, with national ranks among FBS teams. "Stats from" limits those to the whole season or each team's last 3 or 5 games, and optionally conference games only; they count games played before the selected week.

From CollegeFootballData.com play-by-play (garbage time excluded) the script also counts, per team-game: success rate overall, on early (1st-2nd) and late (3rd-4th) downs, and on standard and passing downs; sack rate; pressure rate (sacks plus dropbacks the stat crew noted as hurried or hit, so it undercounts and varies by crew); and havoc rate. Each week's plays are downloaded once (`data/cfb/playstats.json`). Team stats has Success and Pressure & havoc column sets, and matchup cards have matching sections (pick which with **Card stats**).

**Players** (`#cfbplayers`): tap a quarterback to open his page (`#cfbqb-<id>`): photo and bio, this season's box scores (CMP/ATT, yards, TD, INT, EPA/DB, CPOE, rushing) and career stats by season at every school, with EPA/DB. Careers come from ESPN and CollegeFootballData via `cfb_qb.build_careers` (`data/cfb/qbcareers.json`, refreshed weekly). Quarterbacks with YDS/G, TD, INT, EPA per dropback (sacks included, garbage time excluded) and CPOE, ranked among QBs with at least 15 attempts per team game, using the same Stats from filters (each team's last X games, conference games only). Built by `scripts/cfb_qb.py` from CollegeFootballData box scores and play-by-play; CPOE uses a completion model from every pass this season (depth, direction, down, distance, field position). Writes `data/cfb/qbs.json`, caching each week in `data/cfb/qbgames.json`.

EPA can be shown raw or **opponent-adjusted** (the default): `scripts/fetch_cfb.py` rates every offense and defense at once from per-game EPA (each game weighted by plays, home field included, ratings measured against an average FBS team), then stores each game's EPA adjusted for that opponent, so the page's period filters work on adjusted numbers too.

Cards also show EPA (expected points added) per play, per dropback and per rush for offense and defense, from [CollegeFootballData.com](https://collegefootballdata.com) with garbage time excluded. It needs a free API key stored as the repository secret `CFBD_API_KEY` (Settings → Secrets and variables → Actions); without it the last saved EPA (`data/cfb/epa.json`) is kept.

`scripts/fetch_cfb.py` writes `data/cfb/season.json` (every week's games plus a row per team per completed game). Box scores are downloaded once per game (`data/cfb/boxscores.json`), and the last betting line seen before kickoff is kept (`data/cfb/lines.json`) because ESPN removes lines once games start. The daily GitHub Action runs it each morning and mid-afternoon Eastern.
