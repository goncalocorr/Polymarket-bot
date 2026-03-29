[Ler em Portugues](README.pt.md)

# Polymarket Autonomous Trading Bot

An autonomous 24/7 trading bot for [Polymarket](https://polymarket.com) prediction markets. Designed for small capital (<$500 USDC) running on a Linux VPS.

## What This Bot Does

The bot runs three strategies to find profitable opportunities on Polymarket:

### Strategy 1: Flash Crash
Monitors 15-minute crypto Up/Down markets (BTC, ETH, SOL, XRP) via WebSocket. When a probability drops sharply (>28% in 12 seconds), it buys the crashed side and exits at +8% take-profit or -4% stop-loss. These drops are typically caused by large sell orders that temporarily push the price below fair value.

### Strategy 2: Cross-Market Mispricing
Compares Polymarket prices with the same events on Kalshi, Metaculus, and Manifold Markets. When prices diverge by more than 8 percentage points, trades the underpriced side. The assumption is that prediction market prices converge over time.

### Strategy 3: AI Agent (Claude-powered)
For borderline cases (5-8% divergence), asks Claude AI to analyse the market with recent news context. Only trades when Claude is confident and agrees with the external price signal. Limited to 50 API calls/day to control costs.

## Risk Disclaimer

**You can and will likely lose money.** Prediction markets are speculative and carry significant risk. This bot is experimental software — bugs, API changes, or market conditions could result in total loss of capital. Only use money you can afford to lose. Past backtest performance does not guarantee future results.

## Setup Guide

### Prerequisites
- A MetaMask wallet with USDC on Polygon network
- A Polymarket account (check [polymarket.com/tos](https://polymarket.com/tos) for geographic restrictions)
- A VPS (recommended: Hetzner CX22, ~$6/month)
- Docker and Docker Compose installed

### Step 1: Get Polymarket Credentials
1. Create a wallet on [MetaMask](https://metamask.io)
2. Bridge USDC to Polygon network (use [bridge.polygon.technology](https://bridge.polygon.technology))
3. Sign up at [polymarket.com](https://polymarket.com) and connect your wallet
4. Go to Settings and note your Safe Address
5. Apply for the Builder Program at [docs.polymarket.com](https://docs.polymarket.com) to get API keys (enables gasless transactions)

### Step 2: Clone and Configure
```bash
git clone https://github.com/YOUR_USERNAME/polymarket_bot.git
cd polymarket_bot
cp .env.example .env
# Edit .env with your credentials
```

### Step 3: Run Backtest First
Before risking real money, run the backtest against historical data:
```bash
# Clone the historical dataset
git clone https://github.com/Jon-Becker/prediction-market-analysis
cd prediction-market-analysis && make setup  # Downloads ~36GB of data
cd ../polymarket_bot

pip install -r requirements.txt
python -m scripts.backtest
```
Review the output. Only proceed if the backtest shows positive expected value.

### Step 4: Docker Deploy
```bash
docker compose up -d
docker compose logs -f  # Watch the logs
```

### Step 5: Gradual Rollout
1. **Week 1-2**: Run with `DRY_RUN=true` — the bot logs what it would trade but places no real orders
2. **Week 3**: Set `DRY_RUN=false`, start with $20 USDC on Flash Crash only
3. **Week 5+**: Enable Mispricing after Flash Crash is stable
4. **Week 7+**: Enable AI Agent after Mispricing is stable

## Dashboard and Logs

### Terminal Dashboard
If running interactively (not in Docker), the bot shows a real-time terminal dashboard with positions, P&L, and strategy status.

### Web Dashboard
Open `docs/index.html` in a browser (or serve via GitHub Pages). The bot writes `docs/data/status.json` every 60 seconds with current state. The dashboard supports English and Portuguese.

### Telegram Alerts
Configure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` to receive alerts for:
- Bot start/restart
- New positions opened
- Positions closed (with P&L)
- Daily stop-loss triggered
- Circuit breaker activated
- Daily report at 08:00 UTC

### Logs
Stored in `logs/` directory, rotated daily, kept for 30 days.

## Tuning Risk Parameters

Edit these in `.env`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DAILY_LOSS_LIMIT_PCT` | 0.05 | Halt trading if daily loss exceeds 5% of capital |
| `MAX_POSITION_SIZE_PCT` | 0.10 | Max 10% of capital per position |
| `MAX_TOTAL_EXPOSURE_PCT` | 0.60 | Max 60% of capital in open positions |
| `FLASH_CRASH_DROP_THRESHOLD` | 0.28 | Min price drop to trigger flash crash buy |
| `FLASH_CRASH_SIZE_USDC` | 5.0 | Size per flash crash trade |
| `MIN_MISPRICING_PCT` | 0.08 | Min divergence to trigger mispricing trade |

## Architecture

```
main.py                  Orchestrator — wires everything together
core/                    Polymarket API (bot, websocket, gamma, signer, crypto)
strategies/              Flash Crash, Mispricing, AI Agent
execution/               Three-phase order executor, order lifecycle
data/                    Market scanner, external sources, indexer
risk/                    Risk manager (stop-loss, circuit breaker, limits)
ai/                      Claude analyst
storage/                 SQLite database, Parquet store
notifications/           Telegram alerts
monitor/                 Terminal TUI dashboard, status.json writer
scripts/                 Backtesting
tests/                   Unit tests
docs/                    Web monitoring dashboard (HTML/CSS/JS)
```

## Running Tests

```bash
pip install -r requirements.txt
pytest tests/ -v --cov
```

## Credits

Built upon and adapted from these open-source repositories (MIT license):
- [discountry/polymarket-trading-bot](https://github.com/discountry/polymarket-trading-bot) — Core trading infrastructure
- [Polymarket/agents](https://github.com/Polymarket/agents) — AI agent pattern and market data
- [Jon-Becker/prediction-market-analysis](https://github.com/Jon-Becker/prediction-market-analysis) — Historical data and backtesting
- [zydomus219/Polymarket-betting-bot](https://github.com/zydomus219/Polymarket-betting-bot) — Three-phase order execution

## Geographic Restrictions

Polymarket may not be available in all jurisdictions. Check [polymarket.com/tos](https://polymarket.com/tos) before using this bot. It is your responsibility to comply with local laws and regulations.

## License

MIT
