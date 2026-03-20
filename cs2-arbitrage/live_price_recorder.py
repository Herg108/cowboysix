"""
Live Polymarket Price Recorder
Polls the CLOB API and appends each price tick to a JSONL file.

Usage:
    python live_price_recorder.py --team1-token <TOKEN> --team2-token <TOKEN> --team1-name FaZe --team2-name TYLOO --output data/match_name

Presets (no tokens needed):
    python live_price_recorder.py --preset faze-tyloo-bo3 --output data/faze_tyloo_bo3
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

import json as json_mod

import httpx
import orjson

CLOB_URL = "https://clob.polymarket.com"

PRESETS = {
    "faze-tyloo-map2": {
        "team1_name": "FaZe", "team1_token": "55046853534947028598505959313963401759497259770850839813845827905720358461966",
        "team2_name": "TYLOO", "team2_token": "99581201002210600666086497586667312769959469460130142942401507502819616238518",
    },
    "faze-tyloo-bo3": {
        "team1_name": "FaZe", "team1_token": "52253849497190465038442463410087453126454104709115542677667998979089556591627",
        "team2_name": "TYLOO", "team2_token": "101958465723685744167137796657899098904530951998538288108427147101804003806944",
    },
}

POLL_INTERVAL = 0.5  # seconds


def append_jsonl(filepath: Path, obj: dict):
    line = orjson.dumps(obj) + b"\n"
    with open(filepath, "ab") as f:
        f.write(line)


async def poll_price(client: httpx.AsyncClient, token_id: str) -> dict | None:
    try:
        resp = await client.get(
            f"{CLOB_URL}/book",
            params={"token_id": token_id},
        )
        resp.raise_for_status()
        book = resp.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        # API returns bids ascending, asks descending — best bid is last, best ask is last
        best_bid = float(bids[-1]["price"]) if bids else None
        best_ask = float(asks[-1]["price"]) if asks else None
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else None
        spread = (best_ask - best_bid) if best_bid and best_ask else None
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread": spread,
            "bid_depth": len(bids),
            "ask_depth": len(asks),
        }
    except Exception as e:
        print(f"  [ERROR] {e}")
        return None


def load_hltv_events(chartfile: Path, records: list = None):
    """Load HLTV events from the parent directory, filtered to the map
    that overlaps in time with our price data.

    Chart is at: data/<date>/<match>/map1/live_chart.html
    HLTV file is at: data/<date>/<match>/hltv_events.jsonl

    Instead of matching by map number (which breaks if we started recording
    mid-match), we match by time: find the HLTV map whose events overlap
    with our price data timestamps.
    """
    hltv_file = chartfile.parent.parent / "hltv_events.jsonl"
    if not hltv_file.exists():
        return []

    # Read all events
    all_events = []
    with open(hltv_file) as f:
        for line in f:
            line = line.strip()
            if line:
                all_events.append(json_mod.loads(line))

    if not all_events:
        return []

    # Get the time range of our price data
    if records and len(records) > 1:
        price_start = records[0].get("ts_ms", 0)
        price_end = records[-1].get("ts_ms", 0)
    else:
        # Fallback: read from the market_prices.jsonl in the same folder
        prices_file = chartfile.parent / "market_prices.jsonl"
        if prices_file.exists():
            lines = prices_file.read_text().strip().split("\n")
            if lines:
                price_start = json_mod.loads(lines[0]).get("ts_ms", 0)
                price_end = json_mod.loads(lines[-1]).get("ts_ms", 0)
            else:
                price_start = price_end = 0
        else:
            price_start = price_end = 0

    # Get round_end events and all events with a map field (for time overlap)
    maps_all = {}  # map_name -> list of all events with that map
    round_ends = []  # all round_end events
    for e in all_events:
        map_name = e.get("map")
        if map_name:
            if map_name not in maps_all:
                maps_all[map_name] = []
            maps_all[map_name].append(e)
        if e.get("type") == "round_end":
            round_ends.append(e)

    # Find the map with the most time overlap with our price data
    best_map = None
    best_overlap = 0
    for map_name, evts in maps_all.items():
        map_start = evts[0].get("ts_ms", 0)
        map_end = evts[-1].get("ts_ms", 0)
        overlap_start = max(price_start, map_start)
        overlap_end = min(price_end, map_end)
        overlap = max(0, overlap_end - overlap_start)
        if overlap > best_overlap:
            best_overlap = overlap
            best_map = map_name

    if best_map is None:
        return []

    # Get time range of the best map to filter round_ends
    map_evts = maps_all[best_map]
    map_start = map_evts[0].get("ts_ms", 0)
    map_end = map_evts[-1].get("ts_ms", 0)

    # Return round_end events within the map's time range
    events = []
    for e in round_ends:
        ts = e.get("ts_ms", 0)
        if map_start <= ts <= map_end:
            events.append(e)
    return events


def write_chart(chartfile: Path, records: list, team1_name: str, team2_name: str):
    t1 = team1_name.lower()
    t2 = team2_name.lower()
    timestamps = [r["ts_iso"] for r in records]
    t1_mid = [r.get(f"{t1}_mid") for r in records]
    t1_bid = [r.get(f"{t1}_bid") for r in records]
    t1_ask = [r.get(f"{t1}_ask") for r in records]
    t2_mid = [r.get(f"{t2}_mid") for r in records]

    last_t1 = next((v for v in reversed(t1_mid) if v is not None), 0)
    last_t2 = next((v for v in reversed(t2_mid) if v is not None), 0)

    # Load HLTV round events for score markers
    hltv_events = load_hltv_events(chartfile, records)
    score_markers_js = ""
    score_status = ""

    if hltv_events:
        last_ev = hltv_events[-1]
        ct_s = last_ev.get("ct_score", 0)
        t_s = last_ev.get("t_score", 0)
        score_status = f" | Round ends: {len(hltv_events)}"

        # Build vertical lines, score annotations, and marker points on the price curve
        shapes = []
        annotations = []
        marker_x = []
        marker_y = []
        marker_text = []
        for ev in hltv_events:
            ts = ev.get("ts_iso", "")
            ct_score = ev.get("ct_score", 0)
            t_score = ev.get("t_score", 0)
            winner = ev.get("winner", "?")
            win_type = ev.get("win_type", "?").replace("_", " ")

            # Vertical line
            shapes.append(f"""{{
                type: 'line', x0: '{ts}', x1: '{ts}', y0: 0, y1: 1,
                line: {{color: 'rgba(255,255,0,0.3)', width: 1, dash: 'dot'}}
            }}""")

            # Score label at top
            annotations.append(f"""{{
                x: '{ts}', y: 1.03, xref: 'x', yref: 'y',
                text: '{ct_score}-{t_score}', showarrow: false,
                font: {{color: '#ffdd57', size: 10}}
            }}""")

            # Find the price at this timestamp to place marker on the line
            price_at_ts = None
            for r in records:
                if r["ts_iso"] >= ts:
                    price_at_ts = r.get(f"{t1}_mid")
                    break
            if price_at_ts is not None:
                marker_x.append(ts)
                marker_y.append(price_at_ts)
                marker_text.append(f"R end: CT {ct_score}-{t_score} T<br>{win_type}")

        shapes_str = ",\n".join(shapes)
        annots_str = ",\n".join(annotations)
        score_markers_js = f"shapes: [{shapes_str}],\n  annotations: [{annots_str}],"

    html = f"""<!DOCTYPE html>
