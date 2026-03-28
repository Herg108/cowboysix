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
import os
import signal
import time
from pathlib import Path

import json as json_mod
import subprocess
import threading
import http.server
import functools

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

    Live: http://localhost:8888/live.html
    Static chart: data/<date>/<match>/map1/chart.html
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

    # Build player→team cache from kills where team is known
    player_team = {}
    for e in all_events:
        if e.get("type") == "kill":
            kt = e.get("killer_team", "?")
            if kt and kt != "?":
                player_team[e.get("killer", "")] = kt
            # Also learn from victims
            vt = e.get("victim_team", "?")
            if vt and vt != "?":
                player_team[e.get("victim", "")] = vt

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
            e["type"] = "round_start"
            events.append(e)
        elif etype == "kill":
            if not e.get("killer_team") or e["killer_team"] == "?":
                side = e.get("killer_side", "?")
                if side and side != "?":
                    e["killer_team"] = resolve_side(ts, side)
            # Still unknown — use player name cache
            if not e.get("killer_team") or e["killer_team"] == "?":
                e["killer_team"] = player_team.get(e.get("killer", ""), "?")
            events.append(e)

    return events


def write_data_json(chartfile: Path, records: list, team1_name: str, team2_name: str):
    """Write live_data.json with all chart data for JS polling."""
    t1 = team1_name.lower()
    t2 = team2_name.lower()

    data = {
        "team1": team1_name,
        "team2": team2_name,
        "ts": [r["ts_iso"] for r in records],
        "t1_mid": [r.get(f"{t1}_mid") for r in records],
        "t1_bid": [r.get(f"{t1}_bid") for r in records],
        "t1_ask": [r.get(f"{t1}_ask") for r in records],
        "t2_mid": [r.get(f"{t2}_mid") for r in records],
        "hltv": [],
    }

    hltv_events = load_hltv_events(chartfile, records)
    if hltv_events:
        def is_team1(name):
            a = name.lower().replace(" ", "")
            b = team1_name.lower().replace(" ", "")
            return (a in b) or (b in a)

        def find_price(ts):
            for r in records:
                if r["ts_iso"] >= ts:
                    return r.get(f"{t1}_mid")
            return None

        for ev in hltv_events:
            etype = ev.get("type", "")
            ts = ev.get("ts_iso", "")
            price = find_price(ts)
            if price is None:
                continue

            if etype == "round_end":
                winner_team = ev.get("winner_team", "?")
                t1_win = is_team1(winner_team)
                data["hltv"].append({
                    "t": ts, "y": price, "type": "round_end",
                    "t1_win": t1_win,
                    "ct_score": ev.get("ct_score", 0),
                    "t_score": ev.get("t_score", 0),
                    "winner": winner_team,
                    "win_type": ev.get("win_type", "?"),
                })
            elif etype == "round_start":
                data["hltv"].append({"t": ts, "y": price, "type": "round_start"})
            elif etype == "kill":
                victim_team = ev.get("victim_team", ev.get("killer_team", "?"))
                # Kill color based on victim: if victim is team1, red (bad for team1)
                # We stored killer_team, so victim is the opposite
                killer_team = ev.get("killer_team", "?")
                t1_kill = is_team1(killer_team)
                data["hltv"].append({
                    "t": ts, "y": price, "type": "first_kill" if ev.get("first_kill") else "kill",
                    "t1_kill": t1_kill,
                    "killer": ev.get("killer", "?"),
                    "victim": ev.get("victim", "?"),
                    "weapon": ev.get("weapon", "?"),
                    "headshot": ev.get("headshot", False),
                })

    datafile = chartfile.parent / "live_data.json"
    datafile.write_text(json_mod.dumps(data))


