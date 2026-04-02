"""Tests for improvements ported from reference repositories."""

import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.signer import sign_auth_message, get_address
from core.bot import TradingBot, ApiCredentials
from core.websocket_client import MarketWebSocket
from core.utils import retry_async
from ai.prompt_templates import SYSTEM_SUPERFORECASTER, superforecaster_prompt
from storage.database import Database


# --- HMAC L2 Authentication ---


class TestHMACAuth:
    def test_build_hmac_headers_with_api_creds(self):
        bot = TradingBot.__new__(TradingBot)
        bot.address = "0xTestAddress"
        bot.api_creds = ApiCredentials(
            api_key="test-key",
            secret="dGVzdC1zZWNyZXQ=",  # base64("test-secret")
            passphrase="test-pass",
        )
        bot.builder_api_key = ""
        bot.builder_api_secret = ""
        bot.builder_api_passphrase = ""

        headers = bot._build_hmac_headers("GET", "/data/orders")

        assert "POLY_API_KEY" in headers
        assert headers["POLY_API_KEY"] == "test-key"
        assert "POLY_SIGNATURE" in headers
        assert "POLY_TIMESTAMP" in headers
        assert "POLY_PASSPHRASE" in headers
        assert headers["POLY_ADDRESS"] == "0xTestAddress"

    def test_build_hmac_headers_with_body(self):
        bot = TradingBot.__new__(TradingBot)
        bot.address = "0xTestAddress"
        bot.api_creds = ApiCredentials(
            api_key="k", secret="dGVzdA==", passphrase="p"
        )
        bot.builder_api_key = ""
        bot.builder_api_secret = ""
        bot.builder_api_passphrase = ""

        h1 = bot._build_hmac_headers("POST", "/order", "")
        h2 = bot._build_hmac_headers("POST", "/order", '{"test":1}')

        # Different body → different signature (timestamps may also differ)
        assert "POLY_SIGNATURE" in h1
        assert "POLY_SIGNATURE" in h2

    def test_build_hmac_headers_no_creds(self):
        bot = TradingBot.__new__(TradingBot)
        bot.address = "0x"
        bot.api_creds = None
        bot.builder_api_key = ""
        bot.builder_api_secret = ""
        bot.builder_api_passphrase = ""

        headers = bot._build_hmac_headers("GET", "/data/orders")
        assert headers == {}

    def test_builder_hmac_headers(self):
        bot = TradingBot.__new__(TradingBot)
        bot.address = "0x"
        bot.api_creds = None
        bot.builder_api_key = "builder-key"
        bot.builder_api_secret = "builder-secret"
        bot.builder_api_passphrase = "builder-pass"

        headers = bot._build_hmac_headers("POST", "/order", '{"data":1}')
        assert headers["POLY_BUILDER_API_KEY"] == "builder-key"
        assert "POLY_BUILDER_SIGNATURE" in headers
        assert "POLY_BUILDER_TIMESTAMP" in headers

    def test_api_credentials_validation(self):
        valid = ApiCredentials(api_key="k", secret="s", passphrase="p")
        assert valid.is_valid()

        empty = ApiCredentials(api_key="", secret="s", passphrase="p")
        assert not empty.is_valid()


class TestAuthMessageSigning:
    def test_sign_auth_message_returns_hex(self):
        # Use a test private key (never use in production)
        pk = "0x" + "ab" * 32
        sig = sign_auth_message(pk, timestamp="1234567890", nonce=0)
        assert sig.startswith("0x")
        assert len(sig) > 10

    def test_sign_auth_message_deterministic(self):
        pk = "0x" + "ab" * 32
        sig1 = sign_auth_message(pk, timestamp="1234567890", nonce=0)
        sig2 = sign_auth_message(pk, timestamp="1234567890", nonce=0)
        assert sig1 == sig2

    def test_different_timestamp_different_sig(self):
        pk = "0x" + "ab" * 32
        sig1 = sign_auth_message(pk, timestamp="1000000000", nonce=0)
        sig2 = sign_auth_message(pk, timestamp="2000000000", nonce=0)
        assert sig1 != sig2


# --- Superforecaster Prompt ---


class TestSuperforecasterPrompt:
    def test_prompt_contains_methodology(self):
        assert "Decompose" in SYSTEM_SUPERFORECASTER
        assert "base rate" in SYSTEM_SUPERFORECASTER
        assert "probabilistically" in SYSTEM_SUPERFORECASTER

    def test_prompt_template_interpolation(self):
        prompt = superforecaster_prompt(
            question="Will BTC hit 100k?",
            price=0.45,
            volume=50000,
            end_date="2026-12-31",
            external_info="Kalshi: 0.50",
            news_section="- BTC rising",
        )
        assert "Will BTC hit 100k?" in prompt
        assert "45.0%" in prompt
        assert "$50,000" in prompt
        assert "2026-12-31" in prompt
        assert "Kalshi: 0.50" in prompt
        assert "BTC rising" in prompt

    def test_prompt_requests_json_output(self):
        prompt = superforecaster_prompt(
            question="Test?", price=0.5, volume=0,
            end_date="", external_info="", news_section="",
        )
        assert "JSON" in prompt
        assert "probability" in prompt
        assert "confidence" in prompt


