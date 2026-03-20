from __future__ import annotations

"""
Polymarket Live Price Tracker

Polls the Polymarket CLOB API for order book and price data on CS2 match markets.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import httpx

import config
from event_log import append_event


@dataclass
class MarketState:
    token_yes: str = ""
    token_no: str = ""
    best_bid_yes: float = 0.0
    best_ask_yes: float = 0.0
    best_bid_no: float = 0.0
    best_ask_no: float = 0.0
    mid_yes: float = 0.0
    mid_no: float = 0.0
    last_price_yes: float = 0.0
    spread_yes: float = 0.0
    spread_no: float = 0.0
    last_update_ms: int = 0
    market_slug: str = ""
    # Orderbook depth
    bids_yes: list = field(default_factory=list)
    asks_yes: list = field(default_factory=list)


PriceCallback = Callable[[dict, "MarketState"], Coroutine[Any, Any, None]]


class MarketTracker:
    def __init__(
        self,
        token_yes: str,
        token_no: str,
        market_slug: str = "",
        on_price: PriceCallback | None = None,
    ):
        self.state = MarketState(
            token_yes=token_yes,
            token_no=token_no,
            market_slug=market_slug,
        )
        self.on_price = on_price
        self._running = False
        self._client: httpx.AsyncClient | None = None

    async def start_polling(self):
        self._running = True
        interval = config.MARKET_POLL_INTERVAL_MS / 1000.0
        async with httpx.AsyncClient(timeout=10.0) as client:
            self._client = client
            while self._running:
                try:
                    await self._poll_once(client)
                except httpx.HTTPError as e:
                    print(f"[MARKET] HTTP error: {e}")
                except Exception as e:
                    print(f"[MARKET] Unexpected error: {e}")
                await asyncio.sleep(interval)

    async def _poll_once(self, client: httpx.AsyncClient):
        ts_ms = int(time.time() * 1000)

        # Fetch order book for YES token
        book_resp = await client.get(
            f"{config.POLYMARKET_CLOB_URL}/book",
            params={"token_id": self.state.token_yes},
        )
        book_resp.raise_for_status()
        book = book_resp.json()

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        self.state.bids_yes = bids[:5]  # top 5 levels
        self.state.asks_yes = asks[:5]

        if bids:
            self.state.best_bid_yes = float(bids[0].get("price", 0))
        if asks:
            self.state.best_ask_yes = float(asks[0].get("price", 0))

        if self.state.best_bid_yes and self.state.best_ask_yes:
            self.state.mid_yes = (self.state.best_bid_yes + self.state.best_ask_yes) / 2
            self.state.spread_yes = self.state.best_ask_yes - self.state.best_bid_yes

        # Derive NO side (shares pay $1, so NO = 1 - YES)
        self.state.best_bid_no = round(1.0 - self.state.best_ask_yes, 4) if self.state.best_ask_yes else 0
        self.state.best_ask_no = round(1.0 - self.state.best_bid_yes, 4) if self.state.best_bid_yes else 0
        self.state.mid_no = round(1.0 - self.state.mid_yes, 4) if self.state.mid_yes else 0

        # Also fetch midpoint for comparison
        mid_resp = await client.get(
            f"{config.POLYMARKET_CLOB_URL}/midpoint",
            params={"token_id": self.state.token_yes},
        )
        if mid_resp.status_code == 200:
            mid_data = mid_resp.json()
            self.state.last_price_yes = float(mid_data.get("mid", self.state.mid_yes))

        self.state.last_update_ms = ts_ms

        log_entry = {
            "ts_ms": ts_ms,
            "token_yes": self.state.token_yes,
            "best_bid_yes": self.state.best_bid_yes,
            "best_ask_yes": self.state.best_ask_yes,
            "mid_yes": self.state.mid_yes,
            "spread_yes": self.state.spread_yes,
            "mid_no": self.state.mid_no,
            "book_depth_bids": len(bids),
            "book_depth_asks": len(asks),
        }
        append_event(config.MARKET_PRICES_LOG, log_entry)

        if self.on_price:
            await self.on_price(log_entry, self.state)

    def stop(self):
        self._running = False


async def search_cs2_markets() -> list[dict]:
    """Search Polymarket for active CS2 match markets."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{config.POLYMARKET_GAMMA_URL}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": 50,
                "order": "volume",
                "ascending": "false",
            },
        )
        resp.raise_for_status()
        markets = resp.json()

        # Filter for CS2-related markets
        cs2_keywords = {"cs2", "counter-strike", "counter strike", "csgo", "cs:go"}
        cs2_markets = []
        for m in markets:
            text = f"{m.get('question', '')} {m.get('description', '')} {m.get('slug', '')}".lower()
            if any(kw in text for kw in cs2_keywords):
                cs2_markets.append({
                    "slug": m.get("slug", ""),
                    "question": m.get("question", ""),
                    "tokens": m.get("clobTokenIds", []),
                    "volume": m.get("volume", 0),
                    "liquidity": m.get("liquidity", 0),
                    "active": m.get("active", False),
                    "end_date": m.get("endDate", ""),
                })

        return cs2_markets
