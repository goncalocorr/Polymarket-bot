"""Shared test fixtures and configuration."""

import os
import pytest

# Ensure DRY_RUN is always true during tests
os.environ["DRY_RUN"] = "true"
os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
