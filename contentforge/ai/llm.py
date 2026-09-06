"""Optional LLM access with a fully offline fallback.

Providers (selected by ``CONTENTFORGE_LLM_PROVIDER``):

* ``none``   - no network; callers fall back to deterministic rule-based logic.
* ``ollama`` - local models via the Ollama HTTP API (``OLLAMA_BASE_URL``).
* ``openai`` - OpenAI Chat Completions (``OPENAI_API_KEY``). Uses plain
  ``requests`` so no SDK is required and the key is only read from the environment.

The pipeline never *requires* an LLM: every consumer must accept ``None``
from :meth:`LLMClient.complete` and degrade gracefully.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import requests

from contentforge.log import get_logger

log = get_logger("llm")


@dataclass
class LLMConfig:
    provider: str = "none"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    timeout: float = 90.0
    temperature: float = 0.7

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> LLMConfig:
        env = environ if environ is not None else os.environ
        provider = (env.get("CONTENTFORGE_LLM_PROVIDER") or "none").strip().lower()
        if provider == "openai":
            return cls(
                provider="openai",
                model=env.get("OPENAI_MODEL", "gpt-4o-mini"),
                base_url=env.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                api_key=env.get("OPENAI_API_KEY", ""),
            )
        if provider == "ollama":
            return cls(
                provider="ollama",
                model=env.get("OLLAMA_MODEL", "llama3.1"),
                base_url=env.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            )
        return cls(provider="none")


class LLMClient:
    """Minimal chat-completion client. ``complete`` returns ``None`` on any failure."""

    def __init__(self, config: LLMConfig | None = None, session: requests.Session | None = None):
        self.config = config or LLMConfig.from_env()
        self.session = session or requests.Session()

    @property
    def enabled(self) -> bool:
        if self.config.provider == "openai":
            return bool(self.config.api_key)
        return self.config.provider == "ollama"

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str | None:
        if not self.enabled:
            return None
        try:
            if self.config.provider == "openai":
                return self._openai(system, user, json_mode)
            if self.config.provider == "ollama":
                return self._ollama(system, user, json_mode)
        except Exception as exc:  # network errors, bad JSON, rate limits...
            log.warning(
                "LLM call failed (%s): %s - falling back to rule-based output",
                self.config.provider,
                exc,
            )
        return None

    def complete_json(self, system: str, user: str) -> dict[str, Any] | None:
        raw = self.complete(system, user, json_mode=True)
        if not raw:
            return None
        return parse_json_object(raw)

    # ------------------------------------------------------------ providers
    def _openai(self, system: str, user: str, json_mode: bool) -> str:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = self.session.post(
            f"{self.config.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            json=payload,
            timeout=self.config.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _ollama(self, system: str, user: str, json_mode: bool) -> str:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "stream": False,
            "options": {"temperature": self.config.temperature},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["format"] = "json"
        resp = self.session.post(
            f"{self.config.base_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=self.config.timeout,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from ``text`` (tolerates ``` fences and prose)."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.M)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
