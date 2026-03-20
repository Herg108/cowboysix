#!/usr/bin/env python3
"""
Generate an interactive Plotly chart of FURIA vs TYLOO Polymarket price data.
Opens in browser as an HTML file.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

DATA_DIR = Path(__file__).parent / "data"


def load_prices(filename: str) -> tuple[list[datetime], list[float]]:
    with open(DATA_DIR / filename) as f:
        pts = json.load(f)
    times = [datetime.fromtimestamp(p["t"], tz=timezone.utc) for p in pts]
    prices = [p["p"] * 100 for p in pts]  # convert to percentage
    return times, prices


def main():
    # Load all three datasets
    mono_t, mono_p = load_prices("furia_tyloo_prices.json")
    map2_t, map2_p = load_prices("furia_tyloo_map2.json")
    handi_t, handi_p = load_prices("furia_tyloo_handicap.json")

    # Filter to match window only (21:00 - 00:00 UTC where the action is)
    match_start = datetime(2026, 3, 18, 21, 0, tzinfo=timezone.utc)
    match_end = datetime(2026, 3, 19, 0, 10, tzinfo=timezone.utc)

    def clip(times, prices):
        paired = [(t, p) for t, p in zip(times, prices) if match_start <= t <= match_end]
        if not paired:
            return times, prices
        return [x[0] for x in paired], [x[1] for x in paired]

    mono_t_c, mono_p_c = clip(mono_t, mono_p)
    map2_t_c, map2_p_c = clip(map2_t, map2_p)
    handi_t_c, handi_p_c = clip(handi_t, handi_p)

    # Build figure
    fig = make_subplots(
        rows=2, cols=1,
        row_heights=[0.7, 0.3],
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=(
            "FURIA vs TYLOO — Polymarket Prices During Match (March 18, 2026)",
            "Price Volatility (1-min absolute change)"
        ),
    )

    # Main price traces
    fig.add_trace(go.Scatter(
        x=mono_t_c, y=mono_p_c,
        name="Match Winner (FURIA)",
        line=dict(color="#2196F3", width=2.5),
        hovertemplate="<b>Match Winner</b><br>%{x|%H:%M UTC}<br>FURIA: %{y:.1f}%<extra></extra>",
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=map2_t_c, y=map2_p_c,
        name="Map 2 Winner (FURIA)",
        line=dict(color="#FF9800", width=2),
        hovertemplate="<b>Map 2 Winner</b><br>%{x|%H:%M UTC}<br>FURIA: %{y:.1f}%<extra></extra>",
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=handi_t_c, y=handi_p_c,
        name="Map Handicap -1.5 (FURIA)",
        line=dict(color="#4CAF50", width=2),
        hovertemplate="<b>Handicap -1.5</b><br>%{x|%H:%M UTC}<br>FURIA: %{y:.1f}%<extra></extra>",
    ), row=1, col=1)

    # Volatility subplot (absolute 1-min change for moneyline)
    vol_times = mono_t_c[1:]
    vol_vals = [abs(mono_p_c[i] - mono_p_c[i - 1]) for i in range(1, len(mono_p_c))]
    vol_colors = ["#f44336" if v > 2 else "#ffcdd2" if v > 0.5 else "#e0e0e0" for v in vol_vals]

    fig.add_trace(go.Bar(
        x=vol_times, y=vol_vals,
        name="1-min |change|",
        marker_color=vol_colors,
        hovertemplate="%{x|%H:%M UTC}<br>|Δ|: %{y:.2f}%<extra></extra>",
    ), row=2, col=1)

    # Annotations for key moments
    annotations = [
        ("21:54", "Map 1 starts\n(price moves)", 83.5),
        ("22:04", "FURIA dominant\n→ 89%", 89),
        ("22:25", "TYLOO comeback\n→ 84.5%", 84.5),
        ("22:57", "Map 1 ends\nFURIA wins", 95.5),
        ("23:17", "Map 2 FURIA\nup big → 99%", 99.2),
        ("23:47", "TYLOO fights\nback → 87%", 87.1),
        ("23:51", "Match over\nFURIA 2-0", 100),
    ]

    for time_str, text, y_val in annotations:
        h, m = map(int, time_str.split(":"))
        day = 18 if h >= 12 else 19
        ann_time = datetime(2026, 3, day, h, m, tzinfo=timezone.utc)

        fig.add_annotation(
            x=ann_time, y=y_val,
            text=text,
            showarrow=True,
            arrowhead=2,
            arrowsize=1,
            arrowwidth=1.5,
            arrowcolor="#666",
            font=dict(size=10),
            bgcolor="rgba(255,255,255,0.85)",
            bordercolor="#999",
            borderwidth=1,
            borderpad=3,
            row=1, col=1,
        )

    # Layout
    fig.update_layout(
        height=750,
        template="plotly_white",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="center",
            x=0.5,
            font=dict(size=12),
        ),
        hovermode="x unified",
        margin=dict(t=80, b=40),
    )

    fig.update_yaxes(title_text="Implied Probability (%)", range=[50, 102], row=1, col=1)
    fig.update_yaxes(title_text="|Change| (%)", row=2, col=1)
    fig.update_xaxes(
        title_text="Time (UTC) — March 18-19, 2026",
        tickformat="%H:%M",
        row=2, col=1,
    )

    # Add horizontal reference lines
    fig.add_hline(y=50, line_dash="dot", line_color="#ccc", row=1, col=1)
    fig.add_hline(y=100, line_dash="dot", line_color="#ccc", row=1, col=1)

    # Save and open
    out_path = DATA_DIR / "furia_tyloo_chart.html"
    fig.write_html(str(out_path), auto_open=True)
    print(f"Chart saved to {out_path}")


if __name__ == "__main__":
    main()
