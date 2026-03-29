"""Tests for the Claude analyst (mocked API)."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from ai.claude_analyst import ClaudeAnalyst


@pytest.fixture
def analyst():
    return ClaudeAnalyst(api_key="test-key")


def mock_response(text: str):
    """Create a mock Anthropic API response."""
    content = MagicMock()
    content.text = text
    response = MagicMock()
    response.content = [content]
    return response


class TestClaudeAnalyst:
    @pytest.mark.asyncio
    async def test_valid_json_response(self, analyst):
        response_text = json.dumps({
            "probability": 0.65,
            "confidence": 0.8,
            "reasoning": "Strong evidence"
        })

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response(response_text))
        analyst._client = mock_client

        result = await analyst.analyse_market(
            {"question": "Will X?", "price": 0.5, "volume": 10000},
            ["News headline 1"]
        )

        assert result["probability"] == 0.65
        assert result["confidence"] == 0.8
        assert result["reasoning"] == "Strong evidence"

    @pytest.mark.asyncio
    async def test_json_in_code_block(self, analyst):
        response_text = '```json\n{"probability": 0.7, "confidence": 0.6, "reasoning": "test"}\n```'

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response(response_text))
        analyst._client = mock_client

        result = await analyst.analyse_market(
            {"question": "Will X?", "price": 0.5}, []
        )
        assert result["probability"] == 0.7

    @pytest.mark.asyncio
    async def test_invalid_json_returns_default(self, analyst):
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response("not json at all"))
        analyst._client = mock_client

        result = await analyst.analyse_market(
            {"question": "Will X?", "price": 0.5}, []
        )
        assert result["probability"] == 0.5
        assert result["confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_clamps_probability(self, analyst):
        response_text = json.dumps({
            "probability": 1.5,
            "confidence": -0.2,
            "reasoning": "clamped"
        })

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response(response_text))
        analyst._client = mock_client

        result = await analyst.analyse_market(
            {"question": "Will X?", "price": 0.5}, []
        )
        assert result["probability"] == 1.0
        assert result["confidence"] == 0.0
