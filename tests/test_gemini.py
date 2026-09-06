"""Tests for Google Gemini LLM provider integration."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from contentforge.ai.llm import LLMClient, LLMConfig


def test_gemini_config_from_env():
    env = {
        "CONTENTFORGE_LLM_PROVIDER": "gemini",
        "GEMINI_API_KEY": "fake_gemini_key_123",
        "GEMINI_MODEL": "gemini-2.5-flash",
    }
    cfg = LLMConfig.from_env(env)
    assert cfg.provider == "gemini"
    assert cfg.api_key == "fake_gemini_key_123"
    assert cfg.model == "gemini-2.5-flash"


def test_gemini_supports_vision_flag():
    cfg = LLMConfig(provider="gemini", api_key="fake_key", model="gemini-2.5-flash")
    client = LLMClient(cfg)
    assert client.supports_vision is True

    cfg_openai = LLMConfig(provider="openai", api_key="fake_key", model="gpt-4o")
    client_openai = LLMClient(cfg_openai)
    assert client_openai.supports_vision is False


def test_gemini_fallback_on_network_error():
    cfg = LLMConfig(provider="gemini", api_key="fake_key")
    client = LLMClient(cfg)

    with patch.object(client, "_gemini", side_effect=Exception("API connection timeout")):
        result = client.complete("System prompt", "User prompt")
        assert result is None  # Must gracefully degrade to None contract
