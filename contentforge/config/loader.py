"""Load, merge and validate configuration.

Resolution order (later wins):

1. Built-in defaults from :class:`~contentforge.config.schema.Settings`.
2. ``config/config.yaml`` (or the file pointed to by ``CONTENTFORGE_CONFIG``).
3. The active *preset* (``preset: student_reel`` -> ``presets.student_reel``),
   a named partial config deep-merged on top (``CONTENTFORGE_PRESET`` env var
   selects a different one; ``preset: none`` disables presets).
4. ``config/config.local.yaml`` next to it, if present (git-ignored overrides;
   it may also define presets or pick ``preset:``).
5. Environment variables ``CONTENTFORGE__SECTION__KEY=value``.

Secrets never live in YAML; they are read directly from the environment by the
modules that need them (see :mod:`contentforge.ai.llm`).
"""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from contentforge.config.schema import Settings

ENV_PREFIX = "CONTENTFORGE__"
_SETTINGS: Settings | None = None


class ConfigError(RuntimeError):
    """Raised when the configuration cannot be loaded or is invalid."""


def find_repo_root(start: Path | None = None) -> Path:
    """Walk upwards from ``start`` to find the directory containing ``config/config.yaml``.

    Falls back to the current working directory when nothing is found so the
    package can still be used with an explicit config path.
    """
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "config" / "config.yaml").exists():
            return candidate
    # Package location fallback: <root>/contentforge/config/loader.py
    pkg_root = Path(__file__).resolve().parents[2]
    if (pkg_root / "config" / "config.yaml").exists():
        return pkg_root
    return here


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _coerce(value: str) -> Any:
    """Best-effort YAML coercion for env var strings ("true" -> True, "3" -> 3)."""
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError:
        return value


def _env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Convert ``CONTENTFORGE__A__B=value`` env vars into a nested dict."""
    env = environ if environ is not None else os.environ
    out: dict[str, Any] = {}
    for key, raw in env.items():
        if not key.startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in key[len(ENV_PREFIX) :].split("__") if p]
        if not parts:
            continue
        node = out
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = _coerce(raw)
    return out


def apply_preset(data: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    """Deep-merge the selected preset from ``data["presets"]`` into ``data``.

    ``name`` (env/CLI) wins over ``data["preset"]``.  ``"none"`` / empty disables.
    Unknown names raise :class:`ConfigError` so typos never silently produce a
    default-looking video.
    """
    presets = data.get("presets") or {}
    if not isinstance(presets, dict):
        raise ConfigError("'presets' must be a mapping of name -> partial config")
    chosen = (name if name is not None else data.get("preset", "")) or ""
    chosen = str(chosen).strip()
    if not chosen or chosen.lower() == "none":
        return {**data, "preset": "none"}
    if chosen not in presets:
        raise ConfigError(
            f"Unknown preset '{chosen}'. Available: {', '.join(sorted(presets)) or '-'}"
        )
    override = presets[chosen] or {}
    if not isinstance(override, dict):
        raise ConfigError(f"Preset '{chosen}' must be a mapping")
    for key in ("preset", "presets"):
        if key in override:
            raise ConfigError(f"Preset '{chosen}' may not set '{key}'")
    merged = deep_merge(data, override)
    merged["preset"] = chosen
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Top level of {path} must be a mapping")
    return data


def load_settings(
    config_path: str | Path | None = None,
    *,
    root: Path | None = None,
    environ: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Load and validate settings.

    Args:
        config_path: Explicit YAML path. Defaults to ``$CONTENTFORGE_CONFIG`` or
            ``<repo>/config/config.yaml``.
        root: Repository root used to anchor relative paths.
        environ: Environment mapping (defaults to ``os.environ``); injectable for tests.
        overrides: Final programmatic overrides (highest precedence).
    """
    env = environ if environ is not None else os.environ
    if environ is None:
        load_dotenv(override=False)

    repo_root = Path(root).resolve() if root else find_repo_root()
    path = Path(
        config_path or env.get("CONTENTFORGE_CONFIG") or repo_root / "config" / "config.yaml"
    )
    if not path.is_absolute():
        path = repo_root / path

    data = _read_yaml(path)
    local = path.with_name("config.local.yaml")
    local_data: dict[str, Any] = _read_yaml(local) if local.exists() else {}
    # local file may define/select presets, so merge it before resolving the preset...
    data = deep_merge(data, local_data)
    data = apply_preset(data, env.get("CONTENTFORGE_PRESET"))
    # ...and its explicit values still win over the preset afterwards.
    data = deep_merge(data, {k: v for k, v in local_data.items() if k not in ("preset", "presets")})
    data = deep_merge(data, _env_overrides(env))
    if overrides:
        data = deep_merge(data, overrides)

    data_dir_env = env.get("CONTENTFORGE_DATA_DIR")
    if data_dir_env:
        data.setdefault("paths", {})["data_dir"] = data_dir_env

    try:
        settings = Settings(**data, root=repo_root)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration in {path}:\n{exc}") from exc

    settings = _resolve_paths(settings, repo_root, data_dir_env)
    return settings


def _resolve_paths(settings: Settings, root: Path, data_dir_env: str | None) -> Settings:
    """Anchor relative paths at the repo root (or at ``data_dir`` when overridden)."""
    paths = settings.paths
    if data_dir_env:
        # Rebase every default "data/..." path onto the custom data dir.
        data_dir = Path(data_dir_env)
        data_dir = data_dir if data_dir.is_absolute() else root / data_dir
        rebased: dict[str, Path] = {"data_dir": data_dir}
        for name, value in paths.model_dump().items():
            if name == "data_dir":
                continue
            p = Path(value)
            if p.is_absolute():
                rebased[name] = p
            elif p.parts and p.parts[0] == "data":
                rebased[name] = data_dir.joinpath(*p.parts[1:])
            else:
                rebased[name] = root / p
        paths = paths.model_copy(update=rebased)
        settings = settings.model_copy(update={"paths": paths})
        for section, attr in (
            ("analytics", "report_dir"),
            ("transcription", "download_root"),
        ):
            obj = getattr(settings, section)
            p = Path(getattr(obj, attr))
            if not p.is_absolute() and p.parts and p.parts[0] == "data":
                setattr(obj, attr, data_dir.joinpath(*p.parts[1:]))
        piper_dir = Path(settings.tts.piper.models_dir)
        if not piper_dir.is_absolute() and piper_dir.parts and piper_dir.parts[0] == "data":
            settings.tts.piper.models_dir = data_dir.joinpath(*piper_dir.parts[1:])

    resolved = settings.paths.resolve(root)
    settings = settings.model_copy(update={"paths": resolved})
    for section, attr in (("analytics", "report_dir"), ("transcription", "download_root")):
        obj = getattr(settings, section)
        p = Path(getattr(obj, attr))
        if not p.is_absolute():
            setattr(obj, attr, root / p)
    if not Path(settings.tts.piper.models_dir).is_absolute():
        settings.tts.piper.models_dir = root / settings.tts.piper.models_dir
    return settings


def get_settings() -> Settings:
    """Return the process-wide settings singleton, loading it on first use."""
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS


def reset_settings(settings: Settings | None = None) -> None:
    """Replace (or clear) the cached singleton. Used by tests and the CLI."""
    global _SETTINGS
    _SETTINGS = settings
