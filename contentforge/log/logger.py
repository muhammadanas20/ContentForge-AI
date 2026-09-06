"""Rich console logging + dated file logging.

Log files are written to ``<logs>/contentforge-YYYY-MM-DD.log``. A new file
is started automatically at midnight and files older than the configured
retention are pruned on start-up.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

LOGGER_NAME = "contentforge"
_CONFIGURED = False
_LOG_DIR: Path | None = None


class DailyFileHandler(logging.Handler):
    """File handler that switches to a new file when the calendar date changes."""

    def __init__(self, log_dir: Path, prefix: str = "contentforge", json_lines: bool = False):
        super().__init__()
        self.log_dir = Path(log_dir)
        self.prefix = prefix
        self.json_lines = json_lines
        self._current_date: str | None = None
        self._stream = None
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, date_str: str) -> Path:
        return self.log_dir / f"{self.prefix}-{date_str}.log"

    def _rotate_if_needed(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._current_date:
            if self._stream:
                self._stream.close()
            self._current_date = today
            self._stream = self._path_for(today).open("a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._rotate_if_needed()
            if self.json_lines:
                payload = {
                    "ts": datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                }
                if record.exc_info:
                    payload["exc"] = logging.Formatter().formatException(record.exc_info)
                line = json.dumps(payload, ensure_ascii=False)
            else:
                line = self.format(record)
            assert self._stream is not None
            self._stream.write(line + "\n")
            self._stream.flush()
        except Exception:  # pragma: no cover - never let logging crash the app
            self.handleError(record)

    def close(self) -> None:
        if self._stream:
            self._stream.close()
            self._stream = None
        super().close()


def prune_old_logs(log_dir: Path, retention_days: int, prefix: str = "contentforge") -> int:
    """Delete dated log files older than ``retention_days``. Returns count removed."""
    if retention_days <= 0 or not log_dir.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=retention_days)
    removed = 0
    for f in log_dir.glob(f"{prefix}-*.log"):
        try:
            date_part = f.stem[len(prefix) + 1 :]
            if datetime.strptime(date_part, "%Y-%m-%d") < cutoff:
                f.unlink()
                removed += 1
        except ValueError:
            continue
    return removed


def setup_logging(
    log_dir: Path | str,
    level: str = "INFO",
    *,
    console: bool = True,
    rich_tracebacks: bool = True,
    json_lines: bool = False,
    retention_days: int = 30,
    force: bool = False,
) -> logging.Logger:
    """Configure the ``contentforge`` logger hierarchy. Idempotent unless ``force``."""
    global _CONFIGURED, _LOG_DIR
    logger = logging.getLogger(LOGGER_NAME)
    if _CONFIGURED and not force:
        return logger

    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()

    env_level = os.environ.get("CONTENTFORGE_LOG_LEVEL")
    logger.setLevel(getattr(logging, (env_level or level).upper(), logging.INFO))
    logger.propagate = False

    log_dir = Path(log_dir)
    _LOG_DIR = log_dir
    prune_old_logs(log_dir, retention_days)

    file_handler = DailyFileHandler(log_dir, json_lines=json_lines)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    )
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    if console:
        rich_handler = RichHandler(
            console=Console(stderr=False),
            rich_tracebacks=rich_tracebacks,
            show_path=False,
            markup=True,
        )
        rich_handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))
        logger.addHandler(rich_handler)

    _CONFIGURED = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger, e.g. ``get_logger("pipeline")`` -> ``contentforge.pipeline``."""
    if not name:
        return logging.getLogger(LOGGER_NAME)
    if name.startswith(LOGGER_NAME):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def list_log_files(log_dir: Path | str | None = None) -> list[Path]:
    """Return dated log files, newest first."""
    d = Path(log_dir) if log_dir else _LOG_DIR
    if d is None or not d.exists():
        return []
    return sorted(d.glob("contentforge-*.log"), reverse=True)


def tail_log(path: Path | str, lines: int = 200) -> list[str]:
    """Return the last ``lines`` lines of a log file (memory-friendly)."""
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        return list(deque(fh, maxlen=lines))
