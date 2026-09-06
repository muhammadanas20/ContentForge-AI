"""Canva Connect API integration package for ContentForge-AI v0.4.

Provides optional design creation, asset uploading, and export via Canva Connect APIs,
with automatic capability detection and transparent fallback to local Pillow rendering.
"""

from contentforge.integrations.canva.capability import CanvaCapabilityReport, detect_canva_capability
from contentforge.integrations.canva.client import CanvaClient, CanvaConfig

__all__ = [
    "CanvaClient",
    "CanvaConfig",
    "CanvaCapabilityReport",
    "detect_canva_capability",
]
