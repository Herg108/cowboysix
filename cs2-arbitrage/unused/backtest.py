#!/usr/bin/env python3
"""
Backtester: Reconstruct historical round-end → market-reaction delays.

Pipeline:
1. Parse a CS2 .dem file to extract round-end tick numbers
2. Convert ticks to approximate wall-clock timestamps using match start time + tickrate
3. Fetch historical Polymarket prices for the match time window
4. For each round end, find the nearest price points before/after and measure the reaction

Usage:
    # Full backtest with demo file + Polymarket data
    python backtest.py --demo-file match.dem --match-start 1710000000 --token-id <TOKEN>

    # Just parse a demo file to see round-by-round data
    python backtest.py --demo-file match.dem --parse-only

    # Just fetch Polymarket price history for a time range
    python backtest.py --token-id <TOKEN> --start-ts 1710000000 --end-ts 1710010000 --prices-only

    # Fetch historical + resolved CS2 markets from Polymarket
    python backtest.py --find-markets
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pandas as pd

import config


CS2_TICKRATE = 64  # CS2 server tickrate


@dataclass
class RoundEvent:
    round_num: int
    tick: int
    elapsed_s: float  # seconds from match start
    wall_clock_ts: float  # unix timestamp (estimated)
    winner: str  # "CT" or "T"
    reason: str  # round end reason
    score_ct: int
    score_t: int


def parse_demo(demo_path: str) -> list[RoundEvent]:
    """Parse a CS2 .dem file and extract round-end events with tick numbers."""
    from demoparser2 import DemoParser

    parser = DemoParser(demo_path)

    # Parse game events for round_end
    round_ends = parser.parse_events("round_end")

    events = []
    ct_score = 0
    t_score = 0

    for _, row in round_ends.iterrows():
        tick = int(row.get("tick", 0))
        winner_code = int(row.get("winner", 0))
        reason_code = int(row.get("reason", 0))

        # Winner: 2 = T, 3 = CT
        winner = "T" if winner_code == 2 else "CT" if winner_code == 3 else "unknown"
        if winner == "CT":
            ct_score += 1
        elif winner == "T":
            t_score += 1

        # Reason codes: 1=target_bombed, 7=bomb_defused, 8=ct_elim, 9=t_elim, etc.
        reason_map = {
            1: "target_bombed",
            7: "bomb_defused",
            8: "ct_elimination",
            9: "t_elimination",
            10: "round_timer",
            12: "ct_surrender",
            17: "t_surrender",
        }
        reason = reason_map.get(reason_code, f"code_{reason_code}")

        elapsed_s = tick / CS2_TICKRATE
        round_num = ct_score + t_score

        events.append(RoundEvent(
            round_num=round_num,
            tick=tick,
            elapsed_s=elapsed_s,
            wall_clock_ts=0,  # set later when match_start is known
            winner=winner,
            reason=reason,
            score_ct=ct_score,
            score_t=t_score,
        ))

    return events


def set_wall_clock(events: list[RoundEvent], match_start_ts: float):
    """Set wall_clock_ts on each event based on match start time."""
    for e in events:
        e.wall_clock_ts = match_start_ts + e.elapsed_s


async def fetch_price_history(
    token_id: str,
    start_ts: int,
    end_ts: int,
    fidelity: int = 1,
) -> list[dict]:
    """Fetch Polymarket price history for a token in a time range.

    Args:
        token_id: CLOB token ID
        start_ts: Unix timestamp start
        end_ts: Unix timestamp end
        fidelity: Granularity in minutes (default 1)

    Returns:
        List of {"t": unix_ts, "p": price} dicts sorted by time.
    """
    all_points = []
    chunk_size = 15 * 24 * 3600  # 15 days per request to avoid empty responses

    async with httpx.AsyncClient(timeout=30.0) as client:
        current_start = start_ts
        while current_start < end_ts:
            current_end = min(current_start + chunk_size, end_ts)
            resp = await client.get(
                f"{config.POLYMARKET_CLOB_URL}/prices-history",
                params={
                    "market": token_id,
                    "startTs": current_start,
                    "endTs": current_end,
                    "fidelity": fidelity,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            history = data.get("history", [])
            all_points.extend(history)
            current_start = current_end

    # Sort by time and deduplicate
    all_points.sort(key=lambda x: x["t"])
    seen = set()
    deduped = []
    for p in all_points:
        if p["t"] not in seen:
            seen.add(p["t"])
            deduped.append(p)

    return deduped


def correlate_rounds_with_prices(
    rounds: list[RoundEvent],
    prices: list[dict],
    window_before_s: int = 60,
    window_after_s: int = 300,
) -> list[dict]:
    """For each round end, find the market price before and after, measure reaction.

    Args:
        rounds: Round events with wall_clock_ts set
        prices: Price history points [{"t": ts, "p": price}, ...]
        window_before_s: How far back to look for pre-round price
        window_after_s: How far forward to look for post-round price reaction

    Returns:
        List of analysis dicts per round.
    """
    if not prices:
        print("[WARN] No price data to correlate with")
        return []

    price_ts = [p["t"] for p in prices]
    price_vals = [p["p"] for p in prices]

    results = []
    for rnd in rounds:
        t = rnd.wall_clock_ts

        # Find nearest price BEFORE round end
        price_before = None
        price_before_ts = None
        for i in range(len(price_ts) - 1, -1, -1):
            if price_ts[i] <= t:
                price_before = price_vals[i]
                price_before_ts = price_ts[i]
                break

        # Find the price point that represents the market's reaction AFTER round end
        # Look for the first significant move, or just take the price 1-5 min after
        price_after = None
        price_after_ts = None
        max_move = 0
        for i in range(len(price_ts)):
            if price_ts[i] > t and price_ts[i] <= t + window_after_s:
                if price_after is None:
                    # First price point after round end
                    price_after = price_vals[i]
                    price_after_ts = price_ts[i]
                # Track the point with the biggest move from pre-round price
                if price_before is not None:
                    move = abs(price_vals[i] - price_before)
                    if move > max_move:
                        max_move = move
                        price_after = price_vals[i]
                        price_after_ts = price_ts[i]

        delay_s = (price_after_ts - t) if price_after_ts else None
        price_delta = (price_after - price_before) if (price_before is not None and price_after is not None) else None

        results.append({
            "round": rnd.round_num,
            "score": f"{rnd.score_ct}-{rnd.score_t}",
            "winner": rnd.winner,
            "reason": rnd.reason,
            "round_end_ts": round(t, 1),
            "round_elapsed_s": round(rnd.elapsed_s, 1),
            "price_before": price_before,
            "price_before_ts": price_before_ts,
            "price_after": price_after,
            "price_after_ts": price_after_ts,
            "delay_to_reaction_s": round(delay_s, 1) if delay_s is not None else None,
            "price_delta": round(price_delta, 4) if price_delta is not None else None,
            "price_move_pct": round(price_delta / price_before * 100, 2) if (price_delta and price_before) else None,
        })

    return results


def print_analysis(results: list[dict]):
    """Pretty-print backtest results."""
    print(f"\n{'='*80}")
    print(f"  BACKTEST RESULTS — {len(results)} rounds analyzed")
    print(f"{'='*80}\n")

    print(f"{'Rnd':>4} {'Score':>6} {'Win':>3} {'Reason':<18} {'Price Before':>12} {'Price After':>12} {'Delta':>8} {'Delay(s)':>9}")
    print("-" * 80)

    delays = []
    for r in results:
        pb = f"${r['price_before']:.3f}" if r['price_before'] is not None else "—"
        pa = f"${r['price_after']:.3f}" if r['price_after'] is not None else "—"
        delta = f"{r['price_delta']:+.4f}" if r['price_delta'] is not None else "—"
        delay = f"{r['delay_to_reaction_s']:.0f}" if r['delay_to_reaction_s'] is not None else "—"

        print(f"{r['round']:>4} {r['score']:>6} {r['winner']:>3} {r['reason']:<18} {pb:>12} {pa:>12} {delta:>8} {delay:>9}")

        if r['delay_to_reaction_s'] is not None and r['price_delta'] is not None and abs(r['price_delta']) > 0.005:
            delays.append(r['delay_to_reaction_s'])

    print(f"\n{'='*80}")
    if delays:
        print(f"  Rounds with detectable price reaction (>{0.5}¢ move): {len(delays)}/{len(results)}")
        print(f"  Average reaction delay: {sum(delays)/len(delays):.1f}s")
        print(f"  Median reaction delay:  {sorted(delays)[len(delays)//2]:.1f}s")
        print(f"  Min delay:              {min(delays):.1f}s")
        print(f"  Max delay:              {max(delays):.1f}s")

        if sum(delays) / len(delays) > 15:
            print(f"\n  >>> EDGE LIKELY EXISTS: avg delay {sum(delays)/len(delays):.0f}s is well above execution threshold <<<")
        elif sum(delays) / len(delays) > 5:
            print(f"\n  >>> MARGINAL EDGE: avg delay {sum(delays)/len(delays):.0f}s — tight but potentially exploitable <<<")
        else:
            print(f"\n  >>> NO CLEAR EDGE: avg delay {sum(delays)/len(delays):.0f}s — market reacts too fast <<<")
    else:
        print("  No rounds with detectable price reactions (all moves < 0.5¢)")
    print(f"{'='*80}\n")


async def find_cs2_markets(include_closed: bool = True):
    """Search Polymarket for CS2 markets, including resolved ones."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        params = {"limit": 100, "order": "volume", "ascending": "false"}
        if not include_closed:
            params["active"] = "true"
            params["closed"] = "false"

        resp = await client.get(f"{config.POLYMARKET_GAMMA_URL}/markets", params=params)
        resp.raise_for_status()
        markets = resp.json()

        cs2_keywords = {"cs2", "counter-strike", "counter strike", "csgo", "cs:go", "major", "iem", "blast", "esl"}
        # Also search with broader esports terms in case CS2 isn't in title
        team_keywords = {"navi", "faze", "g2", "vitality", "spirit", "mouz", "heroic", "liquid", "cloud9", "complexity", "eternal fire", "virtus.pro"}

        cs2_markets = []
        for m in markets:
            text = f"{m.get('question', '')} {m.get('description', '')} {m.get('slug', '')}".lower()
            if any(kw in text for kw in cs2_keywords) or any(kw in text for kw in team_keywords):
                tokens = m.get("clobTokenIds", "")
                if isinstance(tokens, str):
                    try:
                        tokens = json.loads(tokens) if tokens else []
                    except json.JSONDecodeError:
                        tokens = []

                cs2_markets.append({
                    "slug": m.get("slug", ""),
                    "question": m.get("question", ""),
                    "tokens": tokens,
                    "volume": m.get("volume", 0),
                    "active": m.get("active", False),
                    "closed": m.get("closed", False),
                    "end_date": m.get("endDate", ""),
                    "start_date": m.get("startDate", ""),
                    "condition_id": m.get("conditionId", ""),
                })

        return cs2_markets


