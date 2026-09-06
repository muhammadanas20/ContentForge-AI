"""Assemble the final *upload package* folder for a job.

Layout::

    output/<slug>/
        <slug>.mp4              final vertical video
        cover.jpg               thumbnail
        caption.txt             ready-to-paste Instagram caption (+hashtags)
        caption.md              caption, CTA, comment prompt, SEO, YouTube title
        social.json             machine-readable social package
        script.md / script.json narration script
        subtitles.srt/.ass/...  subtitles in requested formats
        transcript.txt/.srt/.json  raw ASR output
        thumbnail_brief.md/.json  Canva brief
        manifest.json           everything above + timings + config snapshot
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from contentforge.log import get_logger
from contentforge.utils import atomic_write_json, atomic_write_text, human_size

log = get_logger("packager")


class UploadPackager:
    def __init__(self, output_root: Path):
        self.output_root = Path(output_root)

    def build(
        self,
        *,
        slug: str,
        final_video: Path,
        files: dict[str, Path | None],
        caption_text: str,
        manifest_extra: dict[str, Any],
    ) -> Path:
        out_dir = self.output_root / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        copied: dict[str, str] = {}

        dst_video = out_dir / f"{slug}.mp4"
        shutil.copy2(final_video, dst_video)
        copied["video"] = dst_video.name

        for key, src in files.items():
            if not src:
                continue
            src = Path(src)
            if not src.exists():
                continue
            dst = out_dir / _target_name(key, src)
            shutil.copy2(src, dst)
            copied[key] = dst.name

        atomic_write_text(out_dir / "caption.txt", caption_text.strip() + "\n")
        copied["caption_txt"] = "caption.txt"

        manifest = {
            "slug": slug,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "video": dst_video.name,
            "video_size": human_size(dst_video.stat().st_size),
            "files": copied,
            **manifest_extra,
        }
        atomic_write_json(out_dir / "manifest.json", manifest)
        atomic_write_text(out_dir / "README.md", _readme(slug, copied, manifest_extra))
        log.info("Upload package ready: %s (%d files)", out_dir, len(copied) + 2)
        return out_dir


def _target_name(key: str, src: Path) -> str:
    mapping = {
        "thumbnail": "cover" + src.suffix,
        "social_md": "caption.md",
        "social_json": "social.json",
        "script_md": "script.md",
        "script_json": "script.json",
        "brief_md": "thumbnail_brief.md",
        "brief_json": "thumbnail_brief.json",
        "narration": "narration.wav",
        "ass": "subtitles.ass",
        "srt": "subtitles.srt",
        "captions_json": "subtitles.json",
        "captions_txt": "subtitles.txt",
        "transcript_txt": "transcript.txt",
        "transcript_srt": "transcript.srt",
        "transcript_json": "transcript.json",
        # v0.3 artefacts
        "grounded_json": "script_grounded.json",
        "quality_json": "quality.json",
        "quality_md": "quality.md",
        "understanding_json": "understanding.json",
        "edit_plan_json": "edit_plan.json",
        "overlay_json": "overlay.json",
    }
    return mapping.get(key, src.name)


def _readme(slug: str, files: dict[str, str], extra: dict[str, Any]) -> str:
    lines = [
        f"# {extra.get('title', slug)}",
        "",
        "Upload checklist:",
        "",
        f"1. Upload `{files.get('video')}` to Instagram Reels / YouTube Shorts.",
        "2. Paste `caption.txt` as the caption (already includes CTA + hashtags).",
        f"3. Optionally set `{files.get('thumbnail', 'cover.jpg')}` as the cover.",
        "4. After 48 h, record views/likes/comments/shares in the dashboard (Analytics tab).",
        "",
        "Files:",
        "",
    ]
    lines += [f"- `{v}` ({k})" for k, v in files.items()]
    return "\n".join(lines) + "\n"
