"""
Generate a live-updating HTML chart from the price recorder JSONL file.
Re-run this script to refresh the chart with latest data.

Usage:
    python live_chart.py --input data/faze_tyloo_map2/market_prices.jsonl --output site/faze_tyloo_map2_live.html
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_chart(input_path: str, output_path: str, title: str = "FaZe vs TYLOO - Map 2 Live Prices"):
    rows = []
    with open(input_path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    if not rows:
        print("No data yet.")
        return

    timestamps = [r["ts_iso"] for r in rows]
    faze_mid = [r["faze_mid"] for r in rows]
    tyloo_mid = [r["tyloo_mid"] for r in rows]
    faze_bid = [r["faze_bid"] for r in rows]
    faze_ask = [r["faze_ask"] for r in rows]

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>{title}</title>
    <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
    <style>
        body {{ background: #1a1a2e; color: #eee; font-family: 'Segoe UI', sans-serif; margin: 0; padding: 20px; }}
        h1 {{ text-align: center; color: #e94560; }}
        .info {{ text-align: center; color: #999; margin-bottom: 10px; }}
        #chart {{ width: 100%; height: 80vh; }}
    </style>
</head>
<body>
    <h1>{title}</h1>
    <div class="info">{len(rows)} data points | Last update: {timestamps[-1]} UTC | FaZe: {faze_mid[-1]*100:.1f}% | TYLOO: {tyloo_mid[-1]*100:.1f}%</div>
    <div id="chart"></div>
    <script>
        var timestamps = {json.dumps(timestamps)};
        var faze_mid = {json.dumps(faze_mid)};
        var tyloo_mid = {json.dumps(tyloo_mid)};
        var faze_bid = {json.dumps(faze_bid)};
        var faze_ask = {json.dumps(faze_ask)};

        var traces = [
            {{
                x: timestamps, y: faze_mid,
                name: 'FaZe Win %',
                line: {{ color: '#e94560', width: 2 }},
                yaxis: 'y'
            }},
            {{
                x: timestamps, y: tyloo_mid,
                name: 'TYLOO Win %',
                line: {{ color: '#0f3460', width: 2 }},
                yaxis: 'y'
            }},
            {{
                x: timestamps, y: faze_bid,
                name: 'FaZe Bid',
                line: {{ color: '#e94560', width: 1, dash: 'dot' }},
                opacity: 0.4,
                yaxis: 'y',
                showlegend: false
            }},
            {{
                x: timestamps, y: faze_ask,
                name: 'FaZe Ask',
                line: {{ color: '#e94560', width: 1, dash: 'dot' }},
                opacity: 0.4,
                fill: 'tonexty',
                fillcolor: 'rgba(233,69,96,0.1)',
                yaxis: 'y',
                showlegend: false
            }}
        ];

        var layout = {{
            paper_bgcolor: '#1a1a2e',
            plot_bgcolor: '#16213e',
            font: {{ color: '#eee' }},
            xaxis: {{
                title: 'Time (UTC)',
                gridcolor: '#333',
                tickangle: -45
            }},
            yaxis: {{
                title: 'Win Probability',
                gridcolor: '#333',
                range: [0, 1],
                tickformat: '.0%'
            }},
            legend: {{ x: 0.01, y: 0.99 }},
            hovermode: 'x unified',
            margin: {{ t: 20, b: 80 }}
        }};

        Plotly.newPlot('chart', traces, layout, {{ responsive: true }});
    </script>
</body>
</html>"""

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)
    print(f"Chart saved to {output_path} ({len(rows)} points)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/faze_tyloo_map2/market_prices.jsonl")
    parser.add_argument("--output", default="site/faze_tyloo_map2_live.html")
    parser.add_argument("--title", default="FaZe vs TYLOO - Map 2 Live Prices")
    args = parser.parse_args()
    build_chart(args.input, args.output, args.title)