async def run_find_markets():
    print("Searching Polymarket for CS2-related markets (including resolved)...\n")
    markets = await find_cs2_markets(include_closed=True)

    if not markets:
        print("No CS2 markets found.")
        print("Tip: Try browsing https://polymarket.com/sports/counter-strike/games")
        return

    active = [m for m in markets if m["active"] and not m["closed"]]
    closed = [m for m in markets if m["closed"]]

    if active:
        print(f"=== ACTIVE MARKETS ({len(active)}) ===\n")
        for m in active[:10]:
            tokens = m["tokens"]
            print(f"  {m['question']}")
            print(f"    slug: {m['slug']}")
            print(f"    volume: ${float(m.get('volume', 0)):,.0f}")
            if len(tokens) >= 2:
                print(f"    YES token: {tokens[0]}")
                print(f"    NO token:  {tokens[1]}")
            print()

    if closed:
        print(f"=== RESOLVED MARKETS ({len(closed)}) — usable for backtesting ===\n")
        for m in closed[:10]:
            tokens = m["tokens"]
            print(f"  {m['question']}")
            print(f"    slug: {m['slug']}")
            print(f"    dates: {m.get('start_date', '?')} → {m.get('end_date', '?')}")
            if len(tokens) >= 2:
                print(f"    YES token: {tokens[0]}")
                print(f"    NO token:  {tokens[1]}")
            print()


