"""Tests for external source rate limiting and failure handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

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
    assert any(
        "Access forbidden (403)" in str(call.args[0])
        for call in mock_debug.call_args_list
        if call.args
    )


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
