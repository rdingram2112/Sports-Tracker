# Sports Tracker

Pick the sport with the menu at the top right: **NBA**, **College Football** or **NFL**.

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

## NFL

Pick **NFL** in the sport menu. It has the same views as college football (the same page code, reading `data/nfl/` instead of `data/cfb/`):

- **Team stats** (`#nflteams`): all 32 teams ranked across the league by points and yards, EPA per play/dropback/rush on offense and defense (raw or opponent-adjusted), success rates, and sack, pressure and havoc rates. Green is top 8, red bottom 8.
- **Matchups** (`#nfl`): any week, with records, betting lines and both teams' stats side by side. Filter by division.
- **Players** (`#nflplayers`): QBs (passing YDS, YDS/G, TD, INT, EPA/DB, CPOE), RBs (rushing with total yards; scrimmage: touches, scrimmage yards, per game, yards per touch and total TDs; role; efficiency; receiving) and WRs/TEs (receiving, role with target share, air-yard share and aDOT, efficiency). Tap a player for his page: bio with college and draft, this season's box scores and career by season with EPA.
- **Team pages** (`#nflteam-<id>`), with record, division record and record against the spread, the overview, the schedule with lines and covers, and every player's stats.
- **Live scores** (`#nfllive`), with possession, down and distance, leaders and box scores, refreshed every 30 seconds.

"Stats from" works the same way, with **Division games only** in place of conference games.