def write_chart_html(chartfile: Path, team1_name: str, team2_name: str, port: int = 8888):
    """Write the HTML shell once. It polls live_data.json via JS."""
    html = f"""<!DOCTYPE html>
<html><head>
<title>{team1_name} vs {team2_name}</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  body {{ background: #1a1a2e; color: #eee; font-family: 'Segoe UI', sans-serif; margin: 0; padding: 20px; }}
  h1 {{ text-align: center; color: #e94560; margin-bottom: 5px; }}
  .info {{ text-align: center; color: #999; margin-bottom: 10px; }}
  #chart {{ width: 100%; height: 80vh; }}
  .status {{ color: #4caf50; }}
</style>
</head><body>
<h1>{team1_name} vs {team2_name}</h1>
<div class="info" id="info">Loading...</div>
<div id="chart"></div>
<script>
const DATA_URL = 'live_data.json';
let lastLen = 0;
let chartReady = false;
let userZoomed = false;
let savedXRange = null;
let rightPinned = true;  // right edge follows latest data
let ignoreRelayout = false;  // guard against our own relayout calls

function buildTraces(d) {{
  const traces = [
    {{x: d.ts, y: d.t1_mid, name: d.team1, line: {{color: '#00d4ff', width: 2}}}},
    {{x: d.ts, y: d.t2_mid, name: d.team2, line: {{color: '#ff9f43', width: 2}}}},
    {{x: d.ts, y: d.t1_bid, name: 'Bid', line: {{color: '#00d4ff', width: 1, dash: 'dot'}}, showlegend: false}},
    {{x: d.ts, y: d.t1_ask, name: 'Ask', line: {{color: '#00d4ff', width: 1, dash: 'dot'}}, fill: 'tonexty', fillcolor: 'rgba(0,212,255,0.1)', showlegend: false}},
  ];

  // HLTV events
  const re = {{x:[], y:[], text:[], color:[], type: 'round_end'}};
  const rs = {{x:[], y:[], type: 'round_start'}};
  const fk = {{x:[], y:[], text:[], color:[], type: 'first_kill'}};
  const kills = {{x:[], y:[], text:[], color:[], type: 'kill'}};
  const shapes = [];
  const annotations = [];

  for (const ev of (d.hltv || [])) {{
    if (ev.type === 'round_end') {{
      const mc = ev.t1_win ? '#00ff64' : '#ff3c3c';
      const bg = ev.t1_win ? 'rgba(0,255,100,0.4)' : 'rgba(255,60,60,0.4)';
      re.x.push(ev.t); re.y.push(ev.y);
      re.text.push(ev.winner + ' wins<br>CT ' + ev.ct_score + '-' + ev.t_score + ' T<br>' + ev.win_type.replace(/_/g, ' '));
      re.color.push(mc);
      shapes.push({{type:'line',x0:ev.t,x1:ev.t,y0:0,y1:1,line:{{color:bg,width:1,dash:'dot'}}}});
      annotations.push({{x:ev.t,y:1.03,xref:'x',yref:'y',text:ev.ct_score+'-'+ev.t_score,showarrow:false,font:{{color:mc,size:10}}}});
    }} else if (ev.type === 'round_start') {{
      rs.x.push(ev.t); rs.y.push(ev.y);
      shapes.push({{type:'line',x0:ev.t,x1:ev.t,y0:0,y1:1,line:{{color:'rgba(255,255,0,0.4)',width:1,dash:'dash'}}}});
    }} else if (ev.type === 'first_kill') {{
      const mc = ev.t1_kill ? '#00ff64' : '#ff3c3c';
      fk.x.push(ev.t); fk.y.push(ev.y);
      fk.text.push(ev.killer + ' -> ' + ev.victim + ' [' + ev.weapon + ']' + (ev.headshot ? ' (HS)' : ''));
      fk.color.push(mc);
    }} else if (ev.type === 'kill') {{
      const mc = ev.t1_kill ? '#00ff64' : '#ff3c3c';
      kills.x.push(ev.t); kills.y.push(ev.y);
      kills.text.push(ev.killer + ' -> ' + ev.victim + ' [' + ev.weapon + ']' + (ev.headshot ? ' (HS)' : ''));
      kills.color.push(mc);
    }}
  }}

  traces.push({{x: re.x, y: re.y, text: re.text, name: 'Round End', mode: 'markers',
    marker: {{color: re.color, size: 10, symbol: 'diamond'}}, hovertemplate: '%{{text}}<extra></extra>'}});
  traces.push({{x: rs.x, y: rs.y, name: 'Round Start', mode: 'markers',
    marker: {{color: '#ffff00', size: 8, symbol: 'triangle-up'}}, hovertemplate: 'Round Start<extra></extra>'}});
  traces.push({{x: kills.x, y: kills.y, text: kills.text, name: 'Kill', mode: 'markers',
    marker: {{color: kills.color, size: 4, symbol: 'circle'}}, hovertemplate: '%{{text}}<extra></extra>'}});
  traces.push({{x: fk.x, y: fk.y, text: fk.text, name: 'First Kill', mode: 'markers',
    marker: {{color: fk.color, size: 8, symbol: 'x', line: {{color: '#88ddaa', width: 1}}}}, hovertemplate: '%{{text}}<extra></extra>'}});

  return {{traces, shapes, annotations}};
}}

function getLayout(d, shapes, annotations) {{
  return {{
    paper_bgcolor: '#1a1a2e', plot_bgcolor: '#16213e', font: {{color: '#eee'}},
    xaxis: {{title: 'Time (UTC)', gridcolor: '#333',
      range: [d.ts[0], d.ts[d.ts.length-1]],
      rangeslider: {{visible: true, bgcolor: '#1a1a2e', thickness: 0.08,
        range: [d.ts[0], d.ts[d.ts.length-1]]}}}},
    yaxis: {{title: 'Win Probability', gridcolor: '#333', range: [0, 1.08], tickformat: '.0%'}},
    showlegend: false, hovermode: 'x unified', margin: {{t: 40, b: 80}},
    shapes: shapes,
    annotations: annotations,
  }};
}}

async function update() {{
  try {{
    const resp = await fetch(DATA_URL + '?t=' + Date.now());
    const d = await resp.json();

    if (d.ts.length === lastLen) return;
    lastLen = d.ts.length;

    const {{traces, shapes, annotations}} = buildTraces(d);
    const layout = getLayout(d, shapes, annotations);

    const lastT1 = d.t1_mid.filter(v => v !== null).pop() || 0;
    const lastT2 = d.t2_mid.filter(v => v !== null).pop() || 0;
    const rounds = d.hltv.filter(e => e.type === 'round_end').length;
    const killCount = d.hltv.filter(e => e.type === 'kill' || e.type === 'first_kill').length;
    document.getElementById('info').innerHTML =
      d.ts.length + ' pts | ' + d.ts[d.ts.length-1] + ' UTC | ' +
      d.team1 + ': ' + (lastT1*100).toFixed(1) + '% | ' +
      d.team2 + ': ' + (lastT2*100).toFixed(1) + '% | ' +
      rounds + ' rounds, ' + killCount + ' kills | ' +
      '<span class="status">LIVE</span>';

    if (!chartReady) {{
      Plotly.newPlot('chart', traces, layout, {{responsive: true}});
      chartReady = true;
      // Listen for user zoom/pan events
      document.getElementById('chart').on('plotly_relayout', function(ed) {{
        if (ignoreRelayout) return;
        if (ed['xaxis.range[0]'] !== undefined || ed['xaxis.range'] !== undefined) {{
          userZoomed = true;
          const el = document.getElementById('chart');
          savedXRange = el.layout.xaxis.range.slice();
          // Check if right edge is near the end of data
          const lastTs = el.data[0].x[el.data[0].x.length - 1];
          const rightEdge = savedXRange[1];
          const diff = new Date(lastTs) - new Date(rightEdge);
          rightPinned = Math.abs(diff) < 30000;
        }}
        if (ed['xaxis.autorange']) {{
          userZoomed = false;
          savedXRange = null;
          rightPinned = true;
        }}
      }});
    }} else {{
      // Always update rangeslider to full data extent
      layout.xaxis.rangeslider.range = [d.ts[0], d.ts[d.ts.length-1]];

      if (userZoomed && savedXRange) {{
        // If right edge was pinned, update it to latest data
        if (rightPinned) {{
          savedXRange[1] = d.ts[d.ts.length - 1];
        }}
        layout.xaxis.range = savedXRange;
        layout.xaxis.autorange = false;
      }}
      ignoreRelayout = true;
      Plotly.react('chart', traces, layout);

      // Force rangeslider update via relayout
      Plotly.relayout('chart', {{
        'xaxis.rangeslider.range': [d.ts[0], d.ts[d.ts.length-1]]
      }}).then(() => {{ ignoreRelayout = false; }});
    }}
  }} catch(e) {{
    document.getElementById('info').textContent = 'Waiting for data...';
  }}
}}

update();
setInterval(update, 2000);
</script>
</body></html>"""
    chartfile.write_text(html)


