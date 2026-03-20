from __future__ import annotations

"""
Delay Analyzer

Measures the time delta between HLTV round events and Polymarket price reactions.
This is the core metric: how many milliseconds of edge do we have?
"""

import time
from dataclasses import dataclass, field

import config
from event_log import append_event


@dataclass
class RoundEndEvent:
    round_num: int
    score: str
    hltv_ts_ms: int
    winner: str


@dataclass
class PriceReaction:
    round_num: int
    hltv_ts_ms: int
    price_reaction_ts_ms: int
    delay_ms: int
    price_before: float
    price_after: float
    price_delta: float


class DelayAnalyzer:
    def __init__(self):
        self.pending_round_ends: list[RoundEndEvent] = []
        self.reactions: list[PriceReaction] = []
        self.last_known_price: float = 0.0
        self.price_at_round_end: dict[int, float] = {}  # round_num -> price at time of round end
        # Thresholds
        self.price_move_threshold = 0.005  # 0.5 cent move counts as a reaction
        self.max_reaction_window_ms = 120_000  # stop looking after 2 min

    def on_round_end(self, round_num: int, score: str, winner: str, ts_ms: int):
        """Called when HLTV reports a round end."""
        event = RoundEndEvent(
            round_num=round_num,
            score=score,
            hltv_ts_ms=ts_ms,
            winner=winner,
        )
        self.pending_round_ends.append(event)
        self.price_at_round_end[round_num] = self.last_known_price

        append_event(config.DELAY_LOG, {
            "type": "round_end_detected",
            "ts_ms": ts_ms,
            "round": round_num,
            "score": score,
            "winner": winner,
            "market_price_at_detection": self.last_known_price,
        })

    def on_price_update(self, mid_price: float, ts_ms: int):
        """Called on every market price update. Checks if any pending round ends just got priced in."""
        prev_price = self.last_known_price
        self.last_known_price = mid_price

        resolved = []
        for evt in self.pending_round_ends:
            baseline = self.price_at_round_end.get(evt.round_num, prev_price)
            price_delta = abs(mid_price - baseline)
            time_elapsed = ts_ms - evt.hltv_ts_ms

            if time_elapsed > self.max_reaction_window_ms:
                # Timed out — market didn't react or moved too slowly
                resolved.append(evt)
                reaction = PriceReaction(
                    round_num=evt.round_num,
                    hltv_ts_ms=evt.hltv_ts_ms,
                    price_reaction_ts_ms=0,
                    delay_ms=-1,  # -1 = no detectable reaction
                    price_before=baseline,
                    price_after=mid_price,
                    price_delta=price_delta,
                )
                self.reactions.append(reaction)
                self._log_reaction(reaction, timed_out=True)
                continue

            if price_delta >= self.price_move_threshold:
                resolved.append(evt)
                delay = ts_ms - evt.hltv_ts_ms
                reaction = PriceReaction(
                    round_num=evt.round_num,
                    hltv_ts_ms=evt.hltv_ts_ms,
                    price_reaction_ts_ms=ts_ms,
                    delay_ms=delay,
                    price_before=baseline,
                    price_after=mid_price,
                    price_delta=price_delta,
                )
                self.reactions.append(reaction)
                self._log_reaction(reaction, timed_out=False)

        for evt in resolved:
            self.pending_round_ends.remove(evt)

    def _log_reaction(self, r: PriceReaction, timed_out: bool):
        append_event(config.DELAY_LOG, {
            "type": "price_reaction",
            "round": r.round_num,
            "hltv_ts_ms": r.hltv_ts_ms,
            "reaction_ts_ms": r.price_reaction_ts_ms,
            "delay_ms": r.delay_ms,
            "price_before": r.price_before,
            "price_after": r.price_after,
            "price_delta": r.price_delta,
            "timed_out": timed_out,
        })

    @property
    def stats(self) -> dict:
        valid = [r for r in self.reactions if r.delay_ms > 0]
        if not valid:
            return {
                "count": 0,
                "avg_delay_ms": 0,
                "min_delay_ms": 0,
                "max_delay_ms": 0,
                "median_delay_ms": 0,
                "timed_out": len([r for r in self.reactions if r.delay_ms == -1]),
                "pending": len(self.pending_round_ends),
            }

        delays = sorted([r.delay_ms for r in valid])
        n = len(delays)
        median = delays[n // 2] if n % 2 == 1 else (delays[n // 2 - 1] + delays[n // 2]) / 2

        return {
            "count": n,
            "avg_delay_ms": sum(delays) / n,
            "min_delay_ms": delays[0],
            "max_delay_ms": delays[-1],
            "median_delay_ms": median,
            "timed_out": len([r for r in self.reactions if r.delay_ms == -1]),
            "pending": len(self.pending_round_ends),
        }

    @property
    def last_reaction(self) -> PriceReaction | None:
        return self.reactions[-1] if self.reactions else None
