"""
Real-time Terminal Dashboard

Displays live match state, market prices, delay analysis, and model vs market comparison.
Built with Rich for a clean terminal UI.
"""

import asyncio
import time

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import config
from hltv_tracker import MatchState
from market_tracker import MarketState
from delay_analyzer import DelayAnalyzer
from win_model import estimate_win_probability


class Dashboard:
    def __init__(
        self,
        match_state: MatchState,
        market_state: MarketState,
        analyzer: DelayAnalyzer,
    ):
        self.match = match_state
        self.market = market_state
        self.analyzer = analyzer
        self.console = Console()
        self._running = False
        self.start_time = time.time()
        self.event_log: list[str] = []  # recent events for display

    def add_event(self, msg: str):
        self.event_log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(self.event_log) > 15:
            self.event_log = self.event_log[-15:]

    def _build_match_panel(self) -> Panel:
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Label", style="dim")
        table.add_column("Value", style="bold")

        table.add_row("Match", self.match.match_id)
        table.add_row("Map", f"{self.match.map_name} (#{self.match.map_number})")
        table.add_row("Round", str(self.match.current_round))
        table.add_row("Phase", self.match.round_phase or "—")
        table.add_row("", "")

        side1 = f" ({self.match.team1_side})" if self.match.team1_side else ""
        side2 = f" ({self.match.team2_side})" if self.match.team2_side else ""
        t1_name = self.match.team1_name or "Team 1"
        t2_name = self.match.team2_name or "Team 2"

        score_text = f"{self.match.team1_score}  —  {self.match.team2_score}"
        table.add_row(f"{t1_name}{side1}", str(self.match.team1_score))
        table.add_row(f"{t2_name}{side2}", str(self.match.team2_score))
        table.add_row("", "")
        table.add_row("Alive", f"{self.match.team1_alive} vs {self.match.team2_alive}")
        table.add_row("Bomb", "PLANTED" if self.match.bomb_planted else "—")

        return Panel(table, title="[bold cyan]HLTV Match State[/]", border_style="cyan")

    def _build_market_panel(self) -> Panel:
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Label", style="dim")
        table.add_column("Value", style="bold")

        if not self.market.last_update_ms:
            table.add_row("Status", "[yellow]Waiting for data...[/]")
            return Panel(table, title="[bold green]Polymarket[/]", border_style="green")

        age_s = (int(time.time() * 1000) - self.market.last_update_ms) / 1000
        table.add_row("YES bid/ask", f"${self.market.best_bid_yes:.3f} / ${self.market.best_ask_yes:.3f}")
        table.add_row("YES mid", f"${self.market.mid_yes:.3f}")
        table.add_row("YES spread", f"${self.market.spread_yes:.3f}")
        table.add_row("", "")
        table.add_row("NO mid", f"${self.market.mid_no:.3f}")
        table.add_row("", "")
        table.add_row("Implied prob (YES)", f"{self.market.mid_yes * 100:.1f}%")
        table.add_row("Data age", f"{age_s:.1f}s")

        return Panel(table, title="[bold green]Polymarket[/]", border_style="green")

    def _build_model_panel(self) -> Panel:
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Label", style="dim")
        table.add_column("Value", style="bold")

        p1, p2 = estimate_win_probability(
            self.match.team1_score,
            self.match.team2_score,
            self.match.team1_side,
        )
        market_prob = self.market.mid_yes

        t1_name = self.match.team1_name or "Team 1"
        t2_name = self.match.team2_name or "Team 2"

        table.add_row(f"Model: {t1_name}", f"{p1 * 100:.1f}%")
        table.add_row(f"Model: {t2_name}", f"{p2 * 100:.1f}%")
        table.add_row("", "")
        table.add_row("Market: YES", f"{market_prob * 100:.1f}%" if market_prob else "—")
        table.add_row("Market: NO", f"{(1 - market_prob) * 100:.1f}%" if market_prob else "—")

        if market_prob:
            edge = p1 - market_prob
            edge_pct = edge * 100
            style = "bold green" if abs(edge_pct) > 3 else "bold yellow" if abs(edge_pct) > 1 else "dim"
            table.add_row("", "")
            table.add_row("Edge (model - market)", Text(f"{edge_pct:+.1f}%", style=style))

            if abs(edge_pct) > 5:
                direction = "BUY YES" if edge > 0 else "BUY NO"
                table.add_row("Signal", Text(f">>> {direction} <<<", style="bold red blink"))

        return Panel(table, title="[bold magenta]Model vs Market[/]", border_style="magenta")

    def _build_delay_panel(self) -> Panel:
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Label", style="dim")
        table.add_column("Value", style="bold")

        stats = self.analyzer.stats
        table.add_row("Measured reactions", str(stats["count"]))
        table.add_row("Timed out", str(stats["timed_out"]))
        table.add_row("Pending", str(stats["pending"]))
        table.add_row("", "")

        if stats["count"] > 0:
            table.add_row("Avg delay", f"{stats['avg_delay_ms']:.0f} ms")
            table.add_row("Median delay", f"{stats['median_delay_ms']:.0f} ms")
            table.add_row("Min delay", f"{stats['min_delay_ms']:.0f} ms")
            table.add_row("Max delay", f"{stats['max_delay_ms']:.0f} ms")
        else:
            table.add_row("Avg delay", "— (waiting for data)")

        last = self.analyzer.last_reaction
        if last and last.delay_ms > 0:
            table.add_row("", "")
            table.add_row("Last: round", str(last.round_num))
            table.add_row("Last: delay", f"{last.delay_ms} ms")
            table.add_row("Last: price move", f"{last.price_delta:+.3f}")

        return Panel(table, title="[bold yellow]Delay Analysis[/]", border_style="yellow")

    def _build_log_panel(self) -> Panel:
        text = "\n".join(self.event_log[-12:]) if self.event_log else "[dim]No events yet...[/]"
        return Panel(text, title="[bold]Event Log[/]", border_style="white")

    def build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="top", size=3),
            Layout(name="middle"),
            Layout(name="bottom", size=15),
        )

        elapsed = int(time.time() - self.start_time)
        mins, secs = divmod(elapsed, 60)
        header = Text(
            f"  CS2 Arbitrage Monitor  |  Uptime: {mins:02d}:{secs:02d}  |  "
            f"Match: {self.match.match_id}  |  "
            f"Market poll: {config.MARKET_POLL_INTERVAL_MS}ms",
            style="bold white on blue",
        )
        layout["top"].update(Panel(header, style="blue"))

        layout["middle"].split_row(
            Layout(name="match"),
            Layout(name="market"),
            Layout(name="model"),
            Layout(name="delay"),
        )
        layout["match"].update(self._build_match_panel())
        layout["market"].update(self._build_market_panel())
        layout["model"].update(self._build_model_panel())
        layout["delay"].update(self._build_delay_panel())

        layout["bottom"].update(self._build_log_panel())

        return layout

    async def run(self):
        self._running = True
        with Live(
            self.build_layout(),
            console=self.console,
            refresh_per_second=int(1 / config.DASHBOARD_REFRESH_S),
            screen=True,
        ) as live:
            while self._running:
                live.update(self.build_layout())
                await asyncio.sleep(config.DASHBOARD_REFRESH_S)

    def stop(self):
        self._running = False
