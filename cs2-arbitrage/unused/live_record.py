#!/usr/bin/env python3
"""
Live Match Recorder

Records both HLTV scorebot events and Polymarket prices simultaneously
during a live CS2 match. Saves everything with ms-precision timestamps
from the same clock so we can measure the exact delay.

Usage:
    # Step 1: Find the scorebot list ID for your match
    python live_record.py --lookup 2390814

    # Step 2: Record a live match
    python live_record.py --list-id <SCOREBOT_LIST_ID> --token-yes <POLYMARKET_TOKEN>

    # Step 3: After the match, generate the overlay chart
    python live_record.py --chart <output_dir>
"""

import argparse
import asyncio
import json
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from event_log import append_event


async def lookup_match(match_id: str):
    """Look up the scorebot list ID for a match."""
    from hltv_utils import get_scorebot_info

    print(f"Looking up HLTV match {match_id}...")
    try:
        info = await get_scorebot_info(match_id)
        print(f"\n  Team 1:       {info['team1'] or '(unknown)'}")
        print(f"  Team 2:       {info['team2'] or '(unknown)'}")
        print(f"  List ID:      {info['list_id'] or '(not found — match may not be live yet)'}")
        print(f"  Scorebot URL: {info['scorebot_url'] or '(not found)'}")
        if info['list_id']:
            print(f"\n  Run: python live_record.py --list-id {info['list_id']} --token-yes <TOKEN>")
    except Exception as e:
        print(f"  Error: {e}")
        print("  HLTV may be blocking automated requests. Try during a live match.")


async def record_match(list_id: str, token_yes: str, output_dir: Path):
    """Record HLTV events and Polymarket prices simultaneously."""
    from hltv_tracker import HLTVTracker
    from market_tracker import MarketTracker

    output_dir.mkdir(parents=True, exist_ok=True)
    hltv_log = output_dir / "hltv_events.jsonl"
    market_log = output_dir / "market_prices.jsonl"
    combined_log = output_dir / "combined_events.jsonl"

    print(f"Recording to: {output_dir}")
    print(f"HLTV list ID: {list_id}")
    print(f"Polymarket token: {token_yes[:20]}...")
    print(f"Started at: {datetime.now(timezone.utc).isoformat()}")
    print("Press Ctrl+C to stop.\n")

    # Track state for terminal display
    round_count = 0
    price_count = 0
    last_score = "0-0"
    last_price = 0.0

    async def on_hltv_event(event_name, log_entry, state):
        nonlocal round_count, last_score
        ts = log_entry["ts_ms"]

        # Write to HLTV-specific log
        append_event(hltv_log, log_entry)

        # Write to combined log
        append_event(combined_log, {
            "source": "hltv",
            "ts_ms": ts,
            "event": event_name,
            "score": f"{state.team1_score}-{state.team2_score}",
            "team1": state.team1_name,
            "team2": state.team2_name,
            "team1_side": state.team1_side,
            "team2_side": state.team2_side,
            "round": state.current_round,
            "data": log_entry.get("data"),
        })

        if event_name == "roundEnd":
            round_count += 1
            last_score = f"{state.team1_score}-{state.team2_score}"
            t = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            print(f"  [HLTV {t}] Round end → {state.team1_name} {last_score} {state.team2_name}")
        elif event_name == "scoreboard" and state.team1_name:
            pass  # Scoreboard updates are frequent, don't spam
        elif event_name in ("kill", "bombPlanted", "bombDefused", "roundStart"):
            t = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            detail = ""
            if event_name == "kill" and isinstance(log_entry.get("data"), dict):
                d = log_entry["data"]
                detail = f" {d.get('killerNick', '?')} → {d.get('victimNick', '?')}"
            print(f"  [HLTV {t}] {event_name}{detail}")

    async def on_price(log_entry, state):
        nonlocal price_count, last_price
        ts = log_entry["ts_ms"]
        price_count += 1

        append_event(market_log, log_entry)

        # Only write to combined log when price changes
        if abs(state.mid_yes - last_price) > 0.001:
            append_event(combined_log, {
                "source": "polymarket",
                "ts_ms": ts,
                "mid_yes": state.mid_yes,
                "best_bid": state.best_bid_yes,
                "best_ask": state.best_ask_yes,
                "spread": state.spread_yes,
            })
            t = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            print(f"  [MRKT {t}] Price: {state.mid_yes:.4f} (bid {state.best_bid_yes:.3f} / ask {state.best_ask_yes:.3f})")
            last_price = state.mid_yes

    hltv = HLTVTracker(match_id=list_id, on_event=on_hltv_event)
    market = MarketTracker(token_yes=token_yes, token_no="", on_price=on_price)

    shutdown = asyncio.Event()

    def handle_signal():
        print("\n\nStopping recording...")
        hltv.stop()
        market.stop()
        shutdown.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    tasks = [
        asyncio.create_task(hltv.connect()),
        asyncio.create_task(market.start_polling()),
    ]

    # Status printer
    async def print_status():
        while not shutdown.is_set():
            await asyncio.sleep(30)
            print(f"  --- Status: {round_count} rounds, {price_count} price polls, score {last_score}, price {last_price:.4f} ---")

    tasks.append(asyncio.create_task(print_status()))

    try:
        await shutdown.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    print(f"\nRecording complete!")
    print(f"  HLTV events:   {hltv_log}")
    print(f"  Market prices: {market_log}")
    print(f"  Combined log:  {combined_log}")
    print(f"  Rounds seen:   {round_count}")
    print(f"  Price polls:   {price_count}")


