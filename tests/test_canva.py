"""Tests for Canva integration and capability detection."""

from __future__ import annotations

from contentforge.integrations.canva import CanvaClient, CanvaConfig, detect_canva_capability


def test_canva_capability_not_configured():
    cap = detect_canva_capability({})
    assert cap.configured is False
    assert cap.can_generate_covers is False


def test_canva_capability_with_token():
    env = {"CANVA_ACCESS_TOKEN": "mock_token_xyz"}
    cap = detect_canva_capability(env)
    assert cap.configured is True
    assert cap.can_generate_covers is True


def test_canva_client_fallback_when_unauthenticated(tmp_path):
    client = CanvaClient(CanvaConfig())
    assert client.is_authenticated() is False

    res = client.upload_asset(tmp_path / "dummy.png")
    assert res is None

    design = client.create_design("Test")
    assert design is None