# --- Orderbook Analysis ---


class TestOrderbookAnalysis:
    def setup_method(self):
        self.ws = MarketWebSocket(url="wss://test")
        self.ws._order_books["token1"] = {
            "bids": [
                {"price": "0.50", "size": "100"},
                {"price": "0.49", "size": "200"},
                {"price": "0.48", "size": "300"},
            ],
            "asks": [
                {"price": "0.52", "size": "150"},
                {"price": "0.53", "size": "250"},
                {"price": "0.54", "size": "350"},
            ],
        }

    def test_get_spread(self):
        spread = self.ws.get_spread("token1")
        assert spread is not None
        assert abs(spread - 0.02) < 1e-9

    def test_get_spread_no_book(self):
        assert self.ws.get_spread("unknown") is None

    def test_get_depth(self):
        depth = self.ws.get_depth("token1", levels=2)
        assert depth is not None
        assert depth["bid_depth"] == 300  # 100 + 200
        assert depth["ask_depth"] == 400  # 150 + 250

    def test_get_depth_all_levels(self):
        depth = self.ws.get_depth("token1", levels=5)
        assert depth["bid_depth"] == 600  # 100+200+300
        assert depth["ask_depth"] == 750  # 150+250+350

    def test_get_imbalance(self):
        imb = self.ws.get_imbalance("token1", levels=3)
        assert imb is not None
        # bid=600, ask=750, total=1350
        assert abs(imb - 600 / 1350) < 1e-9

    def test_get_imbalance_no_book(self):
        assert self.ws.get_imbalance("unknown") is None

    def test_get_volatility_insufficient_data(self):
        assert self.ws.get_volatility("token1") is None

    def test_get_volatility_with_data(self):
        now = time.time()
        self.ws._price_history["token1"].extend([
            (now - 10, 0.50),
            (now - 5, 0.55),
            (now - 1, 0.48),
        ])
        vol = self.ws.get_volatility("token1", lookback_seconds=60)
        assert vol is not None
        assert abs(vol - 0.07) < 1e-9  # 0.55 - 0.48

    @pytest.mark.asyncio
    async def test_process_message_accepts_list_payload(self):
        payload = json.dumps([
            {
                "event_type": "book",
                "asset_id": "token_list",
                "bids": [{"price": "0.40", "size": "10"}],
                "asks": [{"price": "0.42", "size": "12"}],
            }
        ])

        await self.ws._process_message(payload)

        assert abs(self.ws.get_mid_price("token_list") - 0.41) < 1e-9

    @pytest.mark.asyncio
    async def test_subscribe_uses_market_operation_payload(self):
        self.ws._ws = MagicMock()
        self.ws._ws.send = AsyncMock()

        await self.ws.subscribe("token_sub")

        self.ws._ws.send.assert_awaited_once_with(
            json.dumps({"assets_ids": ["token_sub"], "operation": "subscribe"})
        )

    @pytest.mark.asyncio
    async def test_subscribe_skips_duplicate_on_same_connection(self):
        self.ws._ws = MagicMock()
        self.ws._ws.send = AsyncMock()

        await self.ws.subscribe("token_sub")
        await self.ws.subscribe("token_sub")

        self.ws._ws.send.assert_awaited_once_with(
            json.dumps({"assets_ids": ["token_sub"], "operation": "subscribe"})
        )

    @pytest.mark.asyncio
    async def test_unsubscribe_uses_market_operation_payload(self):
        self.ws._ws = MagicMock()
        self.ws._ws.send = AsyncMock()

        await self.ws.unsubscribe("token_unsub")

        self.ws._ws.send.assert_awaited_once_with(
            json.dumps({"assets_ids": ["token_unsub"], "operation": "unsubscribe"})
        )

    @pytest.mark.asyncio
    async def test_process_message_ignores_no_new_assets_notice(self, monkeypatch):
        debug = MagicMock()
        warning = MagicMock()
        monkeypatch.setattr("core.websocket_client.logger.debug", debug)
        monkeypatch.setattr("core.websocket_client.logger.warning", warning)

        await self.ws._process_message("NO NEW ASSETS")

        debug.assert_called_once_with("WebSocket server notice: NO NEW ASSETS")
        warning.assert_not_called()


