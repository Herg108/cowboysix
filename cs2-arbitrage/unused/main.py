#!/usr/bin/env python3
"""
CS2 Live Data vs Prediction Market Arbitrage Bot — Phase 1

Measures the delay between HLTV live match events and Polymarket price reactions
to determine if an exploitable edge exists.

Usage:
    # Monitor a live match
    python main.py --match-id 2375000 --token-yes <TOKEN_ID> --token-no <TOKEN_ID>

    # Search for active CS2 markets on Polymarket
    python main.py --search-markets

    # Run with demo/simulation data (no live match needed)
    python main.py --demo
"""

import argparse
import asyncio
import signal
import sys
import time

import config
from hltv_tracker import HLTVTracker, MatchState
from market_tracker import MarketTracker, MarketState, search_cs2_markets
from delay_analyzer import DelayAnalyzer
from dashboard import Dashboard


async def run_search():
    """Search for active CS2 markets on Polymarket."""
    print("Searching Polymarket for CS2 markets...\n")
    markets = await search_cs2_markets()
    if not markets:
        print("No active CS2 markets found.")
        print("Try browsing: https://polymarket.com/sports/counter-strike/games")
        return

    print(f"Found {len(markets)} CS2 markets:\n")
    for m in markets:
        tokens = m.get("tokens", [])
        token_str = f"  YES: {tokens[0]}\n  NO:  {tokens[1]}" if len(tokens) >= 2 else "  tokens: N/A"
        print(f"  [{m['slug']}]")
        print(f"  {m['question']}")
        print(f"  Volume: ${float(m.get('volume', 0)):,.0f}  |  Liquidity: ${float(m.get('liquidity', 0)):,.0f}")
        print(token_str)
        print()


async def run_monitor(match_id: str, token_yes: str, token_no: str, market_slug: str):
    """Main monitoring loop: HLTV + Polymarket + Dashboard."""
    analyzer = DelayAnalyzer()

    # Shared state references
    hltv = HLTVTracker(match_id=match_id)
    market = MarketTracker(
        token_yes=token_yes,
        token_no=token_no,
        market_slug=market_slug,
    )
    dash = Dashboard(
        match_state=hltv.state,
        market_state=market.state,
        analyzer=analyzer,
    )

    # Wire up callbacks
    async def on_hltv_event(event_name: str, log_entry: dict, state: MatchState):
        dash.add_event(f"HLTV: {event_name} | {state.team1_score}-{state.team2_score}")
        if event_name == "roundEnd":
            analyzer.on_round_end(
                round_num=state.current_round,
                score=f"{state.team1_score}-{state.team2_score}",
                winner=log_entry.get("data", {}).get("winner", ""),
                ts_ms=log_entry["ts_ms"],
            )

    async def on_price_update(log_entry: dict, state: MarketState):
        analyzer.on_price_update(state.mid_yes, log_entry["ts_ms"])

    hltv.on_event = on_hltv_event
    market.on_price = on_price_update

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def handle_signal():
        print("\n[SHUTDOWN] Stopping...")
        hltv.stop()
        market.stop()
        dash.stop()
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    print(f"Starting monitor: HLTV match {match_id}")
    print(f"Market: {market_slug or 'custom tokens'}")
    print("Press Ctrl+C to stop.\n")

    # Run all three concurrently
    tasks = [
        asyncio.create_task(hltv.connect()),
        asyncio.create_task(market.start_polling()),
        asyncio.create_task(dash.run()),
    ]

    try:
        await shutdown_event.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # Print final stats
    stats = analyzer.stats
    print("\n=== FINAL DELAY ANALYSIS ===")
    print(f"  Reactions measured: {stats['count']}")
    if stats['count'] > 0:
        print(f"  Average delay:     {stats['avg_delay_ms']:.0f} ms")
        print(f"  Median delay:      {stats['median_delay_ms']:.0f} ms")
        print(f"  Min delay:         {stats['min_delay_ms']:.0f} ms")
        print(f"  Max delay:         {stats['max_delay_ms']:.0f} ms")
    print(f"  Timed out:         {stats['timed_out']}")
    print(f"\nLogs saved to {config.DATA_DIR}/")


