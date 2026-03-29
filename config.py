"""Centralised configuration via environment variables."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# === Paths ===
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
DOCS_DIR = BASE_DIR / "docs"
STATUS_JSON_PATH = DOCS_DIR / "data" / "status.json"

DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
(DOCS_DIR / "data").mkdir(parents=True, exist_ok=True)

# === Polygon / Polymarket ===
POLYGON_WALLET_PRIVATE_KEY = os.getenv("POLYGON_WALLET_PRIVATE_KEY", "")
POLY_SAFE_ADDRESS = os.getenv("POLY_SAFE_ADDRESS", "")
POLY_BUILDER_API_KEY = os.getenv("POLY_BUILDER_API_KEY", "")
POLY_BUILDER_API_SECRET = os.getenv("POLY_BUILDER_API_SECRET", "")
POLY_BUILDER_API_PASSPHRASE = os.getenv("POLY_BUILDER_API_PASSPHRASE", "")
POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")

POLYMARKET_CLOB_URL = "https://clob.polymarket.com"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# === AI ===
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_CLAUDE_CALLS_PER_DAY = 50

# === Notifications ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# === External data ===
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
KALSHI_API_URL = "https://trading-api.kalshi.com/trade-api/v2/markets"
METACULUS_API_URL = "https://www.metaculus.com/api2/questions/"
MANIFOLD_API_URL = "https://api.manifold.markets/v0/markets"

# === Flash Crash Strategy ===
FLASH_CRASH_SIZE_USDC = float(os.getenv("FLASH_CRASH_SIZE_USDC", "5.0"))
FLASH_CRASH_DROP_THRESHOLD = float(os.getenv("FLASH_CRASH_DROP_THRESHOLD", "0.28"))
FLASH_CRASH_LOOKBACK_SECONDS = int(os.getenv("FLASH_CRASH_LOOKBACK_SECONDS", "12"))
FLASH_CRASH_TAKE_PROFIT = float(os.getenv("FLASH_CRASH_TAKE_PROFIT", "0.08"))
FLASH_CRASH_STOP_LOSS = float(os.getenv("FLASH_CRASH_STOP_LOSS", "0.04"))

# === Mispricing Strategy ===
MIN_VOLUME_24H_USDC = float(os.getenv("MIN_VOLUME_24H_USDC", "5000"))
MAX_SPREAD_CENTS = float(os.getenv("MAX_SPREAD_CENTS", "3"))
MIN_DAYS_TO_RESOLUTION = int(os.getenv("MIN_DAYS_TO_RESOLUTION", "3"))
MAX_DAYS_TO_RESOLUTION = int(os.getenv("MAX_DAYS_TO_RESOLUTION", "60"))
MIN_MISPRICING_PCT = float(os.getenv("MIN_MISPRICING_PCT", "0.08"))
BLACKLISTED_CATEGORIES = [
    "us-politics",
    "celebrity",
    "crypto-price-24h",
]

# === Risk Management ===
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.05"))
MAX_POSITION_SIZE_PCT = float(os.getenv("MAX_POSITION_SIZE_PCT", "0.10"))
MAX_TOTAL_EXPOSURE_PCT = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "0.60"))
CIRCUIT_BREAKER_CONSECUTIVE_LOSSES = int(os.getenv("CIRCUIT_BREAKER_CONSECUTIVE_LOSSES", "3"))
CIRCUIT_BREAKER_PAUSE_HOURS = int(os.getenv("CIRCUIT_BREAKER_PAUSE_HOURS", "2"))

# === Three-Phase Execution ===
PHASE1_TIMEOUT_S = 45
PHASE2_INCREMENT_CENTS = 1.5
PHASE2_TIMEOUT_S = 30
PHASE3_INCREMENT_CENTS = 2.5
PHASE3_TIMEOUT_S = 20
MIN_ORDER_SIZE_USDC = 2.0

# === Safety ===
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# === Monitoring ===
STATUS_UPDATE_INTERVAL_S = 60
DAILY_REPORT_HOUR_UTC = 8