_live_html_written = False

def write_chart(livefile: Path, records: list, team1_name: str, team2_name: str):
    """Write live.html shell (once) and live_data.json (every tick)."""
    global _live_html_written
    if not _live_html_written:
        write_chart_html(livefile, team1_name, team2_name)
        _live_html_written = True

    # Write data JSON every tick (same directory as livefile)
    write_data_json(livefile, records, team1_name, team2_name)


def write_static_chart(chartfile: Path, records: list, team1_name: str, team2_name: str):
    """Write a self-contained HTML with data embedded inline. For viewing after match ends."""
    # Build the data object
    t1 = team1_name.lower()
    t2 = team2_name.lower()

    data = {
        "team1": team1_name,
        "team2": team2_name,
        "ts": [r["ts_iso"] for r in records],
        "t1_mid": [r.get(f"{t1}_mid") for r in records],
        "t1_bid": [r.get(f"{t1}_bid") for r in records],
        "t1_ask": [r.get(f"{t1}_ask") for r in records],
        "t2_mid": [r.get(f"{t2}_mid") for r in records],
        "hltv": [],
    }

    hltv_events = load_hltv_events(chartfile, records)
    if hltv_events:
        def is_team1(name):
            a = name.lower().replace(" ", "")
            b = team1_name.lower().replace(" ", "")
            return (a in b) or (b in a)

        def find_price(ts):
            for r in records:
                if r["ts_iso"] >= ts:
                    return r.get(f"{t1}_mid")
            return None

        for ev in hltv_events:
            etype = ev.get("type", "")
            ts = ev.get("ts_iso", "")
            price = find_price(ts)
            if price is None:
                continue

            if etype == "round_end":
                winner_team = ev.get("winner_team", "?")
                t1_win = is_team1(winner_team)
                data["hltv"].append({
                    "t": ts, "y": price, "type": "round_end",
                    "t1_win": t1_win,
                    "ct_score": ev.get("ct_score", 0),
                    "t_score": ev.get("t_score", 0),
                    "winner": winner_team,
                    "win_type": ev.get("win_type", "?"),
                })
            elif etype == "round_start":
                data["hltv"].append({"t": ts, "y": price, "type": "round_start"})
            elif etype == "kill":
                killer_team = ev.get("killer_team", "?")
                t1_kill = is_team1(killer_team)
                data["hltv"].append({
                    "t": ts, "y": price, "type": "first_kill" if ev.get("first_kill") else "kill",
                    "t1_kill": t1_kill,
                    "killer": ev.get("killer", "?"),
                    "victim": ev.get("victim", "?"),
                    "weapon": ev.get("weapon", "?"),
                    "headshot": ev.get("headshot", False),
                })

    data_json = json_mod.dumps(data)

    html = f"""<!DOCTYPE html>
<html><head>
<title>{team1_name} vs {team2_name}</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  body {{ background: #1a1a2e; color: #eee; font-family: 'Segoe UI', sans-serif; margin: 0; padding: 20px; }}
  h1 {{ text-align: center; color: #e94560; margin-bottom: 5px; }}
  .info {{ text-align: center; color: #999; margin-bottom: 10px; }}
  #chart {{ width: 100%; height: 70vh; }}
  .controls {{ display: flex; align-items: center; justify-content: center; gap: 10px; margin-bottom: 10px; }}
  .controls button {{
    background: #16213e; border: 1px solid #333; color: #eee; padding: 6px 14px;
    border-radius: 4px; cursor: pointer; font-size: 14px;
  }}
  .controls button:hover {{ background: #1a2a4e; }}
  .controls button.active {{ border-color: #00d4ff; color: #00d4ff; }}
  .round-strip {{
    display: flex; justify-content: center; gap: 2px; margin-bottom: 10px; flex-wrap: wrap;
  }}
  .round-box {{
    width: 22px; height: 22px; border-radius: 3px; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    font-size: 9px; font-weight: bold; color: #fff; opacity: 0.7;
    transition: opacity 0.15s, transform 0.15s;
  }}
  .round-box:hover {{ opacity: 1; transform: scale(1.2); }}
  .round-box.selected {{ opacity: 1; transform: scale(1.3); outline: 2px solid #fff; }}
  .half-sep {{ width: 8px; }}
</style>
</head><body>
<h1>{team1_name} vs {team2_name}</h1>
<div class="info" id="info"></div>
<div class="controls">
  <button onclick="prevRound()" title="Previous round (Left arrow)">&#9664; Prev</button>
  <button onclick="showAll()" id="allBtn" class="active">Full Match</button>
  <button onclick="nextRound()" title="Next round (Right arrow)">Next &#9654;</button>
</div>
<div class="round-strip" id="roundStrip"></div>
<div id="chart"></div>
<script>
const d = {data_json};

// Build round boundaries from round_end events
const roundEnds = (d.hltv || []).filter(e => e.type === 'round_end');
const roundStarts = (d.hltv || []).filter(e => e.type === 'round_start');

// Map recorded round_ends by actual round number (ct_score + t_score)
const recordedByRound = {{}};
for (let i = 0; i < roundEnds.length; i++) {{
  const re = roundEnds[i];
  const roundNum = re.ct_score + re.t_score; // score after round = round number
  recordedByRound[roundNum] = {{ reIdx: i, re: re }};
}}

// Figure out total rounds from max score
const maxRound = roundEnds.length > 0 ? Math.max(...Object.keys(recordedByRound).map(Number)) : 0;

// Build full rounds array including missing ones
const rounds = [];
for (let rn = 1; rn <= maxRound; rn++) {{
  const rec = recordedByRound[rn];
  if (rec) {{
    const re = rec.re;
    const reIdx = rec.reIdx;
    // Find the round start before this round end
    let startT = null;
    for (let j = roundStarts.length - 1; j >= 0; j--) {{
      if (roundStarts[j].t < re.t) {{
        if (reIdx === 0 || roundStarts[j].t > roundEnds[reIdx-1].t) {{
          startT = roundStarts[j].t;
        }}
        break;
      }}
    }}
    if (!startT) {{
      startT = reIdx > 0 ? roundEnds[reIdx-1].t : d.ts[0];
    }}
    const endIdx = d.ts.findIndex(t => t > re.t);
    const endT = endIdx >= 0 && endIdx + 10 < d.ts.length ? d.ts[Math.min(endIdx + 10, d.ts.length - 1)] : re.t;

    // Determine who won this round by comparing to previous recorded round
    let t1_win = re.t1_win;

    rounds.push({{
      roundNum: rn,
      missing: false,
      startT: startT,
      endT: endT,
      ct_score: re.ct_score,
      t_score: re.t_score,
      t1_win: t1_win,
      winner: re.winner,
    }});
  }} else {{
    // Missing round — infer winner from surrounding scores
    // Find the next recorded round to figure out who gained a point
    let t1_win = null;
    const prevRec = rounds.length > 0 && !rounds[rounds.length-1].missing ? rounds[rounds.length-1] : null;
    let nextRec = null;
    for (let nr = rn + 1; nr <= maxRound; nr++) {{
      if (recordedByRound[nr]) {{ nextRec = recordedByRound[nr].re; break; }}
    }}
    // We can infer from score progression between prev and next
    // For now just mark as unknown
    if (prevRec && nextRec) {{
      // Compare team1 score: if it went up between prev and next, team1 won at some point
      // But with multiple missing rounds we can't be sure which — mark unknown
    }}
    rounds.push({{
      roundNum: rn,
      missing: true,
      startT: null, endT: null,
      ct_score: null, t_score: null,
      t1_win: null, winner: null,
    }});
  }}
}}

let currentRound = -1; // -1 = full match

// Build round strip
const strip = document.getElementById('roundStrip');
const halfLen = 12;
rounds.forEach((r, i) => {{
  if (i === halfLen) {{
    const sep = document.createElement('div');
    sep.className = 'half-sep';
    strip.appendChild(sep);
  }}
  const box = document.createElement('div');
  box.className = 'round-box';
  if (r.missing) {{
    box.style.background = '#333';
    box.style.opacity = '0.4';
    box.title = 'R' + r.roundNum + ': not recorded';
  }} else {{
    box.style.background = r.t1_win ? '#00aa44' : '#cc2222';
    box.title = 'R' + r.roundNum + ': CT ' + r.ct_score + '-' + r.t_score + ' T';
    box.onclick = () => goToRound(i);
  }}
  box.textContent = r.roundNum;
  box.id = 'rbox-' + i;
  strip.appendChild(box);
}});

function buildTraces(d) {{
  const traces = [
    {{x: d.ts, y: d.t1_mid, name: d.team1, line: {{color: '#00d4ff', width: 2}}}},
    {{x: d.ts, y: d.t2_mid, name: d.team2, line: {{color: '#ff9f43', width: 2}}}},
    {{x: d.ts, y: d.t1_bid, name: 'Bid', line: {{color: '#00d4ff', width: 1, dash: 'dot'}}, showlegend: false}},
    {{x: d.ts, y: d.t1_ask, name: 'Ask', line: {{color: '#00d4ff', width: 1, dash: 'dot'}}, fill: 'tonexty', fillcolor: 'rgba(0,212,255,0.1)', showlegend: false}},
  ];
  const shapes = [];
  const annotations = [];
  const re = {{x:[], y:[], text:[], color:[]}};
  const rs = {{x:[], y:[]}};
  const fk = {{x:[], y:[], text:[], color:[]}};
  const kills = {{x:[], y:[], text:[], color:[]}};

  for (const ev of (d.hltv || [])) {{
    if (ev.type === 'round_end') {{
      const mc = ev.t1_win ? '#00ff64' : '#ff3c3c';
      const bg = ev.t1_win ? 'rgba(0,255,100,0.4)' : 'rgba(255,60,60,0.4)';
      re.x.push(ev.t); re.y.push(ev.y);
      re.text.push(ev.winner + ' wins<br>CT ' + ev.ct_score + '-' + ev.t_score + ' T<br>' + ev.win_type.replace(/_/g, ' '));
      re.color.push(mc);
      shapes.push({{type:'line',x0:ev.t,x1:ev.t,y0:0,y1:1,line:{{color:bg,width:1,dash:'dot'}}}});
      annotations.push({{x:ev.t,y:1.03,xref:'x',yref:'y',text:ev.ct_score+'-'+ev.t_score,showarrow:false,font:{{color:mc,size:10}}}});
    }} else if (ev.type === 'round_start') {{
      rs.x.push(ev.t); rs.y.push(ev.y);
      shapes.push({{type:'line',x0:ev.t,x1:ev.t,y0:0,y1:1,line:{{color:'rgba(255,255,0,0.4)',width:1,dash:'dash'}}}});
    }} else if (ev.type === 'first_kill') {{
      const mc = ev.t1_kill ? '#00ff64' : '#ff3c3c';
      fk.x.push(ev.t); fk.y.push(ev.y);
      fk.text.push(ev.killer + ' -> ' + ev.victim + ' [' + ev.weapon + ']' + (ev.headshot ? ' (HS)' : ''));
      fk.color.push(mc);
    }} else if (ev.type === 'kill') {{
      const mc = ev.t1_kill ? '#00ff64' : '#ff3c3c';
      kills.x.push(ev.t); kills.y.push(ev.y);
      kills.text.push(ev.killer + ' -> ' + ev.victim + ' [' + ev.weapon + ']' + (ev.headshot ? ' (HS)' : ''));
      kills.color.push(mc);
    }}
  }}
  traces.push({{x: re.x, y: re.y, text: re.text, name: 'Round End', mode: 'markers', marker: {{color: re.color, size: 10, symbol: 'diamond'}}, hovertemplate: '%{{text}}<extra></extra>'}});
  traces.push({{x: rs.x, y: rs.y, name: 'Round Start', mode: 'markers', marker: {{color: '#ffff00', size: 8, symbol: 'triangle-up'}}, hovertemplate: 'Round Start<extra></extra>'}});
  traces.push({{x: kills.x, y: kills.y, text: kills.text, name: 'Kill', mode: 'markers', marker: {{color: kills.color, size: 4, symbol: 'circle'}}, hovertemplate: '%{{text}}<extra></extra>'}});
  traces.push({{x: fk.x, y: fk.y, text: fk.text, name: 'First Kill', mode: 'markers', marker: {{color: fk.color, size: 8, symbol: 'x', line: {{color: '#88ddaa', width: 1}}}}, hovertemplate: '%{{text}}<extra></extra>'}});
  return {{traces, shapes, annotations}};
}}

const {{traces, shapes, annotations}} = buildTraces(d);
const killCount = d.hltv.filter(e => e.type === 'kill' || e.type === 'first_kill').length;
const lastT1 = d.t1_mid.filter(v => v !== null).pop() || 0;
const lastT2 = d.t2_mid.filter(v => v !== null).pop() || 0;
document.getElementById('info').innerHTML =
  d.ts.length + ' pts | ' + d.team1 + ': ' + (lastT1*100).toFixed(1) + '% | ' +
  d.team2 + ': ' + (lastT2*100).toFixed(1) + '% | ' +
  rounds.length + ' rounds, ' + killCount + ' kills';

Plotly.newPlot('chart', traces, {{
  paper_bgcolor: '#1a1a2e', plot_bgcolor: '#16213e', font: {{color: '#eee'}},
  xaxis: {{title: 'Time (UTC)', gridcolor: '#333', rangeslider: {{visible: true, bgcolor: '#1a1a2e', thickness: 0.08}}}},
  yaxis: {{title: 'Win Probability', gridcolor: '#333', range: [0, 1.08], tickformat: '.0%'}},
  showlegend: false, hovermode: 'x unified', margin: {{t: 40, b: 80}},
  shapes: shapes, annotations: annotations,
}}, {{responsive: true}});

function goToRound(i) {{
  if (i < 0 || i >= rounds.length || rounds[i].missing) return;
  currentRound = i;
  const r = rounds[i];
  Plotly.relayout('chart', {{'xaxis.range': [r.startT, r.endT]}});
  updateStripHighlight();
  document.getElementById('allBtn').classList.remove('active');
}}

function showAll() {{
  currentRound = -1;
  Plotly.relayout('chart', {{'xaxis.range': [d.ts[0], d.ts[d.ts.length-1]]}});
  updateStripHighlight();
  document.getElementById('allBtn').classList.add('active');
}}

function prevRound() {{
  let target = currentRound <= 0 ? 0 : currentRound - 1;
  // Skip missing rounds
  while (target > 0 && rounds[target].missing) target--;
  if (!rounds[target].missing) goToRound(target);
}}

function nextRound() {{
  let target = currentRound < 0 ? 0 : currentRound + 1;
  // Skip missing rounds
  while (target < rounds.length - 1 && rounds[target].missing) target++;
  if (target < rounds.length && !rounds[target].missing) goToRound(target);
}}

function updateStripHighlight() {{
  document.querySelectorAll('.round-box').forEach(b => b.classList.remove('selected'));
  if (currentRound >= 0) {{
    const el = document.getElementById('rbox-' + currentRound);
    if (el) el.classList.add('selected');
  }}
}}

// Arrow key navigation
document.addEventListener('keydown', (e) => {{
  if (e.key === 'ArrowLeft') {{ prevRound(); e.preventDefault(); }}
  else if (e.key === 'ArrowRight') {{ nextRound(); e.preventDefault(); }}
  else if (e.key === 'Escape') {{ showAll(); e.preventDefault(); }}
}});
</script>
</body></html>"""
    chartfile.write_text(html)
    print(f"Static chart saved: {chartfile}")


