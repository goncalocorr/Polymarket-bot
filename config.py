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
STATUS_JSON_PATH = DOCS_DIR / "data" / "runtime_status.json"

DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
(DOCS_DIR / "data").mkdir(parents=True, exist_ok=True)

# === Polygon / Polymarket ===
POLYGON_WALLET_PRIVATE_KEY = os.getenv("POLYGON_WALLET_PRIVATE_KEY", "")
POLY_SAFE_ADDRESS = os.getenv("POLY_SAFE_ADDRESS", "")
POLY_BUILDER_API_KEY = os.getenv("POLY_BUILDER_API_KEY", "")
POLY_BUILDER_API_SECRET = os.getenv("POLY_BUILDER_API_SECRET", "")
POLY_BUILDER_API_PASSPHRASE = os.getenv("POLY_BUILDER_API_PASSPHRASE", "")

# L2 API credentials (derived from EIP-712 auth, or supplied directly)
POLY_API_KEY = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE", "")
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
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "")
KALSHI_API_SECRET = os.getenv("KALSHI_API_SECRET", "")
KALSHI_API_URL = "https://trading-api.kalshi.com/trade-api/v2/markets"
METACULUS_API_URL = "https://www.metaculus.com/api2/questions/"
MANIFOLD_API_URL = "https://api.manifold.markets/v0/search-markets"

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
MISPRICING_REENTRY_COOLDOWN_MIN = int(
    os.getenv("MISPRICING_REENTRY_COOLDOWN_MIN", "30")
)
MISPRICING_MAX_HOLD_MIN = int(os.getenv("MISPRICING_MAX_HOLD_MIN", "90"))
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

# === Kelly Criterion Sizing ===
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.50"))  # Half-Kelly
MIN_KELLY_EDGE = float(os.getenv("MIN_KELLY_EDGE", "0.03"))  # Min 3% edge to trade
KELLY_MIN_CONFIDENCE = float(os.getenv("KELLY_MIN_CONFIDENCE", "0.50"))

# === Market Quality ===
MIN_MARKET_QUALITY_SCORE = float(os.getenv("MIN_MARKET_QUALITY_SCORE", "0.40"))
MIN_DEPTH_USDC = float(os.getenv("MIN_DEPTH_USDC", "100.0"))

# === Multi-Outcome Arbitrage ===
ARBITRAGE_MAX_SUM = float(os.getenv("ARBITRAGE_MAX_SUM", "0.97"))  # Sum < this = pure arb
ARBITRAGE_MIN_OUTCOMES = int(os.getenv("ARBITRAGE_MIN_OUTCOMES", "3"))
ARBITRAGE_SIZE_USDC = float(os.getenv("ARBITRAGE_SIZE_USDC", "10.0"))

# === Strategy Tracker ===
TRACKER_ROLLING_WINDOW = int(os.getenv("TRACKER_ROLLING_WINDOW", "20"))
TRACKER_AUTO_PAUSE_WIN_RATE = float(os.getenv("TRACKER_AUTO_PAUSE_WIN_RATE", "0.35"))
TRACKER_AUTO_PAUSE_HOURS = int(os.getenv("TRACKER_AUTO_PAUSE_HOURS", "4"))
TRACKER_BOOST_WIN_RATE = float(os.getenv("TRACKER_BOOST_WIN_RATE", "0.65"))
TRACKER_BOOST_FACTOR = float(os.getenv("TRACKER_BOOST_FACTOR", "1.50"))

# === Three-Phase Execution ===
PHASE1_TIMEOUT_S = 45
PHASE2_INCREMENT_CENTS = 1.5
PHASE2_TIMEOUT_S = 30
PHASE3_INCREMENT_CENTS = 2.5
PHASE3_TIMEOUT_S = 20
MIN_ORDER_SIZE_USDC = 2.0
DRY_RUN_SIMULATED_DELAY_MS = int(os.getenv("DRY_RUN_SIMULATED_DELAY_MS", "250"))
DRY_RUN_SLIPPAGE_BPS = float(os.getenv("DRY_RUN_SLIPPAGE_BPS", "35"))

# === Safety ===
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# === Monitoring ===
STATUS_UPDATE_INTERVAL_S = 60
DAILY_REPORT_HOUR_UTC = 8