`scripts/fetch_nfl.py` builds it all, and the daily GitHub Action runs it. Schedule, scores, records, yards, lines before kickoff, bios and career lines come from ESPN. Play-by-play comes from [nflverse](https://github.com/nflverse): EPA, win probability, air yards and expected completion % for every snap, plus closing lines for finished games and the IDs that link nflverse players to ESPN (for photos). Box-score totals count every play and match ESPN's official numbers. EPA and rate stats leave out garbage time (the offense's win probability under 5% or over 95%). Success uses the college yardage rule (50% of the yards to go on 1st down, 70% on 2nd, 100% on 3rd/4th). Past seasons' EPA for career tables is computed once from each season's nflverse play-by-play since 2016 (`data/nfl/pastepa.json`).

## Defense (college and NFL)

On **Players**, switch **Offense | Defense**. Defense starts with **DL & edge rushers**: defensive linemen (DE, DT, NT) plus linebackers who mostly rush the passer (at least 0.8 pressures per game, and pressures at least 30% of their tackles), tagged EDGE. That's how T.J. Watt, Micah Parsons and Myles Garrett show up even though they're listed as linebackers. Three stat sets:

- **Pass rush**: sacks (half sacks count 0.5), pressures (sacks plus QB hits that weren't sacks in the NFL; sacks plus QB hurries in college), pressures per game, hits or hurries, and sack % (the share of pressures that became sacks)
- **Run defense**: run tackles, stops (tackles that held the offense to an unsuccessful play, using the same yardage rule as success rate), stop % (stops per run tackle), and depth (the yards the runner had gained at the tackle; lower is better)
- **Overall**: tackles, solo tackles, TFL, passes defensed, forced fumbles

Tap a player for his page: bio, this season's box scores (TKL, SACK, TFL, PRESS, STOP, PD) and career by season with games played. Team pages also get a **Defensive line** table.

NFL numbers come from nflverse play-by-play, which lists every tackler, sack, hit, TFL, forced fumble and breakup (`scripts/fb_def.py`, `data/nfl/dls.json`). Tackles there count scrimmage plays only, so they run a little under official totals, which include special teams. College box-score numbers (tackles, sacks, TFL, breakups, hurries, interceptions) come from CollegeFootballData; stops and forced fumbles come from the play text, which names the tacklers (about 98% of tackles are matched to a player). College hurries depend on each stat crew, so pressures there are less consistent than the NFL's. Neither league has snap counts per pass rush, so these are totals and per-game numbers, not per-snap rates. Weeks are cached in `data/cfb/defgames.json`.

## College football

**Team stats** (`#cfbteams`): every FBS team ranked nationally, switching between Points & yards (PTS/G, OPP/G, YDS/G, OPP YDS), EPA offense and EPA defense (per play, per dropback, per rush). Sort by any column and filter to one conference. "Stats from" (season, last 3, last 5 or any number of games, and conference games only) is shared with Matchups.

**Live scores** (`#cfblive`): this week's games straight from ESPN in the browser, refreshed every 30 seconds while games are on. Live games come first, showing quarter-by-quarter scores, who has the ball, down and distance (highlighted in the red zone), the last play, and passing, rushing and receiving leaders. Upcoming games show kickoff, TV and the line. Filter to Top 25 games, all FBS, or one conference. Tap a game to open it:

- **Field** (live games): a field in both teams' colors showing the line of scrimmage, the first-down line, the ball with the direction of play, and the current drive. Underneath are the down and distance, the drive so far, the last play, and win probability.
- **Play-by-play**: every drive (newest first) with its result, and every play with the clock and down and distance. Scoring plays show the new score. Drives you open or close stay that way through the 30-second refreshes. Finished games have the same list under **Plays**.
- **Box score**: a **win probability** chart (ESPN's win probability after every play, plotted on the game clock with the home team on top, scoring plays as dots you can tap, and the game's biggest swing), team stats side by side (college games add red zone TDs and scores, counted from the drives: trips are drives that ran a play inside the 20), plus passing, rushing and receiving for each team. "Show all stats" adds defense, interceptions, kicking and punting. Player names link to their pages.
- **Before kickoff**, a **Preview** with the line, records and both teams' stats with ranks.

NFL live scores work the same way.

**Team pages** (`#cfbteam-<id>`): tap a team in Team stats, on a matchup card, an opponent in a schedule, or the team name on a player page. Shows the team's ranking, record, conference record and record against the spread; an overview of offense and defense side by side (points, yards, 3rd down % and red zone TD %, EPA per play/dropback/rush, success rates, sack, pressure and havoc rates) with national ranks; the full schedule with results, betting lines and whether they covered; and passing, rushing and receiving stats for every player (tap one for their page). The overview and player stats follow the "Stats from" filters.

3rd down % comes from each game's ESPN box score. Red zone TD % is touchdowns per trip inside the opponent's 20: for NFL games from ESPN's box score; for college (ESPN's college box scores have no red-zone line) from play-by-play, where a trip is a drive that ran a play inside the 20 and counts as a touchdown if it scored a rushing or passing TD. Both count every play (no garbage-time filter) and aren't opponent-adjusted. Team stats has a matching **3rd down & red zone** set.

Lines for games played before the site started saving them are filled in once from CollegeFootballData's closing lines.

**Matchups** (`#cfb`): pick any week of the season (‹ › or the week menu). Past weeks show final scores, the current week shows upcoming games with the betting line (spread, over/under, moneyline), and future weeks fill in as ESPN posts them. Rankings are ESPN's for each game (CFP once released, AP before). Filter to Top 25 games or any conference.

Each card compares the two teams' points and total yards per game, scored and allowed, with national ranks among FBS teams. "Stats from" limits those to the whole season or each team's last 3 or 5 games, and optionally conference games only; they count games played before the selected week.

From CollegeFootballData.com play-by-play (garbage time excluded) the script also counts, per team-game: success rate overall, on early (1st-2nd) and late (3rd-4th) downs, and on standard and passing downs; sack rate; pressure rate (sacks plus dropbacks the stat crew noted as hurried or hit, so it undercounts and varies by crew); and havoc rate. Each week's plays are downloaded once (`data/cfb/playstats.json`). Team stats has Success and Pressure & havoc column sets, and matchup cards have matching sections (pick which with **Card stats**).

**Players** (`#cfbplayers`): tap a quarterback to open his page (`#cfbqb-<id>`): photo and bio, this season's box scores (CMP/ATT, yards, TD, INT, EPA/DB, CPOE, rushing) and career stats by season at every school, with EPA/DB. Careers come from ESPN and CollegeFootballData via `cfb_qb.build_careers` (`data/cfb/qbcareers.json`, refreshed weekly). Quarterbacks with YDS/G, TD, INT, EPA per dropback (sacks included, garbage time excluded) and CPOE, ranked among QBs with at least 15 attempts per team game, using the same Stats from filters (each team's last X games, conference games only). Built by `scripts/cfb_qb.py` from CollegeFootballData box scores and play-by-play; CPOE uses a completion model from every pass this season (depth, direction, down, distance, field position). Writes `data/cfb/qbs.json`, caching each week in `data/cfb/qbgames.json`.

