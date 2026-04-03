"""Tests for the OpenAI analyst (mocked API)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ai.openai_analyst import OpenAIAnalyst


@pytest.fixture
def analyst():
    return OpenAIAnalyst(api_key="test-key")


def mock_response(payload: dict, status_code: int = 200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    response.request = MagicMock()
    return response


class TestOpenAIAnalyst:
    @pytest.mark.asyncio
    async def test_valid_output_text_json_response(self, analyst):
        payload = {
            "output_text": '{"probability": 0.62, "confidence": 0.81, "reasoning": "test"}'
        }
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response(payload))
        analyst._client = mock_client

        result = await analyst.analyse_market(
            {"question": "Will X?", "price": 0.5, "volume": 10000},
            ["News headline 1"],
        )

        assert result["probability"] == 0.62
        assert result["confidence"] == 0.81
        assert result["reasoning"] == "test"

    @pytest.mark.asyncio
    async def test_nested_content_response(self, analyst):
        payload = {
            "output": [
                {
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"probability": 0.7, "confidence": 0.6, "reasoning": "nested"}',
                        }
                    ]
                }
            ]
        }
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response(payload))
        analyst._client = mock_client

        result = await analyst.analyse_market(
            {"question": "Will X?", "price": 0.5},
            [],
        )

        assert result["probability"] == 0.7
        assert result["confidence"] == 0.6

    @pytest.mark.asyncio
    async def test_invalid_json_returns_default(self, analyst):
        payload = {"output_text": "not json at all"}
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response(payload))
        analyst._client = mock_client

        result = await analyst.analyse_market(
            {"question": "Will X?", "price": 0.5},
            [],
        )

        assert result["probability"] == 0.5
        assert result["confidence"] == 0.0

