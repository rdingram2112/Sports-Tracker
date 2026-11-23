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

- **Team stats** (`/`): the table above. Click a team to see its games in the window.
- **Matchups** (`#matchups`): each upcoming game with both teams' stats side by side.
- **Players** (`#players`): player leaders in MIN, PTS, REB, AST, STL, BLK and TOV over each player's last X games, as totals or per game. Per game lists players who played at least half the window. Headshots load from ESPN.

### Updating the data

A GitHub Action (`.github/workflows/update-nba.yml`) runs the fetch script every morning at 6:23am Eastern, commits any new games, and asks GitHub Pages to rebuild the site. Pages publishes straight from the `main` branch (Settings → Pages → Deploy from a branch → `main` / root), so any push to `main` also updates the site. You can also run it by hand from the repository's **Actions** tab ("Update NBA stats" → **Run workflow**).

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