async def run_demo(duration_s: int = 0):
    """Run with simulated data to test the dashboard and analyzer.

    Args:
        duration_s: Auto-stop after this many seconds (0 = run until Ctrl+C or match ends).
    """
    import random

    analyzer = DelayAnalyzer()
    match_state = MatchState(
        match_id="DEMO-12345",
        team1_name="NAVI",
        team2_name="FaZe",
        team1_side="CT",
        team2_side="T",
        map_name="de_mirage",
    )
    market_state = MarketState(
        market_slug="demo-market",
        mid_yes=0.55,
        best_bid_yes=0.54,
        best_ask_yes=0.56,
        spread_yes=0.02,
        mid_no=0.45,
        last_update_ms=int(time.time() * 1000),
    )

    dash = Dashboard(match_state, market_state, analyzer)
    dash.add_event("DEMO MODE — simulating match events")

    async def simulate_match():
        round_num = 0
        while dash._running:
            await asyncio.sleep(random.uniform(0.8, 2.0))
            round_num += 1
            match_state.current_round = round_num
            match_state.round_phase = "live"
            match_state.team1_alive = 5
            match_state.team2_alive = 5
            dash.add_event(f"Round {round_num} started")

            # Simulate kills
            for _ in range(random.randint(2, 6)):
                await asyncio.sleep(random.uniform(0.1, 0.4))
                if random.random() > 0.5:
                    match_state.team2_alive = max(0, match_state.team2_alive - 1)
                else:
                    match_state.team1_alive = max(0, match_state.team1_alive - 1)

            # Round end
            await asyncio.sleep(random.uniform(0.2, 0.5))
            winner = random.choice(["team1", "team2"])
            if winner == "team1":
                match_state.team1_score += 1
            else:
                match_state.team2_score += 1
            match_state.round_phase = "over"

            ts_now = int(time.time() * 1000)
            analyzer.on_round_end(round_num, f"{match_state.team1_score}-{match_state.team2_score}", winner, ts_now)
            dash.add_event(f"Round {round_num} won by {winner} → {match_state.team1_score}-{match_state.team2_score}")

            # Simulate delayed market reaction (1-8 seconds in demo)
            delay_ms = random.randint(1000, 8000)
            await asyncio.sleep(delay_ms / 1000)
            price_shift = random.uniform(0.01, 0.05) * (1 if winner == "team1" else -1)
            market_state.mid_yes = max(0.01, min(0.99, market_state.mid_yes + price_shift))
            market_state.best_bid_yes = market_state.mid_yes - 0.01
            market_state.best_ask_yes = market_state.mid_yes + 0.01
            market_state.mid_no = 1.0 - market_state.mid_yes
            market_state.last_update_ms = int(time.time() * 1000)
            analyzer.on_price_update(market_state.mid_yes, market_state.last_update_ms)
            dash.add_event(f"Market reacted: YES={market_state.mid_yes:.3f} (delay ~{delay_ms}ms)")

            if match_state.team1_score >= 13 or match_state.team2_score >= 13:
                dash.add_event("MATCH OVER")
                break

    shutdown = asyncio.Event()

    def handle_signal():
        dash.stop()
        shutdown.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    async def auto_stop():
        if duration_s > 0:
            await asyncio.sleep(duration_s)
            handle_signal()

    tasks = [
        asyncio.create_task(dash.run()),
        asyncio.create_task(simulate_match()),
        asyncio.create_task(auto_stop()),
    ]

    try:
        await shutdown.wait()
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    stats = analyzer.stats
    print("\n=== DEMO DELAY STATS ===")
    print(f"  Reactions: {stats['count']}")
    if stats['count'] > 0:
        print(f"  Avg delay: {stats['avg_delay_ms']:.0f} ms")
        print(f"  Median:    {stats['median_delay_ms']:.0f} ms")


def main():
    parser = argparse.ArgumentParser(description="CS2 Arbitrage Monitor — Phase 1")
    parser.add_argument("--match-id", help="HLTV match list ID")
    parser.add_argument("--token-yes", help="Polymarket CLOB token ID for YES outcome")
    parser.add_argument("--token-no", help="Polymarket CLOB token ID for NO outcome")
    parser.add_argument("--market-slug", default="", help="Polymarket market slug (for display)")
    parser.add_argument("--search-markets", action="store_true", help="Search for active CS2 markets")
    parser.add_argument("--demo", action="store_true", help="Run demo with simulated data")
    parser.add_argument("--demo-duration", type=int, default=0, help="Auto-stop demo after N seconds (0=manual)")
    args = parser.parse_args()

    if args.search_markets:
        asyncio.run(run_search())
        return

    if args.demo:
        asyncio.run(run_demo(duration_s=args.demo_duration))
        return

    # Resolve from args or env
    match_id = args.match_id or config.HLTV_MATCH_ID
    token_yes = args.token_yes or config.POLYMARKET_CLOB_TOKEN_YES
    token_no = args.token_no or config.POLYMARKET_CLOB_TOKEN_NO
    market_slug = args.market_slug or config.POLYMARKET_MARKET_SLUG

    if not match_id:
        parser.error("--match-id is required (or set HLTV_MATCH_ID in .env)")
    if not token_yes:
        parser.error("--token-yes is required (or set POLYMARKET_CLOB_TOKEN_YES in .env)")

    asyncio.run(run_monitor(match_id, token_yes, token_no or "", market_slug))


if __name__ == "__main__":
    main()
