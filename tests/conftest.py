"""Shared test fixtures and configuration."""

import os
import pytest

# Ensure DRY_RUN is always true during tests
os.environ["DRY_RUN"] = "true"
os.environ["AI_PROVIDER"] = "anthropic"
os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ["OPENAI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
