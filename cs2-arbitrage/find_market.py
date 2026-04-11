"""
Find and record Polymarket CS2 match prices.

Usage:
    python3 find_market.py <polymarket-slug-or-url>
    python3 find_market.py <polymarket-slug-or-url> <hltv-url>

Examples:
    python3 find_market.py cs2-faze-tyloo-2026-03-20
    python3 find_market.py https://polymarket.com/sports/counter-strike/cs2-navi-spirit-2026-03-21 https://www.hltv.org/matches/2391770/match
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

            outs = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            toks = json.loads(tokens) if isinstance(tokens, str) else tokens

            if len(outs) != 2:
                continue

            # Detect map N winner markets
            import re as _re
            map_match = _re.search(r'map (\d+)', q_lower)
            is_map_winner = map_match is not None and "winner" in q_lower
            is_moneyline = "map" not in q_lower and "total" not in q_lower and "handicap" not in q_lower and "odd" not in q_lower

            if is_map_winner:
                map_label = f"map{map_match.group(1)}"
            elif is_moneyline:
                map_label = "moneyline"
            else:
                continue

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

    # If moneyline exists, use it as the last map if that map market is missing
    # (in bo3, map3 winner = series winner; in bo5, map5 winner = series winner)
    labels = {r["map_label"] for r in results}
    map_nums = sorted([int(l[3:]) for l in labels if l.startswith("map")])
    max_map = max(map_nums) if map_nums else 0
    # Determine bo size from map markets
    if max_map <= 3:
        last_map = "map3"
    else:
        last_map = f"map{max_map + 1}" if max_map < 5 else "map5"

    if last_map not in labels and "moneyline" in labels:
        for r in results:
            if r["map_label"] == "moneyline":
                r["map_label"] = last_map
                r["output_path"] = r["output_path"].replace("/moneyline", f"/{last_map}")
                break

    # Remove moneyline if we already have the last map
    results = [r for r in results if r["map_label"] != "moneyline"]

    # Sort by map number
    results.sort(key=lambda r: int(r["map_label"][3:]) if r["map_label"].startswith("map") else 99)
    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 find_market.py <polymarket-slug-or-url> [hltv-url]")
        print("Example: python3 find_market.py cs2-faze-tyloo-2026-03-20 https://www.hltv.org/matches/123/match")
        return

    query = sys.argv[1]

    # Check if HLTV URL was passed as second arg
    hltv_url = None
    if len(sys.argv) >= 3 and "hltv.org" in sys.argv[2]:
        hltv_url = sys.argv[2]

    results = search_markets(query)

    if not results:
        return

    print(f"\nFound {len(results)} map markets:\n")
    for i, r in enumerate(results):
        print(f"  [{i + 1}] {r['map_label'].upper():6s}  {r['team1_name']} vs {r['team2_name']}  →  {r['output_path']}")

    print()

    # Ask for HLTV URL if not passed as arg
    if not hltv_url:
        hltv_url = input("HLTV match URL (or press Enter to skip): ").strip()

    # Use the first result's output dir as base for HLTV
    base_dir = "/".join(results[0]["output_path"].split("/")[:3])  # data/date/match_name

    # Build maps JSON for multi-map recorder
    maps_json = json.dumps(results)

    # Start ONE Polymarket recorder with all maps' info
    print(f"\nStarting Polymarket recorder (all maps)")
    print(f"  Live: http://localhost:8888/live.html")

    pid = os.spawnlp(
        os.P_NOWAIT, "python3",
        "python3", "live_price_recorder.py",
        "--maps-json", maps_json,
    )
    pids = [(pid, "polymarket-all-maps")]
    print(f"  PID: {pid}")

    # Start HLTV tracker
    if hltv_url:
        # infer bo from number of map markets: 1-3 maps = bo3, 4-5 maps = bo5
        best_of = 5 if len(results) > 3 else 3

        print(f"\nStarting HLTV tracker (series mode)")
        print(f"  URL: {hltv_url}")
        print(f"  Output: {base_dir}/")
        print(f"  Best of {best_of}")

        polymarket_url = query if query.startswith("http") else f"https://polymarket.com/event/{query}"
        pid = os.spawnlp(
            os.P_NOWAIT, "python3",
            "python3", "hltv_live.py",
            hltv_url,
            "--output", base_dir,
            "--best-of", str(best_of),
            "--polymarket-url", polymarket_url,
        )
        pids.append((pid, "hltv"))
        print(f"  PID: {pid}")

    print(f"\n{'='*60}")
    print(f"{len(pids)} recorders running:")
    for pid, label in pids:
        print(f"  [{pid}] {label}")
    print(f"\nHLTV tracker auto-detects maps and tracks series completion.")
    print(f"Polymarket recorder switches tokens when a new map starts.")
    print(f"\nTo stop everything: bash stop.sh")


if __name__ == "__main__":
    main()
