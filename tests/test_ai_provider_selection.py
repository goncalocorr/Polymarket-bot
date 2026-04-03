"""Tests for selecting the configured AI provider."""

from ai.claude_analyst import ClaudeAnalyst
from ai.openai_analyst import OpenAIAnalyst
from main import build_analyst


def test_build_analyst_uses_openai_when_configured(monkeypatch):
    monkeypatch.setattr("config.AI_PROVIDER", "openai")
    monkeypatch.setattr("config.OPENAI_API_KEY", "test-openai")
    monkeypatch.setattr("config.ANTHROPIC_API_KEY", "test-anthropic")

    analyst = build_analyst()

    assert isinstance(analyst, OpenAIAnalyst)


def test_build_analyst_uses_anthropic_when_configured(monkeypatch):
    monkeypatch.setattr("config.AI_PROVIDER", "anthropic")
    monkeypatch.setattr("config.OPENAI_API_KEY", "test-openai")
    monkeypatch.setattr("config.ANTHROPIC_API_KEY", "test-anthropic")

    analyst = build_analyst()

    assert isinstance(analyst, ClaudeAnalyst)


def test_build_analyst_auto_prefers_openai_when_key_exists(monkeypatch):
    monkeypatch.setattr("config.AI_PROVIDER", "auto")
    monkeypatch.setattr("config.OPENAI_API_KEY", "test-openai")
    monkeypatch.setattr("config.ANTHROPIC_API_KEY", "test-anthropic")

    analyst = build_analyst()

    assert isinstance(analyst, OpenAIAnalyst)


def test_build_analyst_auto_falls_back_to_anthropic(monkeypatch):
    monkeypatch.setattr("config.AI_PROVIDER", "auto")
    monkeypatch.setattr("config.OPENAI_API_KEY", "")
    monkeypatch.setattr("config.ANTHROPIC_API_KEY", "test-anthropic")

    analyst = build_analyst()

    assert isinstance(analyst, ClaudeAnalyst)

