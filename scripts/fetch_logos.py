#!/usr/bin/env python3
"""Download small NBA team logos (light and dark versions) into assets/logos/nba/.

Reads the team list from data/nba/games.json, so run scripts/fetch_nba.py first.

Usage:
    python3 scripts/fetch_logos.py
"""
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "nba" / "games.json"
OUT = ROOT / "assets" / "logos" / "nba"
SIZE = 64  # pixels; shown at 28px, so this stays sharp on high-density screens
URL = "https://a.espncdn.com/combiner/i?img=/i/teamlogos/nba/{variant}/{slug}.png&h={size}&w={size}"


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "sports-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    if not body.startswith(b"\x89PNG"):
        raise ValueError(f"not a PNG: {url}")
    dest.write_bytes(body)


def main():
    teams = json.loads(DATA.read_text())["teams"]
    OUT.mkdir(parents=True, exist_ok=True)
    for abbr, info in teams.items():
        # ESPN logo URLs end in /<slug>.png, e.g. .../500/mil.png
        slug = info["logo"].rsplit("/", 1)[-1].removesuffix(".png")
        for variant, suffix in (("500", ""), ("500-dark", "-dark")):
            dest = OUT / f"{abbr}{suffix}.png"
            try:
                download(URL.format(variant=variant, slug=slug, size=SIZE), dest)
            except Exception as exc:
                print(f"  {abbr} {variant}: {exc}")
            time.sleep(0.1)
    print(f"Saved logos for {len(teams)} teams in {OUT}")


if __name__ == "__main__":
    main()
