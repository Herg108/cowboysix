"""
Scan the data/ directory, build index.json, and copy charts into site/data/
so GitHub Pages can serve everything from the site/ folder.

Usage:
    python3 build_index.py
"""

import json
import shutil
from pathlib import Path

DATA_DIR = Path("data")
SITE_DATA = Path("site") / "data"


def build():
    index = {}

    if not DATA_DIR.exists():
        print("No data/ directory found.")
        return

    for date_dir in sorted(DATA_DIR.iterdir()):
        if not date_dir.is_dir() or not date_dir.name[0].isdigit():
            continue

        date_str = date_dir.name
        matches = {}

        for match_dir in sorted(date_dir.iterdir()):
            if not match_dir.is_dir():
                continue

            maps = []
            for map_dir in sorted(match_dir.iterdir()):
                if not map_dir.is_dir():
                    continue
                if (map_dir / "chart.html").exists():
                    maps.append(map_dir.name)

            if maps:
                matches[match_dir.name] = maps

        if matches:
            index[date_str] = matches

    # Copy new/updated chart files into site/data/
    copied = 0
    for date_str, matches in index.items():
        for match_name, maps in matches.items():
            for map_name in maps:
                src = DATA_DIR / date_str / match_name / map_name / "chart.html"
                dst = SITE_DATA / date_str / match_name / map_name / "chart.html"
                # Skip if destination is already up to date
                if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1

    # Write index.json into site/data/
    (SITE_DATA / "index.json").write_text(json.dumps(index, indent=2))

    total_matches = sum(len(m) for m in index.values())
    print(f"Built index: {len(index)} dates, {total_matches} matches, {copied} charts")
    print(f"Copied to: {SITE_DATA}/")


if __name__ == "__main__":
    build()