# --- Brier Score & Calibration ---


class TestBrierScore:
    def setup_method(self):
        import tempfile, os
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db = Database(db_path=self._tmp.name)

    def teardown_method(self):
        import os
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_save_and_resolve_prediction(self):
        self.db.save_prediction({
            "id": "p1",
            "token_id": "t1",
            "market_question": "Test?",
            "strategy": "ai_agent",
            "predicted_prob": 0.7,
            "market_price": 0.5,
            "confidence": 0.8,
            "created_at": "2026-01-01T00:00:00",
        })
        self.db.resolve_prediction("p1", outcome=1)

        brier = self.db.get_brier_score()
        assert brier is not None
        # Brier = (0.7 - 1)^2 = 0.09
        assert abs(brier - 0.09) < 1e-6

    def test_brier_score_multiple_predictions(self):
        for i, (prob, outcome) in enumerate([
            (0.8, 1),  # (0.8-1)^2 = 0.04
            (0.3, 0),  # (0.3-0)^2 = 0.09
            (0.6, 1),  # (0.6-1)^2 = 0.16
        ]):
            self.db.save_prediction({
                "id": f"p{i}",
                "token_id": "t1",
                "strategy": "test",
                "predicted_prob": prob,
                "market_price": 0.5,
                "created_at": "2026-01-01",
            })
            self.db.resolve_prediction(f"p{i}", outcome)

        brier = self.db.get_brier_score()
        expected = (0.04 + 0.09 + 0.16) / 3
        assert abs(brier - expected) < 1e-6

    def test_close_trade_persists_close_reason(self):
        self.db.save_trade({
            "id": "t-close",
            "token_id": "tok-1",
            "market_question": "Test?",
            "strategy": "mispricing",
            "side": "BUY",
            "entry_price": 0.5,
            "size_usdc": 25,
            "opened_at": "2026-04-02T00:00:00+00:00",
        })

        self.db.close_trade("t-close", exit_price=0.4, pnl=-5.0, close_reason="stop-loss")

        trade = self.db.get_recent_trades(1)[0]
        assert trade["close_reason"] == "stop-loss"

    def test_brier_score_by_strategy(self):
        self.db.save_prediction({
            "id": "a1", "token_id": "t", "strategy": "alpha",
            "predicted_prob": 0.9, "market_price": 0.5, "created_at": "2026-01-01",
        })
        self.db.resolve_prediction("a1", 1)

        self.db.save_prediction({
            "id": "b1", "token_id": "t", "strategy": "beta",
            "predicted_prob": 0.1, "market_price": 0.5, "created_at": "2026-01-01",
        })
        self.db.resolve_prediction("b1", 1)

        alpha_brier = self.db.get_brier_score("alpha")
        beta_brier = self.db.get_brier_score("beta")

        assert alpha_brier < beta_brier  # alpha predicted better

    def test_brier_score_no_resolved(self):
        self.db.save_prediction({
            "id": "p1", "token_id": "t", "strategy": "s",
            "predicted_prob": 0.5, "market_price": 0.5, "created_at": "2026-01-01",
        })
        assert self.db.get_brier_score() is None

    def test_calibration_buckets(self):
        # Create predictions across different probability ranges
        for i, (prob, outcome) in enumerate([
            (0.15, 0), (0.25, 0), (0.35, 1),
            (0.65, 1), (0.75, 1), (0.85, 1),
        ]):
            self.db.save_prediction({
                "id": f"p{i}", "token_id": "t", "strategy": "s",
                "predicted_prob": prob, "market_price": 0.5,
                "created_at": "2026-01-01",
            })
            self.db.resolve_prediction(f"p{i}", outcome)

        buckets = self.db.get_calibration_buckets(bucket_size=0.5)
        assert len(buckets) >= 1


# --- Retry Decorator ---


class TestRetryDecorator:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_try(self):
        call_count = 0

        @retry_async(max_attempts=3)
        async def success():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await success()
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_connection_error(self):
        call_count = 0

        @retry_async(max_attempts=3, base_delay=0.01)
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("down")
            return "recovered"

        result = await flaky()
        assert result == "recovered"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self):
        @retry_async(max_attempts=2, base_delay=0.01)
        async def always_fail():
            raise TimeoutError("timeout")

        with pytest.raises(TimeoutError):
            await always_fail()

    @pytest.mark.asyncio
    async def test_non_retryable_raises_immediately(self):
        call_count = 0

        @retry_async(max_attempts=3, base_delay=0.01)
        async def value_error():
            nonlocal call_count
            call_count += 1
            raise ValueError("not retryable")

        with pytest.raises(ValueError):
            await value_error()
        assert call_count == 1
