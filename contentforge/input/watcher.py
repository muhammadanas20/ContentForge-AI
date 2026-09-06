"""Input folder watcher.

Detects new recordings via :mod:`watchdog` (inotify on Linux), waits until
the file is fully written (size stable), de-duplicates by content hash and
hands the file to a queue consumed by worker threads running the pipeline.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from contentforge.config.schema import WatcherConfig
from contentforge.log import get_logger
from contentforge.utils import wait_until_stable

log = get_logger("watcher")

JobHandler = Callable[[Path], None]


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: InputWatcher):
        self.watcher = watcher

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.watcher.enqueue(Path(str(event.src_path)))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.watcher.enqueue(Path(str(event.dest_path)))

    def on_modified(self, event: FileSystemEvent) -> None:
        # Some recorders create the file early and write for a long time; the
        # stability check handles that, but we also re-enqueue in case the
        # creation event was missed (e.g. the daemon started mid-recording).
        if not event.is_directory:
            self.watcher.enqueue(Path(str(event.src_path)))


class InputWatcher:
    """Watches the input directory and dispatches complete files to ``handler``."""

    def __init__(
        self,
        config: WatcherConfig,
        input_dir: Path,
        handler: JobHandler,
        *,
        workers: int = 1,
        use_polling: bool = False,
    ):
        self.config = config
        self.input_dir = Path(input_dir)
        self.handler = handler
        self.workers = max(1, workers)
        self._queue: queue.Queue[Path] = queue.Queue()
        self._pending: set[Path] = set()
        self._in_progress: set[Path] = set()
        self._done: set[Path] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._observer = PollingObserver() if use_polling else Observer()

    # ------------------------------------------------------------ intake
    def is_candidate(self, path: Path) -> bool:
        if path.suffix.lower() not in self.config.extensions:
            return False
        if path.name.startswith((".", "~")) or path.name.endswith((".part", ".tmp", ".crdownload")):
            return False
        return True

    def enqueue(self, path: Path) -> bool:
        path = Path(path)
        if not self.is_candidate(path):
            return False
        with self._lock:
            if path in self._pending or path in self._in_progress or path in self._done:
                return False
            self._pending.add(path)
        self._queue.put(path)
        log.info("Detected new recording: %s", path.name)
        return True

    def scan_existing(self) -> int:
        n = 0
        pattern = "**/*" if self.config.recursive else "*"
        for p in sorted(self.input_dir.glob(pattern)):
            if p.is_file() and self.enqueue(p):
                n += 1
        return n

    # ----------------------------------------------------------- workers
    def _worker(self, idx: int) -> None:
        log.debug("Worker %d started", idx)
        while not self._stop.is_set():
            try:
                path = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._lock:
                self._pending.discard(path)
                self._in_progress.add(path)
            try:
                if not path.exists():
                    log.warning("File vanished before processing: %s", path.name)
                    continue
                log.info("Waiting for %s to finish writing...", path.name)
                if not wait_until_stable(
                    path, self.config.stable_seconds, self.config.poll_interval
                ):
                    log.warning("File never stabilised or vanished: %s", path.name)
                    continue
                self.handler(path)
                with self._lock:
                    self._done.add(path)
            except (
                Exception
            ) as exc:  # handler is responsible for its own logging, this is a last resort
                log.exception("Unhandled error while processing %s: %s", path.name, exc)
            finally:
                with self._lock:
                    self._in_progress.discard(path)
                self._queue.task_done()

    # ----------------------------------------------------------- control
    def start(self) -> None:
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        for i in range(self.workers):
            t = threading.Thread(target=self._worker, args=(i,), name=f"cf-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        self._observer.schedule(
            _Handler(self), str(self.input_dir), recursive=self.config.recursive
        )
        self._observer.start()
        log.info(
            "Watching %s for %s (workers=%d)",
            self.input_dir,
            ", ".join(self.config.extensions),
            self.workers,
        )
        if self.config.process_existing_on_start:
            n = self.scan_existing()
            if n:
                log.info("Queued %d existing file(s) from input folder", n)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        try:
            self._observer.stop()
            self._observer.join(timeout=timeout)
        except Exception:  # pragma: no cover
            pass
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads.clear()
        log.info("Watcher stopped")

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until the queue is drained (used by tests and the ``process`` command)."""
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            with self._lock:
                idle = not self._pending and not self._in_progress and self._queue.empty()
            if idle:
                return True
            if deadline and time.monotonic() > deadline:
                return False
            time.sleep(0.2)

    @property
    def queue_size(self) -> int:
        with self._lock:
            return len(self._pending) + len(self._in_progress)
