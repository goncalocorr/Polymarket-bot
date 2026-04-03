"""Centralised configuration via environment variables."""

import os
import re
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


def _read_multiline_secret_from_env_file(key: str) -> str:
    """Recover PEM-style multiline secrets directly from .env when needed."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return ""
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return ""

    pattern = re.compile(
        rf"(?ms)^{re.escape(key)}=(-----BEGIN [^-]+-----.*?-----END [^-]+-----)"
    )
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def _normalise_secret(value: str, *, key: str = "") -> str:
    """Normalise secrets that may use escaped newlines or multiline PEM blocks."""
    value = (value or "").strip()
    if "\\n" in value:
        value = value.replace("\\n", "\n")
    if "-----BEGIN" in value and "-----END" not in value and key:
        recovered = _read_multiline_secret_from_env_file(key)
        if recovered:
            value = recovered
    return value

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
AI_PROVIDER = os.getenv("AI_PROVIDER", "auto").strip().lower()
if AI_PROVIDER not in {"auto", "anthropic", "openai"}:
    AI_PROVIDER = "auto"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/responses")
MAX_AI_CALLS_PER_DAY = int(
    os.getenv("MAX_AI_CALLS_PER_DAY", os.getenv("MAX_CLAUDE_CALLS_PER_DAY", "50"))
)
# Backward-compatible alias for older code/tests/config.
MAX_CLAUDE_CALLS_PER_DAY = MAX_AI_CALLS_PER_DAY

# === Notifications ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# === External data ===
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "")
KALSHI_API_SECRET = _normalise_secret(
    os.getenv("KALSHI_API_SECRET", ""),
    key="KALSHI_API_SECRET",
)
KALSHI_API_URL = os.getenv(
    "KALSHI_API_URL",
    "https://api.elections.kalshi.com/trade-api/v2",
)
METACULUS_API_URL = "https://www.metaculus.com/api2/questions/"
MANIFOLD_API_URL = "https://api.manifold.markets/v0/search-markets"
EXTERNAL_PRICE_CACHE_TTL_S = int(os.getenv("EXTERNAL_PRICE_CACHE_TTL_S", "60"))
KALSHI_MARKETS_CACHE_TTL_S = int(os.getenv("KALSHI_MARKETS_CACHE_TTL_S", "60"))

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
MISPRICING_STRIKEOUT_LOSSES = int(os.getenv("MISPRICING_STRIKEOUT_LOSSES", "3"))
MISPRICING_STRIKEOUT_LOOKBACK_HOURS = int(
    os.getenv("MISPRICING_STRIKEOUT_LOOKBACK_HOURS", "24")
)
MISPRICING_STRIKEOUT_PAUSE_HOURS = int(
    os.getenv("MISPRICING_STRIKEOUT_PAUSE_HOURS", "12")
)
BLACKLISTED_CATEGORIES = [
    "us-politics",
    "celebrity",
    "crypto-price-24h",
]

# === Risk Management ===
TOTAL_CAPITAL = float(os.getenv("TOTAL_CAPITAL", "500.0"))
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

# === Reward Market Maker ===
MAKER_ENABLED = os.getenv("MAKER_ENABLED", "true").lower() == "true"
MAKER_ALLOW_LIVE = os.getenv("MAKER_ALLOW_LIVE", "false").lower() == "true"
MAKER_LOOP_INTERVAL_S = float(os.getenv("MAKER_LOOP_INTERVAL_S", "15"))
MAKER_MARKETS_CACHE_TTL_S = int(os.getenv("MAKER_MARKETS_CACHE_TTL_S", "120"))
MAKER_CAPITAL_USDC = float(os.getenv("MAKER_CAPITAL_USDC", "150.0"))
MAKER_MAX_MARKETS = int(os.getenv("MAKER_MAX_MARKETS", "2"))
MAKER_MAX_PER_MARKET_USDC = float(os.getenv("MAKER_MAX_PER_MARKET_USDC", "60.0"))
MAKER_MAX_INVENTORY_USDC = float(os.getenv("MAKER_MAX_INVENTORY_USDC", "80.0"))
MAKER_MIN_QUOTE_SIZE_USDC = float(os.getenv("MAKER_MIN_QUOTE_SIZE_USDC", "10.0"))
MAKER_REQUOTE_BPS = float(os.getenv("MAKER_REQUOTE_BPS", "25"))
MAKER_EXIT_PROFIT_BPS = float(os.getenv("MAKER_EXIT_PROFIT_BPS", "75"))
MAKER_INVENTORY_SKEW_BPS = float(os.getenv("MAKER_INVENTORY_SKEW_BPS", "35"))
MAKER_BOOK_STALE_S = float(os.getenv("MAKER_BOOK_STALE_S", "20"))
MAKER_MIN_DAYS_TO_RESOLUTION = int(os.getenv("MAKER_MIN_DAYS_TO_RESOLUTION", "2"))
MAKER_MAX_DAYS_TO_RESOLUTION = int(os.getenv("MAKER_MAX_DAYS_TO_RESOLUTION", "45"))
MAKER_MARKET_MAX_SPREAD_CENTS = float(os.getenv("MAKER_MARKET_MAX_SPREAD_CENTS", "4"))
MAKER_MIN_REWARD_DAILY_RATE = float(os.getenv("MAKER_MIN_REWARD_DAILY_RATE", "0"))
MAKER_MIN_REWARD_EFFICIENCY = float(
    os.getenv("MAKER_MIN_REWARD_EFFICIENCY", "0.0001")
)
MAKER_RELAX_EFFICIENCY_IN_DRY_RUN = (
    os.getenv("MAKER_RELAX_EFFICIENCY_IN_DRY_RUN", "true").lower() == "true"
)
MAKER_RELAXED_EFFICIENCY_FACTOR = float(
    os.getenv("MAKER_RELAXED_EFFICIENCY_FACTOR", "0.30")
)
MAKER_REQUIRE_MEASURABLE_DAILY_REWARD = (
    os.getenv("MAKER_REQUIRE_MEASURABLE_DAILY_REWARD", "true").lower() == "true"
)
MAKER_SCORING_REFRESH_S = int(os.getenv("MAKER_SCORING_REFRESH_S", "60"))

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
DRY_RUN_MAKER_MIN_FILL_AGE_S = float(
    os.getenv("DRY_RUN_MAKER_MIN_FILL_AGE_S", "45")
)
DRY_RUN_MAKER_FORCE_FILL_AGE_S = float(
    os.getenv("DRY_RUN_MAKER_FORCE_FILL_AGE_S", "180")
)
DRY_RUN_MAKER_MIN_COMPETITIVENESS = float(
    os.getenv("DRY_RUN_MAKER_MIN_COMPETITIVENESS", "0.35")
)
DRY_RUN_MAKER_REQUIRE_SCORING = (
    os.getenv("DRY_RUN_MAKER_REQUIRE_SCORING", "true").lower() == "true"
)

# === Safety ===
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# === Monitoring ===
STATUS_UPDATE_INTERVAL_S = 60
DAILY_REPORT_HOUR_UTC = 8
STRATEGY_EVALUATION_TIMEOUT_S = float(os.getenv("STRATEGY_EVALUATION_TIMEOUT_S", "25"))
