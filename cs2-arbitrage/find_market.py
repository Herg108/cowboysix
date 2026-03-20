"""
Find and record Polymarket CS2 match prices.

Usage:
    python3 find_market.py <polymarket-slug-or-url>

Examples:
    python3 find_market.py cs2-faze-tyloo-2026-03-20
    python3 find_market.py https://polymarket.com/sports/counter-strike/cs2-navi-spirit-2026-03-21
"""

import sys
import os
import json
import httpx


def search_markets(query: str):
    slug = query.strip().rstrip("/")
    if "polymarket.com" in slug:
        slug = slug.split("/")[-1]

    resp = httpx.get(
        "https://gamma-api.polymarket.com/events",
        params={"slug": slug},
        timeout=10,
    )
    events = resp.json()

    if not events:
        print(f'No events found for slug: "{slug}"')
        print(f"Go to Polymarket, find the match, and copy the URL.")
        return []

    results = []
    for event in events:
        title = event.get("title", "")
        event_slug = event.get("slug", "")

        markets = event.get("markets", [])
        for m in markets:
            question = m.get("question", "")
            outcomes = m.get("outcomes", "")
            tokens = m.get("clobTokenIds", "")
            active = m.get("active", False)
            closed = m.get("closed", False)

            if not active or closed:
                continue

            q_lower = question.lower()
            is_moneyline = "bo3" in q_lower or ("vs" in q_lower and "map" not in q_lower and "handicap" not in q_lower)
            is_map_winner = ("map 1" in q_lower or "map 2" in q_lower) and "winner" in q_lower
            if not is_moneyline and not is_map_winner:
                continue

            outs = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            toks = json.loads(tokens) if isinstance(tokens, str) else tokens

            if len(outs) != 2:
                continue

            # Figure out map label
            if "map 1" in q_lower:
                map_label = "map1"
            elif "map 2" in q_lower:
                map_label = "map2"
            else:
                map_label = "map3"

            # Extract date from slug
            date_part = event_slug.split("-")[-3:] if event_slug else []
            if len(date_part) == 3 and date_part[0].isdigit():
                date_str = "-".join(date_part)
            else:
                from datetime import date
                date_str = date.today().isoformat()

            match_name = f"{outs[0].lower()}_vs_{outs[1].lower()}"
            output_path = f"data/{date_str}/{match_name}/{map_label}"

            results.append({
                "market": question,
                "map_label": map_label,
                "team1_name": outs[0],
                "team2_name": outs[1],
                "team1_token": toks[0],
                "team2_token": toks[1],
                "output_path": output_path,
            })

    # Sort: map1, map2, map3
    order = {"map1": 0, "map2": 1, "map3": 2}
    results.sort(key=lambda r: order.get(r["map_label"], 9))
    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 find_market.py <polymarket-slug-or-url>")
        print("Example: python3 find_market.py cs2-faze-tyloo-2026-03-20")
        return

    query = sys.argv[1]
    results = search_markets(query)

    if not results:
        return

    print(f"\nFound {len(results)} markets:\n")
    for i, r in enumerate(results):
        print(f"  [{i + 1}] {r['map_label'].upper():6s}  {r['team1_name']} vs {r['team2_name']}  →  {r['output_path']}")

    print(f"\n  [a] Record ALL markets at once")
    print()

    choice = input("Pick one (1/2/3/a): ").strip().lower()

    if choice == "a":
        selected = results
    elif choice.isdigit() and 1 <= int(choice) <= len(results):
        selected = [results[int(choice) - 1]]
    else:
        print("Invalid choice.")
        return

    # Ask for HLTV URL
    print()
    hltv_url = input("HLTV match URL (or press Enter to skip): ").strip()

    # Use the first selected market's output dir as base for HLTV
    base_dir = "/".join(selected[0]["output_path"].split("/")[:3])  # data/date/match_name

    # Start Polymarket recorders
    pids = []
    for r in selected:
        print(f"\nStarting Polymarket recorder for {r['map_label'].upper()}: {r['team1_name']} vs {r['team2_name']}")
        print(f"  Output: {r['output_path']}")
        print(f"  Chart:  {r['output_path']}/live_chart.html")

        pid = os.spawnlp(
            os.P_NOWAIT, "python3",
            "python3", "live_price_recorder.py",
            "--team1-name", r["team1_name"],
            "--team1-token", r["team1_token"],
            "--team2-name", r["team2_name"],
            "--team2-token", r["team2_token"],
            "--output", r["output_path"],
        )
        pids.append((pid, f"polymarket-{r['map_label']}"))
        print(f"  PID: {pid}")

    # Start HLTV recorder — writes to base_dir, auto-splits into map1/ map2/ map3/
    if hltv_url:
        hltv_output = base_dir
        print(f"\nStarting HLTV score tracker")
        print(f"  URL: {hltv_url}")
        print(f"  Output: {hltv_output}/map1/, map2/, map3/ (auto-split by map)")

        pid = os.spawnlp(
            os.P_NOWAIT, "python3",
            "python3", "hltv_live.py",
            hltv_url,
            "--output", hltv_output,
        )
        pids.append((pid, "hltv"))
        print(f"  PID: {pid}")

    print(f"\n{'='*60}")
    print(f"{len(pids)} recorders running:")
    for pid, label in pids:
        print(f"  [{pid}] {label}")
    print(f"\nPolymarket recorders stop automatically when markets resolve.")
    if hltv_url:
        print(f"HLTV tracker stops when a team reaches 13 rounds.")
    print(f"\nTo stop everything: pkill -f live_price_recorder.py; pkill -f hltv_live.py")
    print(f"To stop Chrome too: pkill -f 'chrome.*remote-debugging'")


if __name__ == "__main__":
    main()
