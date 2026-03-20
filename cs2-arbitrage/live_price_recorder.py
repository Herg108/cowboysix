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
    """Load HLTV events from the same directory as the chart.

    Chart is at: data/<date>/<match>/map1/live_chart.html
    HLTV file is at: data/<date>/<match>/map1/hltv_events.jsonl
    """
    hltv_file = chartfile.parent / "hltv_events.jsonl"
    if not hltv_file.exists():
        return []

    all_events = []
    with open(hltv_file) as f:
        for line in f:
            line = line.strip()
            if line:
                all_events.append(json_mod.loads(line))

    if not all_events:
        return []

    # Scoreboards for resolving CT/T to team names
    scoreboards = [e for e in all_events if e.get("type") == "scoreboard"]

    def resolve_side(ts, side):
        """Map 'CT'/'TERRORIST' to team name using nearest scoreboard."""
        for s in scoreboards:
            if abs(s["ts_ms"] - ts) < 10000:
                if side == "CT":
                    return s.get("ct_side", "?")
                else:
                    return s.get("t_side", "?")
        return "?"

    events = []
    for e in all_events:
        etype = e.get("type", "")
        ts = e.get("ts_ms", 0)

        if etype == "round_end":
            e["winner_team"] = resolve_side(ts, e.get("winner", ""))
            events.append(e)
        elif etype == "round_start":
            events.append(e)
        elif etype == "scoreboard" and e.get("event") == "round_update":
            # Round number changed but score didn't — this is a round start
            e["type"] = "round_start"
            events.append(e)
        elif etype == "kill" and e.get("first_kill"):
            # Use killer_team directly if available, fall back to resolve_side
            if not e.get("killer_team") or e["killer_team"] == "?":
                e["killer_team"] = resolve_side(ts, e.get("killer_side", ""))
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
        round_ends = [e for e in hltv_events if e["type"] == "round_end"]
        first_kills = [e for e in hltv_events if e["type"] == "kill"]
        round_starts = [e for e in hltv_events if e["type"] == "round_start"]
        score_status = f" | Events: {len(round_ends)} rounds, {len(first_kills)} first kills"

        def is_team1(name):
            return (name.lower() in team1_name.lower()) or (team1_name.lower() in name.lower())

        def find_price(ts):
            for r in records:
                if r["ts_iso"] >= ts:
                    return r.get(f"{t1}_mid")
            return None

        shapes = []
        annotations = []

        # Round end markers
        re_x, re_y, re_text, re_colors = [], [], [], []
        for ev in round_ends:
            ts = ev.get("ts_iso", "")
            ct_score = ev.get("ct_score", 0)
            t_score = ev.get("t_score", 0)
            winner_team = ev.get("winner_team", "?")
            win_type = ev.get("win_type", "?").replace("_", " ")
            t1_win = is_team1(winner_team)
            color = "rgba(0,255,100,0.4)" if t1_win else "rgba(255,60,60,0.4)"
            mc = "#00ff64" if t1_win else "#ff3c3c"

            shapes.append(f"""{{type:'line',x0:'{ts}',x1:'{ts}',y0:0,y1:1,line:{{color:'{color}',width:1,dash:'dot'}}}}""")
            annotations.append(f"""{{x:'{ts}',y:1.03,xref:'x',yref:'y',text:'{ct_score}-{t_score}',showarrow:false,font:{{color:'{mc}',size:10}}}}""")

            price = find_price(ts)
            if price is not None:
                re_x.append(ts); re_y.append(price)
                re_text.append(f"{winner_team} wins<br>CT {ct_score}-{t_score} T<br>{win_type}")
                re_colors.append(mc)

        # Round start markers
        rs_x, rs_y = [], []
        for ev in round_starts:
            ts = ev.get("ts_iso", "")
            price = find_price(ts)
            if price is not None:
                rs_x.append(ts); rs_y.append(price)
                shapes.append(f"""{{type:'line',x0:'{ts}',x1:'{ts}',y0:0,y1:1,line:{{color:'rgba(255,255,0,0.4)',width:1,dash:'dash'}}}}""")

        # First kill markers
        fk_x, fk_y, fk_text, fk_colors = [], [], [], []
        for ev in first_kills:
            ts = ev.get("ts_iso", "")
            killer = ev.get("killer", "?")
            victim = ev.get("victim", "?")
            weapon = ev.get("weapon", "?")
            hs = " (HS)" if ev.get("headshot") else ""
            killer_team = ev.get("killer_team", "?")
            t1_kill = is_team1(killer_team)
            price = find_price(ts)
            if price is not None:
                fk_x.append(ts); fk_y.append(price)
                fk_text.append(f"First Kill: {killer} → {victim}<br>{weapon}{hs}")
                fk_colors.append("#00ff64" if t1_kill else "#ff3c3c")

        shapes_str = ",\n".join(shapes)
        annots_str = ",\n".join(annotations)
        score_markers_js = f"shapes: [{shapes_str}],\n  annotations: [{annots_str}],"

    html = f"""<!DOCTYPE html>
<html><head>
<title>{team1_name} vs {team2_name} - Live</title>

<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  body {{ background: #1a1a2e; color: #eee; font-family: 'Segoe UI', sans-serif; margin: 0; padding: 20px; }}
  h1 {{ text-align: center; color: #e94560; margin-bottom: 5px; }}
  .info {{ text-align: center; color: #999; margin-bottom: 10px; }}
  #chart {{ width: 100%; height: 80vh; }}
</style>
</head><body>
<h1>{team1_name} vs {team2_name} - Live Prices</h1>
<div class="info">{len(records)} pts | {timestamps[-1]} UTC | {team1_name}: {last_t1*100:.1f}% | {team2_name}: {last_t2*100:.1f}%{score_status} | <b>Reload to refresh</b></div>
<div id="chart"></div>
<script>
Plotly.newPlot('chart', [
  {{x: {json_mod.dumps(timestamps)}, y: {json_mod.dumps(t1_mid)}, name: '{team1_name}', line: {{color: '#00d4ff', width: 2}}}},
  {{x: {json_mod.dumps(timestamps)}, y: {json_mod.dumps(t2_mid)}, name: '{team2_name}', line: {{color: '#ff9f43', width: 2}}}},
  {{x: {json_mod.dumps(timestamps)}, y: {json_mod.dumps(t1_bid)}, name: 'Bid', line: {{color: '#00d4ff', width: 1, dash: 'dot'}}, showlegend: false}},
  {{x: {json_mod.dumps(timestamps)}, y: {json_mod.dumps(t1_ask)}, name: 'Ask', line: {{color: '#00d4ff', width: 1, dash: 'dot'}}, fill: 'tonexty', fillcolor: 'rgba(0,212,255,0.1)', showlegend: false}},
  {{x: {json_mod.dumps(re_x if hltv_events else [])}, y: {json_mod.dumps(re_y if hltv_events else [])}, text: {json_mod.dumps(re_text if hltv_events else [])}, name: 'Round End', mode: 'markers', marker: {{color: {json_mod.dumps(re_colors if hltv_events else [])}, size: 10, symbol: 'diamond'}}, hovertemplate: '%{{text}}<extra></extra>'}},
  {{x: {json_mod.dumps(rs_x if hltv_events else [])}, y: {json_mod.dumps(rs_y if hltv_events else [])}, name: 'Round Start', mode: 'markers', marker: {{color: '#ffff00', size: 8, symbol: 'triangle-up'}}, hovertemplate: 'Round Start<extra></extra>'}},
  {{x: {json_mod.dumps(fk_x if hltv_events else [])}, y: {json_mod.dumps(fk_y if hltv_events else [])}, text: {json_mod.dumps(fk_text if hltv_events else [])}, name: 'First Kill', mode: 'markers', marker: {{color: {json_mod.dumps(fk_colors if hltv_events else [])}, size: 7, symbol: 'x'}}, hovertemplate: '%{{text}}<extra></extra>'}},
], {{
  paper_bgcolor: '#1a1a2e', plot_bgcolor: '#16213e', font: {{color: '#eee'}},
  xaxis: {{title: 'Time (UTC)', gridcolor: '#333', range: ['{timestamps[0]}', '{timestamps[-1]}']}},
  yaxis: {{title: 'Win Probability', gridcolor: '#333', range: [0, 1.08], tickformat: '.0%'}},
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
            # Note: poll_price returns a dict with mid=None when book is empty,
            # not None itself. Check the mid value.
            t1_is_na = t1_raw is None or (t1_raw and t1_raw.get("mid") is None)
            t2_is_na = t2_raw is None or (t2_raw and t2_raw.get("mid") is None)
            if t1_is_na and t2_is_na:
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
            if len(all_records) > 1:
                write_chart(chartfile, all_records, team1_name, team2_name)

            tick += 1
            if tick % 5 == 0:
                t1_val = t1_data["mid"] if t1_data else None
                t2_val = t2_data["mid"] if t2_data else None
                t1_str = f"{t1_val:.3f}" if t1_val is not None else "N/A"
                t2_str = f"{t2_val:.3f}" if t2_val is not None else "N/A"
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
