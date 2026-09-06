"""Optional LLM access with a fully offline fallback.

Providers (selected by ``CONTENTFORGE_LLM_PROVIDER``):

* ``none``   - no network; callers fall back to deterministic rule-based logic.
* ``ollama`` - local models via the Ollama HTTP API (``OLLAMA_BASE_URL``).
* ``openai`` - OpenAI Chat Completions (``OPENAI_API_KEY``). Uses plain
  ``requests`` so no SDK is required and the key is only read from the environment.
* ``gemini`` - Google Gemini via the ``google-generativeai`` SDK
  (``GEMINI_API_KEY``).  Supports both text and vision (multimodal) modes.

The pipeline never *requires* an LLM: every consumer must accept ``None``
from :meth:`LLMClient.complete` and degrade gracefully.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
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
        if provider == "gemini":
            return cls(
                provider="gemini",
                model=env.get("GEMINI_MODEL", "gemini-2.5-flash"),
                api_key=env.get("GEMINI_API_KEY", ""),
            )
        return cls(provider="none")


class LLMClient:
    """Minimal chat-completion client. ``complete`` returns ``None`` on any failure."""

    def __init__(self, config: LLMConfig | None = None, session: requests.Session | None = None):
        self.config = config or LLMConfig.from_env()
        self.session = session or requests.Session()
        self._gemini_model = None  # lazy-init

    @property
    def enabled(self) -> bool:
        if self.config.provider == "openai":
            return bool(self.config.api_key)
        if self.config.provider == "gemini":
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
            if self.config.provider == "gemini":
                return self._gemini(system, user, json_mode)
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

    def complete_with_images(
        self,
        system: str,
        user: str,
        images: list[Path | bytes],
        *,
        json_mode: bool = False,
    ) -> str | None:
        """Send text + images to a vision-capable model.

        Only Gemini supports this natively; other providers silently ignore images.
        Returns ``None`` on any failure (same contract as :meth:`complete`).
        """
        if not self.enabled:
            return None
        try:
            if self.config.provider == "gemini":
                return self._gemini_vision(system, user, images, json_mode)
            # For non-vision providers, fall back to text-only
            return self.complete(system, user, json_mode=json_mode)
        except Exception as exc:
            log.warning(
                "Vision LLM call failed (%s): %s - falling back to rule-based output",
                self.config.provider,
                exc,
            )
        return None

    def complete_vision_json(
        self, system: str, user: str, images: list[Path | bytes]
    ) -> dict[str, Any] | None:
        raw = self.complete_with_images(system, user, images, json_mode=True)
        if not raw:
            return None
        return parse_json_object(raw)

    @property
    def supports_vision(self) -> bool:
        """True when the current provider can process images."""
        return self.config.provider == "gemini" and self.enabled

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

    def _get_gemini_model(self, model_name: str | None = None):
        """Lazy-initialize the Gemini generative model."""
        name = model_name or self.config.model
        try:
            import google.generativeai as genai  # type: ignore[import-untyped]
        except ImportError:
            raise RuntimeError(
                "google-generativeai is required for the Gemini provider. "
                "Install it with: pip install google-generativeai"
            )
        genai.configure(api_key=self.config.api_key)
        return genai.GenerativeModel(name)

    def _gemini(self, system: str, user: str, json_mode: bool) -> str:
        model = self._get_gemini_model()
        prompt = f"{system}\n\n{user}"
        if json_mode:
            prompt += "\n\nRespond ONLY with valid JSON. No markdown fences, no prose."
        generation_config: dict[str, Any] = {"temperature": self.config.temperature}
        if json_mode:
            generation_config["response_mime_type"] = "application/json"
        response = model.generate_content(
            prompt,
            generation_config=generation_config,
        )
        return response.text

    def _gemini_vision(
        self,
        system: str,
        user: str,
        images: list[Path | bytes],
        json_mode: bool,
    ) -> str:
        """Gemini multimodal: text + images."""
        try:
            import google.generativeai as genai  # type: ignore[import-untyped]
            from PIL import Image as PILImage  # type: ignore[import-untyped]
        except ImportError:
            raise RuntimeError(
                "google-generativeai and Pillow are required for Gemini vision. "
                "Install with: pip install google-generativeai Pillow"
            )

        model = self._get_gemini_model()

        # Build the multimodal content
        parts: list[Any] = []

        # System + user prompt as text
        prompt = f"{system}\n\n{user}"
        if json_mode:
            prompt += "\n\nRespond ONLY with valid JSON. No markdown fences, no prose."
        parts.append(prompt)

        # Add images
        for img_src in images:
            if isinstance(img_src, (str, Path)):
                img_path = Path(img_src)
                if img_path.exists():
                    pil_img = PILImage.open(img_path)
                    # Resize large images to save tokens
                    max_dim = 1024
                    if max(pil_img.size) > max_dim:
                        ratio = max_dim / max(pil_img.size)
                        new_size = (int(pil_img.width * ratio), int(pil_img.height * ratio))
                        pil_img = pil_img.resize(new_size, PILImage.LANCZOS)
                    parts.append(pil_img)
            elif isinstance(img_src, bytes):
                # Raw bytes - wrap as PIL image
                import io
                pil_img = PILImage.open(io.BytesIO(img_src))
                parts.append(pil_img)

        generation_config: dict[str, Any] = {"temperature": self.config.temperature}
        if json_mode:
            generation_config["response_mime_type"] = "application/json"

        response = model.generate_content(
            parts,
            generation_config=generation_config,
        )
        return response.text


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
