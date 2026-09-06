"""Pydantic schema for the ContentForge configuration.

Every section of ``config/config.yaml`` has a model here so that typos and
invalid values are caught at start-up instead of deep inside a render.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Model(BaseModel):
    """Base model: forbid unknown keys so config typos fail loudly."""

    model_config = ConfigDict(extra="forbid")


class ProjectConfig(_Model):
    name: str = "ContentForge-AI"
    brand: str = "StudentTools.pk"
    website: str = "StudentTools.pk"
    timezone: str = "UTC"


class PathsConfig(_Model):
    data_dir: Path = Path("data")
    input: Path = Path("data/input")
    work: Path = Path("data/work")
    output: Path = Path("data/output")
    archive: Path = Path("data/archive")
    logs: Path = Path("data/logs")
    assets: Path = Path("data/assets")
    models: Path = Path("data/models")
    db: Path = Path("data/db/contentforge.sqlite3")

    def resolve(self, root: Path) -> PathsConfig:
        """Return a copy with all relative paths anchored at ``root``."""
        data = {}
        for name, value in self.model_dump().items():
            p = Path(value)
            data[name] = p if p.is_absolute() else (root / p)
        return PathsConfig(**data)


class WatcherConfig(_Model):
    enabled: bool = True
    extensions: list[str] = Field(default_factory=lambda: [".mp4", ".mkv", ".mov", ".webm"])
    stable_seconds: float = 5
    poll_interval: float = 2
    process_existing_on_start: bool = True
    recursive: bool = False

    @field_validator("extensions")
    @classmethod
    def _normalise_ext(cls, v: list[str]) -> list[str]:
        return [e.lower() if e.startswith(".") else f".{e.lower()}" for e in v]


class PipelineSteps(_Model):
    transcribe: bool = True
    script: bool = True
    narration: bool = True
    subtitles: bool = True
    silence_removal: bool = True
    jump_cuts: bool = True
    vertical_crop: bool = True
    auto_zoom: bool = True
    progress_bar: bool = True
    branding: bool = True
    thumbnail: bool = True
    social: bool = True
    analytics: bool = True
    package: bool = True
    archive: bool = True


class PipelineConfig(_Model):
    max_workers: int = Field(1, ge=1, le=8)
    retries: int = Field(2, ge=0, le=10)
    retry_backoff_seconds: float = 5
    cleanup_work_on_success: bool = True
    steps: PipelineSteps = Field(default_factory=PipelineSteps)


class SilenceConfig(_Model):
    threshold_db: float = -35
    min_duration: float = 0.6
    keep_padding: float = 0.15


class LoudnessConfig(_Model):
    target_lufs: float = -14
    true_peak: float = -1.5
    lra: float = 11


class MixConfig(_Model):
    original_volume: float = Field(0.0, ge=0, le=2)
    narration_volume: float = Field(1.0, ge=0, le=2)
    music_volume: float = Field(0.08, ge=0, le=1)
    background_music: str = ""


class AudioConfig(_Model):
    sample_rate: int = 16000
    channels: int = 1
    silence: SilenceConfig = Field(default_factory=SilenceConfig)
    loudness: LoudnessConfig = Field(default_factory=LoudnessConfig)
    mix: MixConfig = Field(default_factory=MixConfig)


class TranscriptionConfig(_Model):
    engine: Literal["faster-whisper"] = "faster-whisper"
    model_size: str = "base"
    device: Literal["cpu", "cuda", "auto"] = "cpu"
    compute_type: str = "int8"
    language: str | None = "en"
    beam_size: int = 5
    vad_filter: bool = True
    word_timestamps: bool = True
    download_root: Path = Path("data/models/whisper")


class ScriptConfig(_Model):
    style: str = "student-friendly"
    target_words_min: int = 60
    target_words_max: int = 140
    max_duration_seconds: int = 60
    hook_styles: list[str] = Field(default_factory=lambda: ["question", "shock", "problem"])
    cta_default: str = "Follow StudentTools.pk for more free student tools."
    speaking_rate_wps: float = 2.6
    strict_grounding: bool = True


class PiperConfig(_Model):
    voice: str = "en_US-lessac-medium"
    models_dir: Path = Path("data/models/piper")
    auto_download: bool = True
    length_scale: float = 1.0
    noise_scale: float = 0.667
    noise_w: float = 0.8


class KokoroConfig(_Model):
    voice: str = "af_heart"
    lang_code: str = "a"


class EdgeConfig(_Model):
    voice: str = "en-US-AriaNeural"
    rate: str = "+0%"
    pitch: str = "+0Hz"


class TTSConfig(_Model):
    engine: Literal["piper", "kokoro", "edge"] = "piper"
    speed: float = 1.0
    output_sample_rate: int = 24000
    piper: PiperConfig = Field(default_factory=PiperConfig)
    kokoro: KokoroConfig = Field(default_factory=KokoroConfig)
    edge: EdgeConfig = Field(default_factory=EdgeConfig)


class CropConfig(_Model):
    mode: Literal["smart", "center", "left", "right", "blur-pad"] = "smart"
    sample_fps: float = 2
    smoothing: float = Field(0.85, ge=0, le=1)


class ZoomConfig(_Model):
    enabled: bool = True
    max_zoom: float = Field(1.12, ge=1.0, le=2.0)
    interval_seconds: float = 6
    duration_seconds: float = 2.5
    ease: Literal["linear", "in-out"] = "in-out"


class JumpCutConfig(_Model):
    enabled: bool = True
    min_gap_seconds: float = 1.2
    motion_threshold: float = 2.0


class TransitionConfig(_Model):
    enabled: bool = True
    type: Literal["fade", "none"] = "fade"
    duration: float = 0.08


class ProgressBarConfig(_Model):
    enabled: bool = True
    height: int = 14
    color: str = "#FF7A00"
    background: str = "#00000080"
    position: Literal["top", "bottom"] = "top"


class BrandingConfig(_Model):
    enabled: bool = True
    watermark_text: str = "StudentTools.pk"
    watermark_position: Literal["top-left", "top-right", "bottom-left", "bottom-right"] = (
        "top-right"
    )
    watermark_opacity: float = Field(0.85, ge=0, le=1)
    font_size: int = 38
    logo_path: str = ""
    logo_width: int = 180
    intro_title: bool = True
    outro_cta: bool = True
    primary_color: str = "#FF7A00"
    secondary_color: str = "#FFFFFF"
    background_color: str = "#0F0F14"


class VideoConfig(_Model):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    crf: int = Field(20, ge=0, le=51)
    preset: str = "medium"
    pix_fmt: str = "yuv420p"
    audio_bitrate: str = "192k"
    max_duration_seconds: int = 90
    crop: CropConfig = Field(default_factory=CropConfig)
    zoom: ZoomConfig = Field(default_factory=ZoomConfig)
    jump_cuts: JumpCutConfig = Field(default_factory=JumpCutConfig)
    transitions: TransitionConfig = Field(default_factory=TransitionConfig)
    progress_bar: ProgressBarConfig = Field(default_factory=ProgressBarConfig)
    branding: BrandingConfig = Field(default_factory=BrandingConfig)


class SubtitleConfig(_Model):
    enabled: bool = True
    style: Literal["bold-pop", "clean", "karaoke", "minimal"] = "bold-pop"
    font: str = "DejaVu Sans"
    font_size: int = 68
    max_chars_per_line: int = 22
    max_words_per_caption: int = 4
    position_v: float = Field(0.72, ge=0, le=1)
    primary_color: str = "#FFFFFF"
    highlight_color: str = "#FFD400"
    outline_color: str = "#000000"
    outline_width: int = 5
    shadow: int = 2
    highlight_keywords: list[str] = Field(default_factory=list)
    uppercase: bool = True
    formats: list[str] = Field(default_factory=lambda: ["srt", "ass", "json", "txt"])


class ThumbnailConfig(_Model):
    enabled: bool = True
    width: int = 1080
    height: int = 1920
    frame_position: float = Field(0.25, ge=0, le=1)
    overlay_title: bool = True
    font: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    title_font_size: int = 96
    dark_overlay_alpha: float = Field(0.45, ge=0, le=1)
    accent_color: str = "#FF7A00"
    text_color: str = "#FFFFFF"
    canva_brief: bool = True


class HashtagConfig(_Model):
    count: int = Field(18, ge=1, le=30)
    categories: list[str] = Field(default_factory=lambda: ["brand", "students", "viral"])
    banned: list[str] = Field(default_factory=list)
    rotation_memory: int = 5


class SocialConfig(_Model):
    platform_defaults: list[str] = Field(default_factory=lambda: ["instagram"])
    caption_max_length: int = 2200
    hashtags: HashtagConfig = Field(default_factory=HashtagConfig)
    cta_variants: list[str] = Field(default_factory=list)
    comment_prompts: list[str] = Field(default_factory=list)


class Benchmarks(_Model):
    completion_rate_good: float = 0.55
    completion_rate_poor: float = 0.30
    like_rate_good: float = 0.06
    share_rate_good: float = 0.01
    comment_rate_good: float = 0.005


class AnalyticsConfig(_Model):
    enabled: bool = True
    benchmarks: Benchmarks = Field(default_factory=Benchmarks)
    report_dir: Path = Path("data/output/reports")


class CleanupConfig(_Model):
    enabled: bool = True
    delete_raw_after_success: bool = False
    delete_temp_after_success: bool = True
    archive_retention_days: int = 30
    output_retention_days: int = 0
    min_free_disk_gb: float = 5
    min_age_minutes: int = 30
    logs_retention_days: int = 30


class SchedulerConfig(_Model):
    enabled: bool = True
    mode: Literal["immediate", "scheduled"] = "immediate"
    process_cron: str = "0 */2 * * *"
    daily_cleanup_cron: str = "30 3 * * *"
    weekly_analytics_cron: str = "0 9 * * 1"
    monthly_report_cron: str = "0 9 1 * *"


class DatabaseConfig(_Model):
    backend: Literal["sqlite", "postgres"] = "sqlite"


class LoggingConfig(_Model):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    console: bool = True
    rich_tracebacks: bool = True
    file_rotation: Literal["daily"] = "daily"
    retention_days: int = 30
    json_lines: bool = False


class DashboardConfig(_Model):
    host: str = "0.0.0.0"
    port: int = 8501
    refresh_seconds: int = 10
    log_tail_lines: int = 200


class Settings(_Model):
    """Root settings object."""

    project: ProjectConfig = Field(default_factory=ProjectConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    script: ScriptConfig = Field(default_factory=ScriptConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    subtitles: SubtitleConfig = Field(default_factory=SubtitleConfig)
    thumbnail: ThumbnailConfig = Field(default_factory=ThumbnailConfig)
    social: SocialConfig = Field(default_factory=SocialConfig)
    analytics: AnalyticsConfig = Field(default_factory=AnalyticsConfig)
    cleanup: CleanupConfig = Field(default_factory=CleanupConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)

    # Populated by the loader; not part of YAML.
    root: Path = Field(default_factory=Path.cwd, exclude=True)

    def ensure_directories(self) -> None:
        """Create all runtime directories (idempotent)."""
        for p in (
            self.paths.input,
            self.paths.work,
            self.paths.output,
            self.paths.archive,
            self.paths.logs,
            self.paths.assets,
            self.paths.models,
            self.paths.db.parent,
            self.analytics.report_dir,
            self.transcription.download_root,
            self.tts.piper.models_dir,
        ):
            Path(p).mkdir(parents=True, exist_ok=True)
