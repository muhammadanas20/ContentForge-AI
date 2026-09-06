"""Configuration loading and validation tests."""

from pathlib import Path

import pytest
import yaml

from contentforge.config import ConfigError, deep_merge, load_settings
from contentforge.config.loader import _env_overrides

REPO = Path(__file__).resolve().parents[1]


def test_default_config_loads(settings):
    assert settings.project.brand == "StudentTools.pk"
    assert settings.video.width == 1080 and settings.video.height == 1920
    assert settings.paths.input.is_absolute()


def test_data_dir_env_rebases_paths(settings, tmp_path):
    assert settings.paths.input == tmp_path / "data" / "input"
    assert settings.paths.db.parent == tmp_path / "data" / "db"
    assert settings.analytics.report_dir == tmp_path / "data" / "output" / "reports"
    assert settings.tts.piper.models_dir == tmp_path / "data" / "models" / "piper"


def test_deep_merge_nested():
    base = {"a": {"b": 1, "c": 2}, "x": 1}
    over = {"a": {"c": 3}, "y": 2}
    merged = deep_merge(base, over)
    assert merged == {"a": {"b": 1, "c": 3}, "x": 1, "y": 2}
    assert base["a"]["c"] == 2  # original untouched


def test_env_overrides_nested_and_coerced():
    env = {
        "CONTENTFORGE__TTS__ENGINE": "edge",
        "CONTENTFORGE__VIDEO__CRF": "18",
        "OTHER": "x",
        "CONTENTFORGE__WATCHER__ENABLED": "false",
    }
    out = _env_overrides(env)
    assert out == {"tts": {"engine": "edge"}, "video": {"crf": 18}, "watcher": {"enabled": False}}


def test_env_override_applied(tmp_path):
    env = {"CONTENTFORGE_DATA_DIR": str(tmp_path), "CONTENTFORGE__TTS__ENGINE": "edge"}
    s = load_settings(REPO / "config" / "config.yaml", root=REPO, environ=env)
    assert s.tts.engine == "edge"


def test_local_override_file(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    base = yaml.safe_load((REPO / "config" / "config.yaml").read_text())
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump(base))
    (cfg_dir / "config.local.yaml").write_text(yaml.safe_dump({"video": {"fps": 24}}))
    s = load_settings(cfg_dir / "config.yaml", root=tmp_path, environ={})
    assert s.video.fps == 24


def test_invalid_key_rejected(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"video": {"not_a_real_key": 1}}))
    with pytest.raises(ConfigError):
        load_settings(cfg, root=tmp_path, environ={})


def test_invalid_value_rejected(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"tts": {"engine": "nonexistent"}}))
    with pytest.raises(ConfigError):
        load_settings(cfg, root=tmp_path, environ={})


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.yaml", root=tmp_path, environ={})