def generate_chart(output_dir: Path):
    """Generate an overlay chart from recorded data."""
    combined_log = output_dir / "combined_events.jsonl"
    if not combined_log.exists():
        print(f"No combined log found at {combined_log}")
        return

    events = []
    with open(combined_log) as f:
        for line in f:
            events.append(json.loads(line))

    # Separate into HLTV round ends and price updates
    round_ends = [e for e in events if e["source"] == "hltv" and e["event"] == "roundEnd"]
    prices = [e for e in events if e["source"] == "polymarket"]

    print(f"Loaded {len(round_ends)} round ends and {len(prices)} price updates")

    if not round_ends or not prices:
        print("Not enough data to generate chart.")
        return

    # Calculate delays: for each round end, find when price first moved significantly
    print("\n=== DELAY ANALYSIS ===")
    print(f"{'Round':<8} {'Score':<10} {'HLTV Time':<15} {'Price Move At':<15} {'Delay (ms)':<12} {'Price Δ':<10}")
    print("-" * 70)

    for re_evt in round_ends:
        re_ts = re_evt["ts_ms"]
        score = re_evt.get("score", "?")

        # Get price at round end time
        price_at_round = None
        for p in prices:
            if p["ts_ms"] <= re_ts:
                price_at_round = p["mid_yes"]
            else:
                break

        if price_at_round is None:
            continue

        # Find first significant price move after round end
        reaction_ts = None
        reaction_price = None
        for p in prices:
            if p["ts_ms"] > re_ts:
                if abs(p["mid_yes"] - price_at_round) >= 0.005:
                    reaction_ts = p["ts_ms"]
                    reaction_price = p["mid_yes"]
                    break

        re_time = datetime.fromtimestamp(re_ts / 1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]

        if reaction_ts:
            delay = reaction_ts - re_ts
            r_time = datetime.fromtimestamp(reaction_ts / 1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            delta = reaction_price - price_at_round
            print(f"{re_evt.get('round', '?'):<8} {score:<10} {re_time:<15} {r_time:<15} {delay:<12} {delta:<+10.4f}")
        else:
            print(f"{re_evt.get('round', '?'):<8} {score:<10} {re_time:<15} {'(no move)':<15} {'—':<12} {'—':<10}")


def main():
    parser = argparse.ArgumentParser(description="Live CS2 Match Recorder")
    parser.add_argument("--lookup", metavar="MATCH_ID", help="Look up scorebot list ID for an HLTV match")
    parser.add_argument("--list-id", help="HLTV scorebot list ID")
    parser.add_argument("--token-yes", help="Polymarket CLOB token ID for YES outcome")
    parser.add_argument("--output", default=None, help="Output directory for logs (default: data/live_<timestamp>)")
    parser.add_argument("--chart", metavar="OUTPUT_DIR", help="Generate chart from recorded data")
    args = parser.parse_args()

    if args.lookup:
        asyncio.run(lookup_match(args.lookup))
        return

    if args.chart:
        generate_chart(Path(args.chart))
        return

    if not args.list_id:
        parser.error("--list-id required. Use --lookup <MATCH_ID> first to find it.")
    if not args.token_yes:
        token_yes = config.POLYMARKET_CLOB_TOKEN_YES
        if not token_yes:
            parser.error("--token-yes required (or set POLYMARKET_CLOB_TOKEN_YES in .env)")
    else:
        token_yes = args.token_yes

    if args.output:
        output_dir = Path(args.output)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = config.DATA_DIR / f"live_{ts}"

    asyncio.run(record_match(args.list_id, token_yes, output_dir))


if __name__ == "__main__":
    main()
