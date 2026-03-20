#!/usr/bin/env python3
"""
Overlay chart: Polymarket prices + HLTV round-end markers.

Parses demo files to get round-by-round results with correct team attribution
(handling side swap at round 12), then overlays on Polymarket price data.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots
from demoparser2 import DemoParser

DATA_DIR = Path(__file__).parent / "data"
SITE_DIR = Path(__file__).parent / "site"
TICKRATE = 64


def parse_demo_with_teams(demo_path: str) -> tuple[list[dict], str, str]:
    """Parse a demo file and return rounds with correct team scores.

    Returns (rounds, furia_start_side, map_name).
    """
    p = DemoParser(demo_path)
    header = p.parse_header()
    map_name = header.get("map_name", "unknown")

    # Get starting sides
    ticks_df = p.parse_ticks(["team_name", "team_clan_name"], ticks=[100])
    furia_row = ticks_df[ticks_df["team_clan_name"] == "FURIA"].iloc[0]
    furia_start_side = furia_row["team_name"]  # "CT" or "TERRORIST"

    # Parse round ends
    result = p.parse_events(["round_end"])
    _, rdf = result[0]

    rounds = []
    furia_score = 0
    tyloo_score = 0

    for _, row in rdf.iterrows():
        rnd = int(row["round"])
        if rnd == 0:
            continue

        tick = int(row["tick"])
        winner_side = row["winner"]
        reason = row["reason"] if row["reason"] else ""
        elapsed_s = tick / TICKRATE

        # Determine which team won based on side and half
        if rnd <= 12:
            furia_is_ct = (furia_start_side == "CT")
        else:
            furia_is_ct = (furia_start_side != "CT")

        if winner_side == "CT":
            winner_team = "FURIA" if furia_is_ct else "TYLOO"
        else:
            winner_team = "TYLOO" if furia_is_ct else "FURIA"

        if winner_team == "FURIA":
            furia_score += 1
        else:
            tyloo_score += 1

        is_half = (rnd == 12)
        is_map_over = (furia_score >= 13 or tyloo_score >= 13)

        rounds.append({
            "round": rnd,
            "tick": tick,
            "elapsed_s": round(elapsed_s, 1),
            "winner": winner_team,
            "reason": reason,
            "furia_score": furia_score,
            "tyloo_score": tyloo_score,
            "is_half": is_half,
            "is_map_over": is_map_over,
        })

        if is_map_over:
            break

    return rounds, furia_start_side, map_name


def load_prices(filename: str) -> list[dict]:
    with open(DATA_DIR / filename) as f:
        return json.load(f)


def align_rounds(rounds: list[dict], map_start_utc: datetime) -> list[dict]:
    start_ts = map_start_utc.timestamp()
    for r in rounds:
        r["wall_clock_ts"] = start_ts + r["elapsed_s"]
        r["wall_clock_dt"] = datetime.fromtimestamp(r["wall_clock_ts"], tz=timezone.utc)
    return rounds


def find_price_at_time(prices: list[dict], ts: float) -> float | None:
    best = None
    for p in prices:
        if p["t"] <= ts:
            best = p["p"]
        else:
            break
    return best


def find_price_reaction(prices: list[dict], ts: float, window_s: int = 300) -> dict:
    baseline = find_price_at_time(prices, ts)
    if baseline is None:
        return {"delay_s": None, "price_before": None, "price_after": None, "delta": None}

    for p in prices:
        if p["t"] > ts and p["t"] <= ts + window_s:
            delta = abs(p["p"] - baseline)
            if delta >= 0.005:
                return {
                    "delay_s": round(p["t"] - ts, 1),
                    "price_before": baseline,
                    "price_after": p["p"],
                    "delta": round(p["p"] - baseline, 4),
                }

    return {"delay_s": None, "price_before": baseline, "price_after": None, "delta": None}


def main():
    # Parse demos
    map1_rounds, m1_side, m1_map = parse_demo_with_teams(str(DATA_DIR / "furia-vs-tyloo-m1-mirage.dem"))
    map2_rounds, m2_side, m2_map = parse_demo_with_teams(str(DATA_DIR / "furia-vs-tyloo-m2-overpass.dem"))

    print(f"Map 1 ({m1_map}): FURIA starts {m1_side}, {len(map1_rounds)} rounds")
    print(f"  Final: FURIA {map1_rounds[-1]['furia_score']} - {map1_rounds[-1]['tyloo_score']} TYLOO")
    print(f"Map 2 ({m2_map}): FURIA starts {m2_side}, {len(map2_rounds)} rounds")
    print(f"  Final: FURIA {map2_rounds[-1]['furia_score']} - {map2_rounds[-1]['tyloo_score']} TYLOO")
    print()

    # Load prices
    prices = load_prices("furia_tyloo_prices.json")

    # Estimated map start times (UTC)
    map1_start = datetime(2026, 3, 18, 21, 50, 0, tzinfo=timezone.utc)
    map2_start = datetime(2026, 3, 18, 22, 51, 18, tzinfo=timezone.utc)

    map1_rounds = align_rounds(map1_rounds, map1_start)
    map2_rounds = align_rounds(map2_rounds, map2_start)

    # Save corrected round data
    for name, rounds in [("map1_rounds.json", map1_rounds), ("map2_rounds.json", map2_rounds)]:
        save = [{k: v for k, v in r.items() if k != "wall_clock_dt"} for r in rounds]
        with open(DATA_DIR / name, "w") as f:
            json.dump(save, f, indent=2)

    # Print delay analysis
    print("=== DELAY ANALYSIS ===\n")
    all_delays = []

    for label, rounds, map_name in [
        ("Map 1", map1_rounds, m1_map),
        ("Map 2", map2_rounds, m2_map),
    ]:
        print(f"{label} ({map_name}):")
        print(f"  {'Rnd':>3} {'Time':>8} {'Score':>10} {'Winner':>7} {'Reason':>16} {'Mkt Before':>10} {'Mkt After':>10} {'Delay':>7}")
        print("  " + "-" * 80)

        for r in rounds:
            reaction = find_price_reaction(prices, r["wall_clock_ts"])
            dt_str = r["wall_clock_dt"].strftime("%H:%M:%S")
            score_str = f"FUR {r['furia_score']}-{r['tyloo_score']} TYL"

            pb = f"${reaction['price_before']:.3f}" if reaction["price_before"] else "—"
            pa = f"${reaction['price_after']:.3f}" if reaction["price_after"] else "—"
            delay = f"{reaction['delay_s']:.0f}s" if reaction["delay_s"] else "—"

            half_mark = " ← HALF" if r["is_half"] else ""
            end_mark = " ← MAP" if r["is_map_over"] else ""

            if reaction["delay_s"] is not None:
                all_delays.append(reaction["delay_s"])

            print(f"  R{r['round']:>2} {dt_str} {score_str:>10} {r['winner']:>7} {r['reason']:>16} {pb:>10} {pa:>10} {delay:>7}{half_mark}{end_mark}")
        print()

    if all_delays:
        sorted_delays = sorted(all_delays)
        n = len(sorted_delays)
        median = sorted_delays[n // 2]
        print(f"Rounds with detectable price reaction: {len(all_delays)}/{len(map1_rounds) + len(map2_rounds)}")
        print(f"Average delay: {sum(all_delays)/len(all_delays):.1f}s")
        print(f"Median delay:  {median:.1f}s")
        print(f"Min delay:     {min(all_delays):.1f}s")
        print(f"Max delay:     {max(all_delays):.1f}s")

    # === BUILD CHART ===
    fig = make_subplots(
        rows=2, cols=1,
        row_heights=[0.75, 0.25],
        shared_xaxes=True,
        vertical_spacing=0.06,
        subplot_titles=(
            "FURIA vs TYLOO — Polymarket Price + Round Events (March 18, 2026)",
            "Delay: Seconds from Round End to Market Reaction"
        ),
    )

    # Filter prices to match window
    match_start = datetime(2026, 3, 18, 21, 30, tzinfo=timezone.utc)
    match_end = datetime(2026, 3, 19, 0, 10, tzinfo=timezone.utc)

    price_times = []
    price_vals = []
    for p in prices:
        dt = datetime.fromtimestamp(p["t"], tz=timezone.utc)
        if match_start <= dt <= match_end:
            price_times.append(dt)
            price_vals.append(p["p"] * 100)

    # Price line
    fig.add_trace(go.Scatter(
        x=price_times, y=price_vals,
        name="FURIA Win % (Polymarket)",
        line=dict(color="#2196F3", width=2.5),
        hovertemplate="%{x|%H:%M:%S UTC}<br>FURIA: %{y:.1f}%<extra></extra>",
    ), row=1, col=1)

    # Round markers
    for label, rounds, symbol, map_name in [
        ("Map 1 (Mirage)", map1_rounds, "triangle-up", m1_map),
        ("Map 2 (Overpass)", map2_rounds, "diamond", m2_map),
    ]:
        colors = ["#4CAF50" if r["winner"] == "FURIA" else "#f44336" for r in rounds]

        y_vals = []
        for r in rounds:
            # Interpolate on the price line to sit exactly on it
            ts = r["wall_clock_ts"]
            before_p, after_p, before_t, after_t = None, None, None, None
            for px in prices:
                if px["t"] <= ts:
                    before_p = px["p"]
                    before_t = px["t"]
                elif after_p is None:
                    after_p = px["p"]
                    after_t = px["t"]
                    break
            if before_p is not None and after_p is not None and after_t != before_t:
                frac = (ts - before_t) / (after_t - before_t)
                interp = before_p + frac * (after_p - before_p)
                y_vals.append(interp * 100)
            elif before_p is not None:
                y_vals.append(before_p * 100)
            else:
                y_vals.append(80)

        hover_texts = []
        for r in rounds:
            note = ""
            if r["is_half"]:
                note = "<br><b>HALFTIME — sides swap</b>"
            if r["is_map_over"]:
                note = "<br><b>MAP OVER</b>"
            hover_texts.append(
                f"<b>{label} — Round {r['round']}</b><br>"
                f"{r['winner']} wins ({r['reason']})<br>"
                f"FURIA {r['furia_score']} - {r['tyloo_score']} TYLOO"
                f"{note}"
            )

        fig.add_trace(go.Scatter(
            x=[r["wall_clock_dt"] for r in rounds],
            y=y_vals,
            mode="markers+text",
            name=label,
            marker=dict(size=10, color=colors, symbol=symbol, line=dict(width=1, color="white")),
            text=[f"R{r['round']}" for r in rounds],
            textposition="top center",
            textfont=dict(size=8),
            hovertext=hover_texts,
            hoverinfo="text",
        ), row=1, col=1)

    # Half-time vertical lines
    for rounds, label in [(map1_rounds, "M1"), (map2_rounds, "M2")]:
        for r in rounds:
            if r["is_half"]:
                fig.add_shape(
                    type="line", x0=r["wall_clock_dt"], x1=r["wall_clock_dt"],
                    y0=70, y1=102, line=dict(dash="dash", color="rgba(255,255,0,0.4)", width=1.5),
                    row=1, col=1,
                )
                fig.add_annotation(
                    x=r["wall_clock_dt"], y=102, text=f"{label} Half",
                    showarrow=False, font=dict(size=9, color="#cc0"), row=1, col=1,
                )

    # Map break
    map1_end = map1_rounds[-1]["wall_clock_dt"]
    map2_first = map2_rounds[0]["wall_clock_dt"]
    fig.add_shape(
        type="rect", x0=map1_end, x1=map2_first, y0=0, y1=1, yref="paper",
        fillcolor="rgba(255,255,255,0.05)", line_width=0,
    )
    fig.add_annotation(
        x=map1_end + (map2_first - map1_end) / 2, y=102,
        text="Map Break", showarrow=False, font=dict(size=10, color="#888"),
        row=1, col=1,
    )

    # Delay bars (bottom subplot)
    delay_times = []
    delay_values = []
    delay_labels = []
    delay_colors = []

    for label, rounds in [("M1", map1_rounds), ("M2", map2_rounds)]:
        for r in rounds:
            reaction = find_price_reaction(prices, r["wall_clock_ts"])
            if reaction["delay_s"] is not None:
                delay_times.append(r["wall_clock_dt"])
                delay_values.append(reaction["delay_s"])
                delay_labels.append(f"{label} R{r['round']}")
                delay_colors.append(
                    "#4CAF50" if reaction["delay_s"] < 30
                    else "#FF9800" if reaction["delay_s"] < 60
                    else "#f44336"
                )

    fig.add_trace(go.Bar(
        x=delay_times, y=delay_values,
        name="Reaction Delay (s)",
        marker_color=delay_colors,
        text=delay_labels,
        textposition="outside",
        textfont=dict(size=7),
        hovertemplate="%{text}<br>Delay: %{y:.0f}s<extra></extra>",
    ), row=2, col=1)

    # Layout
    fig.update_layout(
        height=800,
        template="plotly_white",
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5,
            font=dict(size=11),
        ),
        hovermode="closest",
        margin=dict(t=80, b=40),
    )

    fig.update_yaxes(title_text="FURIA Win Probability (%)", range=[70, 102], row=1, col=1)
    fig.update_yaxes(title_text="Delay (seconds)", row=2, col=1)
    fig.update_xaxes(tickformat="%H:%M", title_text="Time (UTC)", row=2, col=1)

    fig.add_annotation(
        text="Green = FURIA wins round | Red = TYLOO wins round | Yellow dashed = Halftime (sides swap)",
        xref="paper", yref="paper", x=0.01, y=-0.08,
        showarrow=False, font=dict(size=10, color="#888"),
    )

    out_path = SITE_DIR / "furia_tyloo_overlay.html"
    fig.write_html(str(out_path), auto_open=True)
    print(f"\nChart saved to {out_path}")


if __name__ == "__main__":
    main()
