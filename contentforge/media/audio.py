"""Reel audio: narration on top, music ducked under it, light UI clicks.

The v0.2 :class:`~contentforge.processing.audio_mixer.AudioMixer` mixes tracks
at fixed volumes.  For a talking Reel that is not enough: music has to *duck*
whenever the narrator speaks, and click moments deserve a subtle tick so the
edit feels tactile.  Both are done with plain FFmpeg filters (``sidechaincompress``
and a synthesised sine blip) - no sample packs, no extra dependencies.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from contentforge.config.schema import AudioConfig
from contentforge.log import get_logger
from contentforge.utils.ffmpeg import FFmpeg

log = get_logger("audio.reel")


@dataclass
class ReelAudioResult:
    path: Path
    duration: float
    tracks: list[str]
    ducked: bool = False
    clicks: int = 0

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "duration": round(self.duration, 3),
            "tracks": self.tracks,
            "ducked": self.ducked,
            "clicks": self.clicks,
        }


class ReelAudioMixer:
    def __init__(self, config: AudioConfig, ffmpeg: FFmpeg | None = None):
        self.config = config
        self.ff = ffmpeg or FFmpeg()

    def mix(
        self,
        dst: Path,
        *,
        duration: float,
        narration: Path | None = None,
        music: Path | None = None,
        original: Path | None = None,
        click_times: Sequence[float] = (),
        sample_rate: int = 48000,
        click_gain: float = 0.16,
        duck: bool = True,
    ) -> ReelAudioResult:
        cfg = self.config
        duration = max(0.5, duration)
        inputs: list[str] = []
        filters: list[str] = []
        tracks: list[str] = []
        idx = 0

        def add_input(path: Path, loop: bool = False) -> int:
            nonlocal idx
            if loop:
                inputs.extend(["-stream_loop", "-1"])
            inputs.extend(["-i", str(path)])
            i = idx
            idx += 1
            return i

        narration_label = None
        if narration and Path(narration).exists() and cfg.mix.narration_volume > 0:
            i = add_input(Path(narration))
            filters.append(
                f"[{i}:a]aresample={sample_rate},aformat=channel_layouts=stereo,"
                f"volume={cfg.mix.narration_volume:.3f},apad=whole_dur={duration:.3f},"
                f"atrim=0:{duration:.3f}[narr]"
            )
            narration_label = "[narr]"
            tracks.append("narration")

        music_label = None
        if music and Path(music).exists() and cfg.mix.music_volume > 0:
            i = add_input(Path(music), loop=True)
            filters.append(
                f"[{i}:a]aresample={sample_rate},aformat=channel_layouts=stereo,"
                f"volume={cfg.mix.music_volume:.3f},atrim=0:{duration:.3f},"
                f"afade=t=in:st=0:d=0.4,afade=t=out:st={max(0.0, duration - 0.6):.3f}:d=0.6[music]"
            )
            music_label = "[music]"
            tracks.append("music")

        original_label = None
        if original and Path(original).exists() and cfg.mix.original_volume > 0:
            i = add_input(Path(original))
            filters.append(
                f"[{i}:a]aresample={sample_rate},aformat=channel_layouts=stereo,"
                f"volume={cfg.mix.original_volume:.3f},apad=whole_dur={duration:.3f},"
                f"atrim=0:{duration:.3f}[orig]"
            )
            original_label = "[orig]"
            tracks.append("original")

        # ---- music ducking, keyed on the narration
        ducked = False
        if music_label and narration_label and duck:
            filters.append(f"{narration_label}asplit=2[narr_out][narr_key]")
            filters.append(
                "[music][narr_key]sidechaincompress=threshold=0.045:ratio=9:attack=12:"
                "release=320:makeup=1[music_d]"
            )
            narration_label, music_label = "[narr_out]", "[music_d]"
            ducked = True

        # ---- click ticks (synthesised, very quiet)
        click_labels: list[str] = []
        clicks = [t for t in click_times if 0 <= t < duration][:24]
        for n, t in enumerate(clicks):
            label = f"[clk{n}]"
            inputs.extend(["-f", "lavfi", "-t", "0.07", "-i", f"sine=frequency=1650:sample_rate={sample_rate}"])
            src = f"[{idx}:a]"
            idx += 1
            filters.append(
                f"{src}aformat=channel_layouts=stereo,volume={click_gain:.3f},"
                f"afade=t=out:st=0.012:d=0.055,adelay={int(t * 1000)}|{int(t * 1000)},"
                f"apad=whole_dur={duration:.3f},atrim=0:{duration:.3f}{label}"
            )
            click_labels.append(label)

        labels = [x for x in (narration_label, original_label, music_label) if x] + click_labels
        if not labels:
            self.ff.make_silence(dst, duration, sample_rate)
            log.warning("No audio sources - wrote %.1fs of silence", duration)
            return ReelAudioResult(dst, duration, [], False, 0)

        if len(labels) > 1:
            filters.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0[mix]")
            mixed = "[mix]"
        else:
            mixed = labels[0]
        loud = cfg.loudness
        filters.append(
            f"{mixed}alimiter=limit=0.97,loudnorm=I={loud.target_lufs}:TP={loud.true_peak}:"
            f"LRA={loud.lra},aresample={sample_rate},apad=whole_dur={duration:.3f},"
            f"atrim=0:{duration:.3f}[aout]"
        )
        self.ff.run(
            [
                *inputs,
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[aout]",
                "-c:a",
                "pcm_s16le",
                "-ar",
                str(sample_rate),
                str(dst),
            ],
            timeout=1800,
        )
        log.info(
            "Reel audio: %s%s, %d click ticks, %.1fs",
            "+".join(tracks) or "silence",
            " (music ducked)" if ducked else "",
            len(clicks),
            duration,
        )
        return ReelAudioResult(dst, duration, tracks, ducked, len(clicks))

    def measure(self, path: Path) -> dict[str, float]:
        """Peak/RMS levels via ``volumedetect`` - used by the quality gates."""
        out = self.ff.run(
            ["-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
            timeout=600,
        )
        text = f"{getattr(out, 'stderr', '')}{getattr(out, 'stdout', '')}"
        levels: dict[str, float] = {}
        for key, name in (("mean_volume", "mean_db"), ("max_volume", "peak_db")):
            for line in text.splitlines():
                if key in line:
                    try:
                        levels[name] = float(line.split(":")[-1].replace("dB", "").strip())
                    except ValueError:
                        pass
        return levels
