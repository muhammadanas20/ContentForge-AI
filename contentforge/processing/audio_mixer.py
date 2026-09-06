"""Audio processing: silence analysis input, cut application, mixing and loudness.

Produces the final audio track that :meth:`VideoEditor.render_final` muxes.
"""

from __future__ import annotations

from pathlib import Path

from contentforge.config.schema import AudioConfig
from contentforge.log import get_logger
from contentforge.processing.segments import Interval
from contentforge.utils.ffmpeg import FFmpeg

log = get_logger("audio")


class AudioMixer:
    def __init__(self, config: AudioConfig, ffmpeg: FFmpeg | None = None):
        self.config = config
        self.ff = ffmpeg or FFmpeg()

    def cut_audio(
        self, src: Path, dst: Path, keep: list[Interval], sample_rate: int = 48000
    ) -> Path:
        """Apply the keep-list to the original audio track (same edit as the video)."""
        filters = []
        for i, (s, e) in enumerate(keep):
            filters.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
        n = len(keep)
        if n > 1:
            filters.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[ac]")
            out_label = "[ac]"
        else:
            out_label = "[a0]"
        filters.append(f"{out_label}aresample={sample_rate},aformat=channel_layouts=stereo[aout]")
        self.ff.run(
            [
                "-i",
                str(src),
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[aout]",
                "-c:a",
                "pcm_s16le",
                str(dst),
            ]
        )
        return dst

    def mix(
        self,
        dst: Path,
        *,
        duration: float,
        narration: Path | None = None,
        original: Path | None = None,
        music: Path | None = None,
        sample_rate: int = 48000,
    ) -> Path:
        """Mix narration + (ducked) original + (quiet, looped) music, then normalise loudness."""
        cfg = self.config
        inputs: list[str] = []
        labels: list[str] = []
        filters: list[str] = []
        idx = 0

        def add(path: Path, volume: float, loop: bool = False) -> None:
            nonlocal idx
            if loop:
                inputs.extend(["-stream_loop", "-1"])
            inputs.extend(["-i", str(path)])
            filters.append(
                f"[{idx}:a]aresample={sample_rate},aformat=channel_layouts=stereo,"
                f"volume={volume:.3f},apad=whole_dur={duration:.3f},atrim=0:{duration:.3f}[s{idx}]"
            )
            labels.append(f"[s{idx}]")
            idx += 1

        if narration and cfg.mix.narration_volume > 0:
            add(narration, cfg.mix.narration_volume)
        if original and cfg.mix.original_volume > 0:
            add(original, cfg.mix.original_volume)
        if music and cfg.mix.music_volume > 0 and Path(music).exists():
            add(Path(music), cfg.mix.music_volume, loop=True)

        if not labels:
            # Nothing to mix -> silence of the right length keeps players happy
            self.ff.make_silence(dst, duration, sample_rate)
            return dst

        if len(labels) > 1:
            filters.append(
                f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0[mix]"
            )
            mixed = "[mix]"
        else:
            mixed = labels[0]
        loud = cfg.loudness
        filters.append(
            f"{mixed}loudnorm=I={loud.target_lufs}:TP={loud.true_peak}:LRA={loud.lra},"
            f"aresample={sample_rate},atrim=0:{duration:.3f}[aout]"
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
            ]
        )
        log.info(
            "Mixed audio: %d source(s), %.1fs, target %.0f LUFS",
            len(labels),
            duration,
            loud.target_lufs,
        )
        return dst

    def normalise(self, src: Path, dst: Path) -> Path:
        loud = self.config.loudness
        self.ff.run(
            [
                "-i",
                str(src),
                "-af",
                f"loudnorm=I={loud.target_lufs}:TP={loud.true_peak}:LRA={loud.lra}",
                "-c:a",
                "pcm_s16le",
                str(dst),
            ]
        )
        return dst