async def run_prices_only(token_id: str, start_ts: int, end_ts: int):
    print(f"Fetching price history for token {token_id[:20]}...")
    print(f"  Range: {start_ts} → {end_ts} ({(end_ts - start_ts) / 3600:.1f} hours)\n")
    prices = await fetch_price_history(token_id, start_ts, end_ts, fidelity=1)
    print(f"Got {len(prices)} price points\n")
    if prices:
        print(f"{'Timestamp':>12} {'Price':>8}")
        print("-" * 22)
        # Show first 10 and last 10
        show = prices[:10] + ([{"t": "...", "p": "..."}] if len(prices) > 20 else []) + prices[-10:]
        for p in show:
            if isinstance(p["t"], str):
                print(f"{'...':>12} {'...':>8}")
            else:
                print(f"{p['t']:>12} ${p['p']:.4f}")
    print()

    # Save to file
    out_path = config.DATA_DIR / "price_history.json"
    with open(out_path, "w") as f:
        json.dump(prices, f, indent=2)
    print(f"Saved to {out_path}")


async def run_backtest(demo_path: str, match_start_ts: int, token_id: str):
    """Full backtest: parse demo + fetch prices + correlate."""
    print(f"Parsing demo: {demo_path}")
    rounds = parse_demo(demo_path)
    print(f"  Found {len(rounds)} rounds\n")

    if not rounds:
        print("No rounds found in demo file.")
        return

    set_wall_clock(rounds, match_start_ts)

    # Print round summary
    print(f"{'Rnd':>4} {'Tick':>8} {'Elapsed':>8} {'Winner':>6} {'Reason':<18} {'Score':>6}")
    print("-" * 60)
    for r in rounds:
        print(f"{r.round_num:>4} {r.tick:>8} {r.elapsed_s:>7.1f}s {r.winner:>6} {r.reason:<18} {r.score_ct}-{r.score_t}")

    # Fetch price history covering the match duration + buffer
    first_ts = int(rounds[0].wall_clock_ts) - 300  # 5 min before
    last_ts = int(rounds[-1].wall_clock_ts) + 600  # 10 min after
    print(f"\nFetching Polymarket prices ({(last_ts - first_ts) / 60:.0f} min window)...")
    prices = await fetch_price_history(token_id, first_ts, last_ts, fidelity=1)
    print(f"  Got {len(prices)} price points\n")

    if not prices:
        print("No price data available for this time range.")
        print("The market may not have had trading activity during this match,")
        print("or the token ID may be incorrect.")
        return

    # Correlate and analyze
    results = correlate_rounds_with_prices(rounds, prices)
    print_analysis(results)

    # Save full results
    out_path = config.DATA_DIR / "backtest_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Full results saved to {out_path}")


