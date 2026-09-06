"""Thin, testable wrapper around the ``ffmpeg``/``ffprobe`` binaries.

All video/audio operations in ContentForge go through this module so that:

* the binary location is resolved in one place (``$FFMPEG_BINARY``, PATH, or
  the ``imageio-ffmpeg`` bundled build as a fallback);
* every invocation is logged at DEBUG with the full command line;
* failures raise :class:`FFmpegError` with the captured stderr.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from contentforge.log import get_logger

log = get_logger("ffmpeg")


class FFmpegError(RuntimeError):
    """Raised when ffmpeg/ffprobe exits non-zero or cannot be found."""

    def __init__(self, message: str, cmd: Sequence[str] | None = None, stderr: str = ""):
        super().__init__(message)
        self.cmd = list(cmd) if cmd else []
        self.stderr = stderr


@dataclass
class MediaInfo:
    """Subset of ffprobe output that the pipeline cares about."""

    path: Path
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    has_video: bool = False
    audio_sample_rate: int = 0
    audio_channels: int = 0
    video_codec: str = ""
    audio_codec: str = ""
    size_bytes: int = 0
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_vertical(self) -> bool:
        return self.height > self.width


def _resolve_binary(name: str) -> str | None:
    env_key = f"{name.upper()}_BINARY"
    explicit = os.environ.get(env_key)
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which(name)
    if found:
        return found
    if name == "ffmpeg":
        try:  # optional fallback used in CI / sandboxes without system ffmpeg
            import imageio_ffmpeg  # type: ignore

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    return None


def ffmpeg_available() -> bool:
    """True when an ffmpeg binary can be located."""
    return _resolve_binary("ffmpeg") is not None


class FFmpeg:
    """Stateless helper exposing high-level ffmpeg operations."""

    def __init__(self, ffmpeg_bin: str | None = None, ffprobe_bin: str | None = None):
        self.ffmpeg_bin = ffmpeg_bin or _resolve_binary("ffmpeg")
        self.ffprobe_bin = ffprobe_bin or _resolve_binary("ffprobe")
        if not self.ffmpeg_bin:
            raise FFmpegError(
                "ffmpeg binary not found. Install it (Fedora: sudo dnf install ffmpeg) "
                "or set FFMPEG_BINARY."
            )

    # ------------------------------------------------------------------ core
    def run(
        self,
        args: Sequence[str],
        *,
        timeout: float | None = 3600,
        quiet: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run ``ffmpeg <args>``. Always adds ``-y -hide_banner -nostdin``."""
        cmd = [self.ffmpeg_bin, "-y", "-hide_banner", "-nostdin"]
        if quiet:
            cmd += ["-loglevel", "error"]
        cmd += [str(a) for a in args]
        log.debug("ffmpeg: %s", " ".join(_shell_quote(c) for c in cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise FFmpegError(f"ffmpeg timed out after {timeout}s", cmd) from exc
        if proc.returncode != 0:
            tail = proc.stderr.strip().splitlines()[-15:]
            raise FFmpegError(
                f"ffmpeg failed (exit {proc.returncode}):\n" + "\n".join(tail),
                cmd,
                proc.stderr,
            )
        return proc

    def version(self) -> str:
        proc = subprocess.run(
            [self.ffmpeg_bin, "-version"], capture_output=True, text=True, check=False
        )
        first = proc.stdout.splitlines()[0] if proc.stdout else ""
        m = re.search(r"ffmpeg version (\S+)", first)
        return m.group(1) if m else first

    # ---------------------------------------------------------------- probe
    def probe(self, path: str | Path) -> MediaInfo:
        """Return :class:`MediaInfo` for ``path``.

        Uses ``ffprobe`` when available, otherwise parses ``ffmpeg -i`` output
        (the imageio bundled build ships without ffprobe).
        """
        p = Path(path)
        if not p.exists():
            raise FFmpegError(f"File not found: {p}")
        if self.ffprobe_bin:
            return self._probe_with_ffprobe(p)
        return self._probe_with_ffmpeg(p)

    def _probe_with_ffprobe(self, p: Path) -> MediaInfo:
        cmd = [
            self.ffprobe_bin,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(p),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise FFmpegError("ffprobe failed", cmd, proc.stderr)
        data = json.loads(proc.stdout or "{}")
        info = MediaInfo(path=p, raw=data, size_bytes=p.stat().st_size)
        fmt = data.get("format", {})
        info.duration = float(fmt.get("duration") or 0.0)
        for s in data.get("streams", []):
            if s.get("codec_type") == "video" and not info.has_video:
                info.has_video = True
                info.width = int(s.get("width") or 0)
                info.height = int(s.get("height") or 0)
                info.video_codec = s.get("codec_name", "")
                info.fps = _parse_rate(s.get("avg_frame_rate") or s.get("r_frame_rate"))
                if not info.duration and s.get("duration"):
                    info.duration = float(s["duration"])
            elif s.get("codec_type") == "audio" and not info.has_audio:
                info.has_audio = True
                info.audio_codec = s.get("codec_name", "")
                info.audio_sample_rate = int(s.get("sample_rate") or 0)
                info.audio_channels = int(s.get("channels") or 0)
        return info

    def _probe_with_ffmpeg(self, p: Path) -> MediaInfo:
        proc = subprocess.run(
            [self.ffmpeg_bin, "-hide_banner", "-i", str(p)],
            capture_output=True,
            text=True,
            check=False,
        )
        text = proc.stderr
        info = MediaInfo(path=p, size_bytes=p.stat().st_size, raw={"stderr": text})
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if m:
            h, mnt, s = m.groups()
            info.duration = int(h) * 3600 + int(mnt) * 60 + float(s)
        vm = re.search(
            r"Stream #\d+:\d+.*?: Video: (\w+).*?(\d{2,5})x(\d{2,5}).*?([\d.]+) fps", text, re.S
        )
        if vm:
            info.has_video = True
            info.video_codec = vm.group(1)
            info.width, info.height = int(vm.group(2)), int(vm.group(3))
            info.fps = float(vm.group(4))
        elif "Video:" in text:
            info.has_video = True
            vm2 = re.search(r"Video: (\w+).*?(\d{2,5})x(\d{2,5})", text)
            if vm2:
                info.video_codec = vm2.group(1)
                info.width, info.height = int(vm2.group(2)), int(vm2.group(3))
        am = re.search(r"Stream #\d+:\d+.*?: Audio: (\w+).*?(\d+) Hz,\s*([\w.]+)", text)
        if am:
            info.has_audio = True
            info.audio_codec = am.group(1)
            info.audio_sample_rate = int(am.group(2))
            layout = am.group(3)
            info.audio_channels = {"mono": 1, "stereo": 2}.get(layout, 2)
        return info

    def duration(self, path: str | Path) -> float:
        return self.probe(path).duration

    # ------------------------------------------------------------ operations
    def extract_audio(
        self, src: str | Path, dst: str | Path, *, sample_rate: int = 16000, channels: int = 1
    ) -> Path:
        """Extract a mono PCM WAV suitable for ASR."""
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        self.run(
            [
                "-i",
                str(src),
                "-vn",
                "-ac",
                str(channels),
                "-ar",
                str(sample_rate),
                "-c:a",
                "pcm_s16le",
                str(dst),
            ]
        )
        return dst

    def detect_silence(
        self, src: str | Path, *, threshold_db: float = -35, min_duration: float = 0.5
    ) -> list[tuple[float, float]]:
        """Return ``[(start, end), ...]`` silent intervals using ``silencedetect``."""
        cmd = [
            self.ffmpeg_bin,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(src),
            "-af",
            f"silencedetect=noise={threshold_db}dB:d={min_duration}",
            "-f",
            "null",
            "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        out = proc.stderr
        starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", out)]
        ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", out)]
        intervals: list[tuple[float, float]] = []
        for i, s in enumerate(starts):
            e = ends[i] if i < len(ends) else None
            if e is None:  # silence runs to end of file
                e = self.duration(src)
            if e > s:
                intervals.append((s, e))
        return intervals

    def extract_frame(self, src: str | Path, dst: str | Path, at: float = 0.0) -> Path:
        """Save a single frame at ``at`` seconds as an image."""
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        self.run(["-ss", f"{at:.3f}", "-i", str(src), "-frames:v", "1", "-update", "1", str(dst)])
        return dst

    def measure_loudness(self, src: str | Path) -> dict[str, float]:
        """First pass of ``loudnorm`` returning measured values."""
        cmd = [
            self.ffmpeg_bin,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(src),
            "-af",
            "loudnorm=print_format=json",
            "-f",
            "null",
            "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        m = re.search(r"\{[^{}]*\}", proc.stderr, re.S)
        if not m:
            return {}
        try:
            data = json.loads(m.group(0))
            return {k: float(v) for k, v in data.items() if _is_float(v)}
        except (json.JSONDecodeError, ValueError):
            return {}

    def concat_files(self, files: Sequence[Path], dst: Path, *, reencode: bool = False) -> Path:
        """Concatenate media files with the concat demuxer."""
        dst = Path(dst)
        list_file = dst.with_suffix(".concat.txt")
        list_file.write_text(
            "".join(f"file '{Path(f).resolve().as_posix()}'\n" for f in files), encoding="utf-8"
        )
        args = ["-f", "concat", "-safe", "0", "-i", str(list_file)]
        args += ["-c", "copy"] if not reencode else ["-c:v", "libx264", "-c:a", "aac"]
        args.append(str(dst))
        try:
            self.run(args)
        finally:
            list_file.unlink(missing_ok=True)
        return dst

    def make_silence(self, dst: Path, duration: float, sample_rate: int = 24000) -> Path:
        dst = Path(dst)
        self.run(
            [
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r={sample_rate}:cl=mono",
                "-t",
                f"{duration:.3f}",
                "-c:a",
                "pcm_s16le",
                str(dst),
            ]
        )
        return dst


def _parse_rate(rate: str | None) -> float:
    if not rate:
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        try:
            return float(num) / float(den) if float(den) else 0.0
        except ValueError:
            return 0.0
    try:
        return float(rate)
    except ValueError:
        return 0.0


def _is_float(v: object) -> bool:
    try:
        float(v)  # type: ignore[arg-type]
        return True
    except (TypeError, ValueError):
        return False


def _shell_quote(s: str) -> str:
    return s if re.fullmatch(r"[\w./:=,+-]+", s) else "'" + s.replace("'", "'\\''") + "'"