**Running backs** (Players → Position → RB): backs with at least 6 carries per team game in the period, with four column sets (pick with the second menu; on phones each set fits the screen):

- **Rushing**: carries, rushing yards, YDS/G, yards per carry, TD
- **Scrimmage**: touches (carries + catches), scrimmage yards (rushing + receiving), scrimmage yards per game, yards per touch, total TDs
- **Role**: carry share (of the team's runs), red-zone carries (inside the 20), goal-line carries (inside the 5), short-yardage conversion (3rd/4th and 2 or less)
- **Efficiency**: EPA per rush, success rate, explosive rate (12+ yards), stuff rate (0 or fewer yards; fewer is better) and highlight yards per opportunity (yards beyond the first 4 on runs of 4+, a measure of what the back adds past the line)
- **Receiving**: targets, catches, yards, TD, EPA per target

Tap a back for his page (`#cfbrb-<id>`): bio, this season's box scores (rushing, EPA, success, catches/yards) and career stats by season with EPA per rush. Built by `scripts/cfb_rb.py` from CollegeFootballData box scores, rosters and play-by-play (garbage time excluded for the efficiency stats); writes `data/cfb/rbs.json`, caching each week in `data/cfb/rbgames.json`, with careers in `data/cfb/rbcareers.json` (refreshed weekly).

**Wide receivers and tight ends** (Players → Position → WR or TE, ranked separately): receivers with at least 3 targets per team game (tight ends 2), with three column sets:

- **Receiving**: targets, catches, receiving yards, YDS/G, TD
- **Role**: target share, air-yard share, aDOT (average depth of target), red-zone targets, 20+ yard catches
- **Efficiency**: catch rate, yards per target, yards after catch per catch, EPA per target, success rate per target

College play-by-play has no air-yards field, so `scripts/cfb_wr.py` reads where each pass was caught or thrown from the play text ("caught at WSU45", "thrown to OhioSt30"), working out which team each abbreviation belongs to from the spots where plays ended. About 98% of targets get air yards. Tap a receiver for his page (`#cfbwr-<id>`): bio, this season's box scores (targets, catches, yards, TD, aDOT, YAC, EPA) and career stats with EPA per target. Writes `data/cfb/wrs.json` (weeks cached in `data/cfb/wrgames.json`) and `data/cfb/wrcareers.json`.

EPA can be shown raw or **opponent-adjusted** (the default): `scripts/fetch_cfb.py` rates every offense and defense at once from per-game EPA (each game weighted by plays, home field included, ratings measured against an average FBS team), then stores each game's EPA adjusted for that opponent, so the page's period filters work on adjusted numbers too.

Cards also show EPA (expected points added) per play, per dropback and per rush for offense and defense, from [CollegeFootballData.com](https://collegefootballdata.com) with garbage time excluded. It needs a free API key stored as the repository secret `CFBD_API_KEY` (Settings → Secrets and variables → Actions); without it the last saved EPA (`data/cfb/epa.json`) is kept.

`scripts/fetch_cfb.py` writes `data/cfb/season.json` (every week's games plus a row per team per completed game). Box scores are downloaded once per game (`data/cfb/boxscores.json`), and the last betting line seen before kickoff is kept (`data/cfb/lines.json`) because ESPN removes lines once games start. The daily GitHub Action runs it each morning and mid-afternoon Eastern.