def main():
    parser = argparse.ArgumentParser(description="CS2 Arbitrage Backtester")
    parser.add_argument("--demo-file", help="Path to CS2 .dem file")
    parser.add_argument("--match-start", type=int, help="Match start unix timestamp")
    parser.add_argument("--token-id", help="Polymarket CLOB token ID")
    parser.add_argument("--start-ts", type=int, help="Price history start timestamp")
    parser.add_argument("--end-ts", type=int, help="Price history end timestamp")
    parser.add_argument("--parse-only", action="store_true", help="Only parse demo, no market data")
    parser.add_argument("--prices-only", action="store_true", help="Only fetch price history")
    parser.add_argument("--find-markets", action="store_true", help="Search for CS2 markets on Polymarket")
    args = parser.parse_args()

    if args.find_markets:
        asyncio.run(run_find_markets())
        return

    if args.prices_only:
        if not args.token_id or not args.start_ts or not args.end_ts:
            parser.error("--prices-only requires --token-id, --start-ts, and --end-ts")
        asyncio.run(run_prices_only(args.token_id, args.start_ts, args.end_ts))
        return

    if args.parse_only:
        if not args.demo_file:
            parser.error("--parse-only requires --demo-file")
        rounds = parse_demo(args.demo_file)
        print(f"Parsed {len(rounds)} rounds from {args.demo_file}\n")
        for r in rounds:
            print(f"  R{r.round_num:>2}  tick={r.tick:>8}  +{r.elapsed_s:>7.1f}s  {r.winner:>3}  {r.reason:<18}  {r.score_ct}-{r.score_t}")
        return

    # Full backtest
    if not args.demo_file or not args.match_start or not args.token_id:
        parser.error("Full backtest requires --demo-file, --match-start, and --token-id")
    asyncio.run(run_backtest(args.demo_file, args.match_start, args.token_id))


if __name__ == "__main__":
    main()