<html><head>
<title>{team1_name} vs {team2_name} — Live</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  body {{ background: #1a1a2e; color: #eee; font-family: 'Segoe UI', sans-serif; margin: 0; padding: 20px; }}
  h1 {{ text-align: center; color: #e94560; margin-bottom: 5px; }}
  .info {{ text-align: center; color: #999; margin-bottom: 10px; }}
  #chart {{ width: 100%; height: 80vh; }}
</style>
</head><body>
<h1>{team1_name} vs {team2_name} — Live Prices</h1>
<div class="info">{len(records)} pts | {timestamps[-1]} UTC | {team1_name}: {last_t1*100:.1f}% | {team2_name}: {last_t2*100:.1f}%{score_status} | <b>Reload to refresh</b></div>
<div id="chart"></div>
<script>
Plotly.newPlot('chart', [
  {{x: {json_mod.dumps(timestamps)}, y: {json_mod.dumps(t1_mid)}, name: '{team1_name}', line: {{color: '#e94560', width: 2}}}},
  {{x: {json_mod.dumps(timestamps)}, y: {json_mod.dumps(t2_mid)}, name: '{team2_name}', line: {{color: '#0f3460', width: 2}}}},
  {{x: {json_mod.dumps(timestamps)}, y: {json_mod.dumps(t1_bid)}, name: 'Bid', line: {{color: '#e94560', width: 1, dash: 'dot'}}, showlegend: false}},
  {{x: {json_mod.dumps(timestamps)}, y: {json_mod.dumps(t1_ask)}, name: 'Ask', line: {{color: '#e94560', width: 1, dash: 'dot'}}, fill: 'tonexty', fillcolor: 'rgba(233,69,96,0.1)', showlegend: false}},
  {{x: {json_mod.dumps(marker_x if hltv_events else [])}, y: {json_mod.dumps(marker_y if hltv_events else [])}, text: {json_mod.dumps(marker_text if hltv_events else [])}, name: 'Round End', mode: 'markers', marker: {{color: '#ffdd57', size: 10, symbol: 'diamond'}}, hovertemplate: '%{{text}}<extra></extra>'}}
], {{
  paper_bgcolor: '#1a1a2e', plot_bgcolor: '#16213e', font: {{color: '#eee'}},
  xaxis: {{title: 'Time (UTC)', gridcolor: '#333'}},
  yaxis: {{title: 'Win Probability', gridcolor: '#333', range: [0, 1.05], tickformat: '.0%'}},
  legend: {{x: 0.01, y: 0.99}}, hovermode: 'x unified', margin: {{t: 40, b: 80}},
  {score_markers_js}
}}, {{responsive: true}});
</script>
</body></html>"""
    chartfile.write_text(html)


async def main(output_dir: str, team1_name: str, team1_token: str, team2_name: str, team2_token: str):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    logfile = out / "market_prices.jsonl"

    chartfile = out / "live_chart.html"

    print(f"Recording prices to: {logfile}")
    print(f"Live chart at: {chartfile}")
    print(f"Polling every {POLL_INTERVAL}s")
    print(f"{team1_name} token: {team1_token[:20]}...")
    print(f"{team2_name} token: {team2_token[:20]}...")
    print("Press Ctrl+C to stop.\n")

    # Load existing records if resuming
    all_records = []
    if logfile.exists():
        with open(logfile) as f:
            for line in f:
                line = line.strip()
                if line:
                    all_records.append(json_mod.loads(line))
        print(f"Loaded {len(all_records)} existing records")

    tick = 0
    last_t1 = None  # carry forward last known prices
    last_t2 = None
    na_streak_start = None  # track consecutive N/A responses
    NA_TIMEOUT = 30  # seconds of consecutive N/A before auto-stop
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            ts_ms = int(time.time() * 1000)

            t1_raw, t2_raw = await asyncio.gather(
                poll_price(client, team1_token),
                poll_price(client, team2_token),
            )

            # Track consecutive N/A streak
            if t1_raw is None and t2_raw is None:
                if na_streak_start is None:
                    na_streak_start = time.time()
                elif time.time() - na_streak_start >= NA_TIMEOUT:
                    print(f"\n[DONE] Market empty for {NA_TIMEOUT}s — market resolved. Stopping.")
                    write_chart(chartfile, all_records, team1_name, team2_name)
                    break
            else:
                na_streak_start = None

            # If API returns None, use last known values
            t1_data = t1_raw if t1_raw is not None else last_t1
            t2_data = t2_raw if t2_raw is not None else last_t2

            if t1_raw is not None:
                last_t1 = t1_raw
            if t2_raw is not None:
                last_t2 = t2_raw

            entry = {
                "ts_ms": ts_ms,
                "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts_ms / 1000)),
                f"{team1_name.lower()}_mid": t1_data["mid"] if t1_data else None,
                f"{team1_name.lower()}_bid": t1_data["best_bid"] if t1_data else None,
                f"{team1_name.lower()}_ask": t1_data["best_ask"] if t1_data else None,
                f"{team1_name.lower()}_spread": t1_data["spread"] if t1_data else None,
                f"{team2_name.lower()}_mid": t2_data["mid"] if t2_data else None,
                f"{team2_name.lower()}_bid": t2_data["best_bid"] if t2_data else None,
                f"{team2_name.lower()}_ask": t2_data["best_ask"] if t2_data else None,
                f"{team2_name.lower()}_spread": t2_data["spread"] if t2_data else None,
            }

            append_jsonl(logfile, entry)
            all_records.append(entry)

            # Regenerate HTML chart every 10 ticks (~5 seconds)
            if tick % 3 == 0 and len(all_records) > 1:
                write_chart(chartfile, all_records, team1_name, team2_name)

            tick += 1
            if tick % 5 == 0:
                t1_str = f"{t1_mid:.3f}" if t1_mid is not None else "N/A"
                t2_str = f"{t2_mid:.3f}" if t2_mid is not None else "N/A"
                print(f"[{entry['ts_iso']}] {team1_name}: {t1_str}  |  {team2_name}: {t2_str}")

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Record live Polymarket prices for a CS2 match")
    parser.add_argument("--output", default="data/live", help="Output directory")
    parser.add_argument("--preset", help="Use a preset (e.g. faze-tyloo-bo3, faze-tyloo-map2)")
    parser.add_argument("--team1-name", help="Team 1 name")
    parser.add_argument("--team1-token", help="Team 1 CLOB token ID")
    parser.add_argument("--team2-name", help="Team 2 name")
    parser.add_argument("--team2-token", help="Team 2 CLOB token ID")
    args = parser.parse_args()

    if args.preset:
        if args.preset not in PRESETS:
            print(f"Unknown preset: {args.preset}")
            print(f"Available: {', '.join(PRESETS.keys())}")
            exit(1)
        p = PRESETS[args.preset]
        t1_name, t1_token = p["team1_name"], p["team1_token"]
        t2_name, t2_token = p["team2_name"], p["team2_token"]
    elif args.team1_token and args.team2_token:
        t1_name = args.team1_name or "Team1"
        t1_token = args.team1_token
        t2_name = args.team2_name or "Team2"
        t2_token = args.team2_token
    else:
        print("Provide either --preset or --team1-token + --team2-token")
        exit(1)

    try:
        asyncio.run(main(args.output, t1_name, t1_token, t2_name, t2_token))
    except KeyboardInterrupt:
        print("\nStopped. Data saved.")
