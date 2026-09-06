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


def test_presets_apply_env_and_none():
    from contentforge.config.loader import ConfigError, apply_preset

    base = yaml.safe_load((REPO / "config" / "config.yaml").read_text())
    s = load_settings(REPO / "config" / "config.yaml", root=REPO, environ={})
    assert s.preset == "student_reel"
    assert s.video.zoom.max_zoom == 1.10  # preset value, not the base 1.12
    assert s.video.crop.follow_cursor.enabled and s.subtitles.word_level.enabled
    assert s.video.branding.watermark_text == "StudentTools.pk" and s.video.progress_bar.enabled
    # env selects another preset; env overrides still win over the preset
    s2 = load_settings(
        REPO / "config" / "config.yaml",
        root=REPO,
        environ={"CONTENTFORGE_PRESET": "fast_preview", "CONTENTFORGE__VIDEO__FPS": "20"},
    )
    assert s2.preset == "fast_preview" and s2.video.width == 720 and s2.video.fps == 20
    assert s2.transcription.model_size == "tiny"
    # 'none' -> raw base config
    s3 = load_settings(
        REPO / "config" / "config.yaml", root=REPO, environ={"CONTENTFORGE_PRESET": "none"}
    )
    assert s3.preset == "none" and s3.video.zoom.max_zoom == 1.12
    # unknown preset / malformed preset are hard errors
    with pytest.raises(ConfigError):
        apply_preset(base, "does_not_exist")
    with pytest.raises(ConfigError):
        apply_preset({"presets": {"x": {"preset": "y"}}}, "x")
    with pytest.raises(ConfigError):
        apply_preset({"presets": {"x": 5}}, "x")
    assert apply_preset({"presets": {}}, None)["preset"] == "none"
