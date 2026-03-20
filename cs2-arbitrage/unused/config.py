import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# HLTV
HLTV_SCOREBOT_URL = "wss://scorebot-secure.hltv.org/socket.io/?EIO=3&transport=websocket"
HLTV_MATCH_ID = os.getenv("HLTV_MATCH_ID", "")
HLTV_RECONNECT_DELAY_S = int(os.getenv("HLTV_RECONNECT_DELAY_S", "5"))

# Polymarket
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_URL = "https://clob.polymarket.com"
POLYMARKET_MARKET_SLUG = os.getenv("POLYMARKET_MARKET_SLUG", "")
POLYMARKET_CLOB_TOKEN_YES = os.getenv("POLYMARKET_CLOB_TOKEN_YES", "")
POLYMARKET_CLOB_TOKEN_NO = os.getenv("POLYMARKET_CLOB_TOKEN_NO", "")
MARKET_POLL_INTERVAL_MS = int(os.getenv("MARKET_POLL_INTERVAL_MS", "500"))

# Dashboard
DASHBOARD_REFRESH_S = float(os.getenv("DASHBOARD_REFRESH_S", "0.5"))

# Logging
MATCH_EVENTS_LOG = DATA_DIR / "match_events.jsonl"
MARKET_PRICES_LOG = DATA_DIR / "market_prices.jsonl"
DELAY_LOG = DATA_DIR / "delay_analysis.jsonl"
