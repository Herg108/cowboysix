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

    # Start recording
    for r in selected:
        print(f"\nStarting recorder for {r['map_label'].upper()}: {r['team1_name']} vs {r['team2_name']}")
        print(f"  Output: {r['output_path']}")
        print(f"  Chart:  {r['output_path']}/live_chart.html")

    if len(selected) == 1:
        # Run directly in foreground
        r = selected[0]
        os.execvp("python3", [
            "python3", "live_price_recorder.py",
            "--team1-name", r["team1_name"],
            "--team1-token", r["team1_token"],
            "--team2-name", r["team2_name"],
            "--team2-token", r["team2_token"],
            "--output", r["output_path"],
        ])
    else:
        # Run each in background
        pids = []
        for r in selected:
            pid = os.spawnlp(
                os.P_NOWAIT, "python3",
                "python3", "live_price_recorder.py",
                "--team1-name", r["team1_name"],
                "--team1-token", r["team1_token"],
                "--team2-name", r["team2_name"],
                "--team2-token", r["team2_token"],
                "--output", r["output_path"],
            )
            pids.append((pid, r["map_label"]))
            print(f"  Started {r['map_label']} recorder (PID: {pid})")

        print(f"\n{len(pids)} recorders running in background.")
        print("They'll stop automatically when each market resolves.")
        print("To stop manually: pkill -f live_price_recorder.py")


if __name__ == "__main__":
    main()
