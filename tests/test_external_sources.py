"""Tests for external source rate limiting and failure handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from data.external_sources import ExternalSources


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"{status_code} error", request=request, response=response
    )


@pytest.mark.asyncio
async def test_metaculus_403_enters_cooldown_after_first_failure():
    external = ExternalSources()
    client = MagicMock()
    client.get = AsyncMock(side_effect=_http_status_error(403))

    with (
        patch.object(external, "_get_client", AsyncMock(return_value=client)),
        patch("data.external_sources._metaculus_throttle", AsyncMock()),
        patch("data.external_sources.logger.debug") as mock_debug,
        patch("data.external_sources._metaculus_disabled_until", 0.0),
    ):
        assert await external.get_metaculus_price("Will it rain?") is None
        assert await external.get_metaculus_price("Will it rain again?") is None

    assert client.get.await_count == 1


@pytest.mark.asyncio
async def test_kalshi_missing_credentials_logs_once():
    external = ExternalSources()

    with (
        patch("data.external_sources.config.KALSHI_API_KEY", ""),
        patch("data.external_sources.config.KALSHI_API_SECRET", ""),
        patch("data.external_sources._kalshi_skip_logged", False),
        patch("data.external_sources.logger.debug") as mock_debug,
    ):
        assert await external.get_kalshi_markets() == []
        assert await external.get_kalshi_markets() == []

    assert mock_debug.call_count == 1


@pytest.mark.asyncio
async def test_kalshi_market_snapshot_is_cached():
    external = ExternalSources()
    client = MagicMock()
    response = MagicMock()
    response.json.return_value = {"markets": [{"ticker": "KXTEST"}]}
    response.raise_for_status.return_value = None
    client.get = AsyncMock(return_value=response)

    with (
        patch("data.external_sources.config.KALSHI_API_KEY", "key"),
        patch("data.external_sources.config.KALSHI_API_SECRET", "secret"),
        patch("data.external_sources.config.KALSHI_MARKETS_CACHE_TTL_S", 60),
        patch.object(
            external,
            "_kalshi_auth_headers",
            return_value={"KALSHI-ACCESS-KEY": "key"},
        ),
        patch("data.external_sources._kalshi_markets_cache", (0.0, [])),
        patch.object(external, "_get_client", AsyncMock(return_value=client)),
    ):
        first = await external.get_kalshi_markets()
        second = await external.get_kalshi_markets()

    assert first == second == [{"ticker": "KXTEST"}]
    assert client.get.await_count == 1


@pytest.mark.asyncio
async def test_find_matching_prices_uses_short_result_cache():
    external = ExternalSources()

    with (
        patch("data.external_sources.config.EXTERNAL_PRICE_CACHE_TTL_S", 60),
        patch("data.external_sources._external_price_cache", {}),
        patch.object(external, "get_kalshi_price", AsyncMock(return_value=0.61)) as kalshi,
        patch.object(external, "get_metaculus_price", AsyncMock(return_value=None)) as metaculus,
        patch.object(external, "get_manifold_price", AsyncMock(return_value=0.63)) as manifold,
    ):
        first = await external.find_matching_prices("Will event X happen?")
        second = await external.find_matching_prices("Will event X happen?")

    assert first == second == {"kalshi": 0.61, "manifold": 0.63}
    assert kalshi.await_count == 1
    assert metaculus.await_count == 1
    assert manifold.await_count == 1


def test_kalshi_auth_headers_uses_signed_access_headers():
    external = ExternalSources()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    with (
        patch("data.external_sources.config.KALSHI_API_KEY", "test-key-id"),
        patch("data.external_sources.config.KALSHI_API_SECRET", pem),
    ):
        headers = external._kalshi_auth_headers(
            "GET",
            "https://api.elections.kalshi.com/trade-api/v2/markets?limit=5",
        )

    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert "KALSHI-ACCESS-TIMESTAMP" in headers
    assert "KALSHI-ACCESS-SIGNATURE" in headers
    assert "Authorization" not in headers
