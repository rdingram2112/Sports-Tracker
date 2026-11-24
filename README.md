# Sports Tracker

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
- **Live scores** (`#live`): today's and yesterday's games with quarter-by-quarter scores and team leaders. Click a game for its box score (MIN, FG, 3PT, FT, PTS, REB, AST for every player; "Show all stats" adds STL, BLK, TOV and PF), which also refreshes every 30 seconds during a live game. The page fetches ESPN's scoreboard directly in the browser and refreshes every 30 seconds while the view is open.
- **Players** (`#players`): player leaders in MIN, eFG%, 3P rate, 3P%, FT rate, FT%, PTS, REB, OREB, AST, STL, BLK and TOV over the last week, 2 weeks, month or season, as totals or per game, with each stat's percentile among players at the same position (G, F, C) underneath. Per game lists players who played at least half of their team's games in that period, so injured players drop off. On phones the columns are split into Basics, Efficiency and Hustle groups. Click a player for a game-by-game bar chart of any stat with season and recent averages. Headshots load from ESPN.

### Updating the data

A GitHub Action (`.github/workflows/update-nba.yml`) runs the fetch script every morning at 6:23am Eastern (and again at 3:23pm Eastern to refresh betting lines), commits any new games, and asks GitHub Pages to rebuild the site. Pages publishes straight from the `main` branch (Settings → Pages → Deploy from a branch → `main` / root), so any push to `main` also updates the site. You can also run it by hand from the repository's **Actions** tab ("Update NBA stats" → **Run workflow**).

To run it yourself:

```bash
python3 scripts/fetch_nba.py
```

This pulls final box scores from ESPN's public API into `data/nba/games.json`. It only downloads games it doesn't already have.

Team logos live in `assets/logos/nba/` (a light and a dark version per team). To re-download them:

```bash
python3 scripts/fetch_logos.py
```

### Viewing locally

```bash
python3 -m http.server 8000
```

Then open http://localhost:8000.
