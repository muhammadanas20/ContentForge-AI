"""Audio/video processing built on FFmpeg and OpenCV."""

from contentforge.processing.analysis import FrameAnalysis, analyse_video
from contentforge.processing.audio_mixer import AudioMixer
from contentforge.processing.cursor import CursorTrack, CursorTracker, detect_cursor_track
from contentforge.processing.segments import (
    Timeline,
    build_keep_ranges,
    invert_intervals,
    merge_intervals,
    shrink_silences,
)
from contentforge.processing.video_editor import (
    CropPlan,
    VideoEditor,
    ZoomPulse,
    plan_crop,
    plan_cursor_crop,
    plan_zoom_pulses,
    zoom_expression,
)

__all__ = [
    "AudioMixer",
    "CropPlan",
    "CursorTrack",
    "CursorTracker",
    "FrameAnalysis",
    "Timeline",
    "VideoEditor",
    "ZoomPulse",
    "analyse_video",
    "build_keep_ranges",
    "detect_cursor_track",
    "invert_intervals",
    "merge_intervals",
    "plan_crop",
    "plan_cursor_crop",
    "plan_zoom_pulses",
    "shrink_silences",
    "zoom_expression",
]