def _rebuild_index():
    try:
        subprocess.run(["python3", "build_index.py"], capture_output=True, timeout=10, text=True)
        print("Site index rebuilt.")
    except Exception:
        pass



def read_map_state(base_dir: Path) -> dict | None:
    """Read map_state.json written by the HLTV tracker."""
    state_file = base_dir / "map_state.json"
    if not state_file.exists():
        return None
    try:
        return json_mod.loads(state_file.read_text())
    except Exception:
        return None


def write_waiting_page(directory: str, message: str):
    """Write a simple waiting page to live.html in the given directory."""
    Path(directory).mkdir(parents=True, exist_ok=True)
    html = f"""<!DOCTYPE html>
<html><head><title>Waiting</title>
<meta http-equiv="refresh" content="5">
<style>
body {{ background: #1a1a2e; color: #e0e0e0; font-family: monospace;
       display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }}
.msg {{ text-align: center; font-size: 1.5em; }}
.dot {{ animation: blink 1.5s infinite; }}
@keyframes blink {{ 0%,100% {{ opacity: 0.2; }} 50% {{ opacity: 1; }} }}
</style></head>
<body><div class="msg">{message}<span class="dot">...</span></div></body></html>"""
    (Path(directory) / "live.html").write_text(html)


async def main_multi(maps_info: list):
    """Record prices for a multi-map series, switching tokens as maps change."""
    base_dir = Path(maps_info[0]["output_path"]).parent

    map_configs = {}
    map_labels = []
    for m in maps_info:
        map_configs[m["map_label"]] = m
        map_labels.append(m["map_label"])

    # Start HTTP server on first map's dir (will be updated on map switch)
    port = 8888
    current_serve_dir = [str(Path(maps_info[0]["output_path"]))]

    class MapHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=current_serve_dir[0], **kwargs)
        def log_message(self, format, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", port), MapHandler)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    # Write initial waiting page
    write_waiting_page(current_serve_dir[0], "Waiting for match to start")

    print(f"Multi-map recorder started")
    print(f"Maps: {', '.join(map_labels)}")
    print(f"Live chart: http://localhost:{port}/live.html")
    print(f"Press Ctrl+C to stop.\n")

    current_map = None
    all_records = []
    logfile = None
    livefile = None
    chartfile = None
    team1_name = None
    team2_name = None
    team1_token = None
    team2_token = None
    last_t1 = None
    last_t2 = None
    na_streak_start = None
    tick = 0
    live_html_written = False

    # Clean up stale state file from previous runs
    stale_state = base_dir / "map_state.json"
    if stale_state.exists():
        stale_state.unlink()

    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            # Read map state from HLTV tracker
            state = read_map_state(base_dir)

            if state is None:
                # HLTV tracker hasn't written state yet
                if tick % 10 == 0:
                    print(f"[{time.strftime('%H:%M:%S')}] Waiting for HLTV tracker...")
                tick += 1
                await asyncio.sleep(POLL_INTERVAL)
                continue

            if state["status"] == "series_over":
                print(f"\n[DONE] Series complete!")
                break

            if state["status"] == "map_ended" and current_map is not None:
                # Current map just ended — save chart, show waiting page
                if all_records and chartfile:
                    write_static_chart(chartfile, all_records, team1_name, team2_name)
                    _rebuild_index()
                    print(f"[SAVE] Static chart for {current_map} saved.")
                write_waiting_page(current_serve_dir[0], f"{current_map.upper()} complete — waiting for next map")
                current_map = None
                tick += 1
                await asyncio.sleep(POLL_INTERVAL)
                continue

            active = state.get("active_map")

            if state["status"] == "live" and active and active != current_map:
                # New map is live — switch to it
                if active not in map_configs:
                    # No Polymarket token for this map — just wait
                    if tick % 10 == 0:
                        print(f"[{time.strftime('%H:%M:%S')}] {active} is live but no Polymarket market found, waiting...")
                    tick += 1
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                current_map = active
                cfg = map_configs[active]
                out = Path(cfg["output_path"])
                out.mkdir(parents=True, exist_ok=True)
                logfile = out / "market_prices.jsonl"
                livefile = out / "live.html"
                chartfile = out / "chart.html"
                team1_name = cfg["team1_name"]
                team2_name = cfg["team2_name"]
                team1_token = cfg["team1_token"]
                team2_token = cfg["team2_token"]
                last_t1 = None
                last_t2 = None
                na_streak_start = None
                live_html_written = False
                current_serve_dir[0] = str(out)

                # Load existing records for this map
                all_records = []
                if logfile.exists():
                    with open(logfile) as f:
                        for line in f:
                            if line.strip():
                                all_records.append(json_mod.loads(line))

                print(f"\n[SWITCH] Recording {active}: {team1_name} vs {team2_name}")
                print(f"  Output: {out}")
                print(f"  Live: http://localhost:{port}/live.html")

            if current_map is None:
                if tick % 10 == 0:
                    print(f"[{time.strftime('%H:%M:%S')}] Waiting for HLTV activity...")
                tick += 1
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # Poll prices
            t1_raw, t2_raw = await asyncio.gather(
                poll_price(client, team1_token),
                poll_price(client, team2_token),
            )

            # Track N/A streak
            t1_is_na = t1_raw is None or (t1_raw and t1_raw.get("mid") is None)
            t2_is_na = t2_raw is None or (t2_raw and t2_raw.get("mid") is None)
            if t1_is_na and t2_is_na:
                if na_streak_start is None:
                    na_streak_start = time.time()
            else:
                na_streak_start = None

            t1_data = t1_raw if t1_raw is not None else last_t1
            t2_data = t2_raw if t2_raw is not None else last_t2
            if t1_raw is not None:
                last_t1 = t1_raw
            if t2_raw is not None:
                last_t2 = t2_raw

            ts_ms = int(time.time() * 1000)
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

            if len(all_records) > 1:
                if not live_html_written:
                    write_chart_html(livefile, team1_name, team2_name)
                    live_html_written = True
                write_data_json(livefile, all_records, team1_name, team2_name)

            tick += 1
            if tick % 5 == 0:
                t1_val = t1_data["mid"] if t1_data else None
                t2_val = t2_data["mid"] if t2_data else None
                t1_str = f"{t1_val:.3f}" if t1_val is not None else "N/A"
                t2_str = f"{t2_val:.3f}" if t2_val is not None else "N/A"
                print(f"[{entry['ts_iso']}] {team1_name}: {t1_str}  |  {team2_name}: {t2_str}")

            await asyncio.sleep(POLL_INTERVAL)



async def main(output_dir: str, team1_name: str, team1_token: str, team2_name: str, team2_token: str):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    logfile = out / "market_prices.jsonl"

    livefile = out / "live.html"
    chartfile = out / "chart.html"

    # Start local HTTP server to serve the chart directory
    port = 8888
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
    httpd = http.server.HTTPServer(("127.0.0.1", port), handler)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    print(f"Recording prices to: {logfile}")
    print(f"Live chart at: http://localhost:{port}/live.html")
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
    last_t1 = None
    last_t2 = None
    na_streak_start = None
    NA_TIMEOUT = 120
    recording = False
    hltv_file = chartfile.parent / "hltv_events.jsonl"
    print("Waiting for HLTV activity before recording...")
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            ts_ms = int(time.time() * 1000)

            if not recording:
                if hltv_file.exists() and hltv_file.stat().st_size > 0:
                    recording = True
                    print("[GO] HLTV activity detected — recording started!")
                else:
                    if tick % 10 == 0:
                        print(f"[{time.strftime('%H:%M:%S')}] Waiting for HLTV...")
                    tick += 1
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

            t1_raw, t2_raw = await asyncio.gather(
                poll_price(client, team1_token),
                poll_price(client, team2_token),
            )

            # Check if HLTV says map is done
            if hltv_file.exists():
                try:
                    with open(hltv_file) as hf:
                        latest_score = None
                        for line in hf:
                            if line.strip():
                                evt = json_mod.loads(line)
                                if evt.get("type") == "scoreboard" and evt.get("event") == "score_change":
                                    latest_score = evt
                        if latest_score:
                            s1 = latest_score.get("team1_score", 0)
                            s2 = latest_score.get("team2_score", 0)
                            hi = max(s1, s2)
                            lo = min(s1, s2)
                            if lo <= 11:
                                target = 13
                            else:
                                ot_num = ((lo - 11) + 2) // 3
                                target = 13 + 3 * ot_num
                            if hi == target and s1 != s2:
                                winner = latest_score.get("team1") if s1 > s2 else latest_score.get("team2")
                                ot_str = " (OT)" if hi > 13 else ""
                                print(f"\n[DONE] HLTV says map over: {winner} wins {s1}-{s2}{ot_str}. Stopping.")
                                write_static_chart(chartfile, all_records, team1_name, team2_name)
                                break
                except Exception:
                    pass

            t1_is_na = t1_raw is None or (t1_raw and t1_raw.get("mid") is None)
            t2_is_na = t2_raw is None or (t2_raw and t2_raw.get("mid") is None)
            if t1_is_na and t2_is_na:
                if na_streak_start is None:
                    na_streak_start = time.time()
                elif time.time() - na_streak_start >= 120:
                    print(f"\n[DONE] Market empty for 120s (fallback). Stopping.")
                    write_static_chart(chartfile, all_records, team1_name, team2_name)
                    break
            else:
                na_streak_start = None

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

            if len(all_records) > 1:
                write_chart(livefile, all_records, team1_name, team2_name)

            tick += 1
            if tick % 5 == 0:
                t1_val = t1_data["mid"] if t1_data else None
                t2_val = t2_data["mid"] if t2_data else None
                t1_str = f"{t1_val:.3f}" if t1_val is not None else "N/A"
                t2_str = f"{t2_val:.3f}" if t2_val is not None else "N/A"
                print(f"[{entry['ts_iso']}] {team1_name}: {t1_str}  |  {team2_name}: {t2_str}")

            await asyncio.sleep(POLL_INTERVAL)

    if all_records:
        write_static_chart(chartfile, all_records, team1_name, team2_name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Record live Polymarket prices for a CS2 match")
    parser.add_argument("--output", default="data/live", help="Output directory")
    parser.add_argument("--preset", help="Use a preset (e.g. faze-tyloo-bo3, faze-tyloo-map2)")
    parser.add_argument("--team1-name", help="Team 1 name")
    parser.add_argument("--team1-token", help="Team 1 CLOB token ID")
    parser.add_argument("--team2-name", help="Team 2 name")
    parser.add_argument("--team2-token", help="Team 2 CLOB token ID")
    parser.add_argument("--maps-json", help="JSON with all maps' token info (multi-map mode)")
    args = parser.parse_args()

    # Handle SIGTERM same as KeyboardInterrupt
    signal.signal(signal.SIGTERM, lambda *_: os.kill(os.getpid(), signal.SIGINT))

    if args.maps_json:
        # Multi-map mode
        maps_info = json_mod.loads(args.maps_json)
        try:
            asyncio.run(main_multi(maps_info))
        except KeyboardInterrupt:
            pass
        finally:
            for m in maps_info:
                out = Path(m["output_path"])
                chartfile = out / "chart.html"
                logfile = out / "market_prices.jsonl"
                if logfile.exists():
                    records = []
                    with open(logfile) as f:
                        for line in f:
                            if line.strip():
                                records.append(json_mod.loads(line))
                    if records:
                        write_static_chart(chartfile, records, m["team1_name"], m["team2_name"])
            _rebuild_index()
            print("\nStopped. Data saved.")
    else:
        # Single-map mode (backward compatible)
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
            print("Provide either --preset, --team1-token + --team2-token, or --maps-json")
            exit(1)

        try:
            asyncio.run(main(args.output, t1_name, t1_token, t2_name, t2_token))
        except KeyboardInterrupt:
            pass
        finally:
            out = Path(args.output)
            chartfile = out / "chart.html"
            logfile = out / "market_prices.jsonl"
            if logfile.exists():
                records = []
                with open(logfile) as f:
                    for line in f:
                        if line.strip():
                            records.append(json_mod.loads(line))
                if records:
                    write_static_chart(chartfile, records, t1_name, t2_name)
            print("\nStopped. Data saved.")
