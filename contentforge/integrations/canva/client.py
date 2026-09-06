"""Canva Connect API client.

Handles OAuth token authentication, asset uploading, design creation,
and design exporting via the official Canva Connect REST APIs.
Degrades gracefully returning None if credentials are missing or network calls fail.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from contentforge.integrations.canva.capability import CanvaCapabilityReport, detect_canva_capability
from contentforge.log import get_logger

log = get_logger("canva.client")


@dataclass
class CanvaConfig:
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    api_base_url: str = "https://api.canva.com/rest/v1"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> CanvaConfig:
        env = environ if environ is not None else os.environ
        return cls(
            client_id=env.get("CANVA_CLIENT_ID", "").strip(),
            client_secret=env.get("CANVA_CLIENT_SECRET", "").strip(),
            access_token=env.get("CANVA_ACCESS_TOKEN", "").strip(),
            refresh_token=env.get("CANVA_REFRESH_TOKEN", "").strip(),
            api_base_url=env.get("CANVA_API_BASE_URL", "https://api.canva.com/rest/v1").rstrip("/"),
        )


class CanvaClient:
    """HTTP client for Canva Connect REST APIs."""

    def __init__(self, config: CanvaConfig | None = None, session: requests.Session | None = None):
        self.config = config or CanvaConfig.from_env()
        self.session = session or requests.Session()

    @property
    def capability(self) -> CanvaCapabilityReport:
        return detect_canva_capability()

    def is_authenticated(self) -> bool:
        return bool(self.config.access_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.access_token}",
            "Content-Type": "application/json",
        }

    def upload_asset(self, file_path: Path | str, name: str = "") -> str | None:
        """Upload an image to Canva assets and return its asset ID."""
        if not self.is_authenticated():
            return None
        p = Path(file_path)
        if not p.exists():
            log.warning("Canva asset upload failed: file %s does not exist", p)
            return None
        try:
            url = f"{self.config.api_base_url}/asset-uploads"
            headers = {"Authorization": f"Bearer {self.config.access_token}"}
            with open(p, "rb") as f:
                resp = self.session.post(
                    url,
                    headers=headers,
                    files={"file": (name or p.name, f, "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png")},
                    timeout=30,
                )
            if resp.status_code in (200, 201):
                data = resp.json()
                asset_id = data.get("asset", {}).get("id") or data.get("id")
                log.info("Uploaded asset %s to Canva (ID: %s)", p.name, asset_id)
                return str(asset_id) if asset_id else None
            log.warning("Canva upload asset returned status %d: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            log.warning("Canva upload asset error: %s", exc)
        return None

    def create_design(self, title: str, asset_id: str | None = None) -> dict[str, Any] | None:
        """Create a new Canva design with preset 9:16 (1080x1920) dimensions."""
        if not self.is_authenticated():
            return None
        try:
            url = f"{self.config.api_base_url}/designs"
            payload: dict[str, Any] = {
                "title": title,
                "design_type": {"name": "instagram_story"},  # 1080x1920 9:16
            }
            if asset_id:
                payload["asset_id"] = asset_id
            resp = self.session.post(url, headers=self._headers(), json=payload, timeout=30)
            if resp.status_code in (200, 201):
                data = resp.json()
                return data.get("design") or data
            log.warning("Canva create design returned status %d: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            log.warning("Canva create design error: %s", exc)
        return None

    def export_design(self, design_id: str, output_path: Path | str, format: str = "png") -> Path | None:
        """Export a Canva design to a local file."""
        if not self.is_authenticated():
            return None
        out = Path(output_path)
        try:
            # 1. Initiate export job
            url = f"{self.config.api_base_url}/exports"
            payload = {"design_id": design_id, "format": format}
            resp = self.session.post(url, headers=self._headers(), json=payload, timeout=30)
            if resp.status_code not in (200, 201):
                log.warning("Canva export job failed: %d %s", resp.status_code, resp.text[:200])
                return None
            job_id = resp.json().get("job", {}).get("id") or resp.json().get("id")
            if not job_id:
                return None

            # 2. Poll for completion
            for _ in range(15):
                time.sleep(2)
                check_resp = self.session.get(f"{url}/{job_id}", headers=self._headers(), timeout=15)
                if check_resp.status_code == 200:
                    status_data = check_resp.json()
                    status = status_data.get("job", {}).get("status") or status_data.get("status")
                    if status == "success":
                        urls = status_data.get("export", {}).get("urls") or status_data.get("urls", [])
                        if urls:
                            dl_resp = self.session.get(urls[0], timeout=60)
                            if dl_resp.status_code == 200:
                                out.parent.mkdir(parents=True, exist_ok=True)
                                out.write_bytes(dl_resp.content)
                                log.info("Exported Canva design %s to %s", design_id, out)
                                return out
                    elif status == "failed":
                        log.warning("Canva export job %s failed", job_id)
                        return None
        except Exception as exc:
            log.warning("Canva export design error: %s", exc)
        return None

    def generate_cover(
        self,
        screenshot_path: Path,
        headline: str,
        subtitle: str,
        output_path: Path,
    ) -> Path | None:
        """High-level cover generation flow via Canva."""
        if not self.is_authenticated():
            return None
        asset_id = self.upload_asset(screenshot_path, name="cover_screenshot")
        design = self.create_design(title=f"Cover: {headline[:30]}", asset_id=asset_id)
        if not design or "id" not in design:
            return None
        design_id = str(design["id"])
        return self.export_design(design_id, output_path)
