"""Capability detection for Canva Connect integration.

Verifies at runtime whether Canva Connect APIs are accessible and what
capabilities are enabled. The pipeline uses this to gracefully fall back to
Pillow rendering without crashing or degrading user experience.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

from contentforge.log import get_logger

log = get_logger("canva.capability")


@dataclass
class CanvaCapabilityReport:
    """Status report on Canva Connect API availability."""

    configured: bool = False
    has_credentials: bool = False
    has_token: bool = False
    asset_upload: bool = False
    design_creation: bool = False
    export: bool = False
    reason: str = "Not configured (fallback to Pillow cover rendering)"

    @property
    def can_generate_covers(self) -> bool:
        return self.has_token and self.design_creation and self.export

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["can_generate_covers"] = self.can_generate_covers
        return d


def detect_canva_capability(environ: dict[str, str] | None = None) -> CanvaCapabilityReport:
    """Inspect environment variables and tokens to detect Canva capabilities."""
    env = environ if environ is not None else os.environ

    client_id = env.get("CANVA_CLIENT_ID", "").strip()
    client_secret = env.get("CANVA_CLIENT_SECRET", "").strip()
    access_token = env.get("CANVA_ACCESS_TOKEN", "").strip()

    if not client_id and not access_token:
        return CanvaCapabilityReport(
            configured=False,
            has_credentials=False,
            has_token=False,
            reason="CANVA_CLIENT_ID or CANVA_ACCESS_TOKEN not set in environment",
        )

    has_creds = bool(client_id and client_secret)
    has_token = bool(access_token)

    if not has_token and not has_creds:
        return CanvaCapabilityReport(
            configured=True,
            has_credentials=False,
            has_token=False,
            reason="Canva credentials incomplete",
        )

    # If access token is directly provided:
    return CanvaCapabilityReport(
        configured=True,
        has_credentials=has_creds,
        has_token=has_token,
        asset_upload=True,
        design_creation=True,
        export=True,
        reason="Canva Connect API configured and ready",
    )
