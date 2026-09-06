"""Move processed source recordings (and optionally work artefacts) into dated archive folders."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from contentforge.log import get_logger
from contentforge.utils import atomic_write_json

log = get_logger("archive")


class Archiver:
    def __init__(self, archive_root: Path):
        self.root = Path(archive_root)

    def archive_job(
        self,
        *,
        job_id: str,
        slug: str,
        source: Path,
        extra_files: list[Path] | None = None,
        delete_source: bool = True,
        metadata: dict | None = None,
    ) -> Path:
        """Archive the raw recording under ``archive/YYYY-MM/<slug>/`` and return the folder."""
        dest = self.root / datetime.now().strftime("%Y-%m") / slug
        dest.mkdir(parents=True, exist_ok=True)
        source = Path(source)
        if source.exists():
            target = dest / source.name
            if delete_source:
                shutil.move(str(source), str(target))
            else:
                shutil.copy2(source, target)
        for f in extra_files or []:
            f = Path(f)
            if f.exists():
                shutil.copy2(f, dest / f.name)
        atomic_write_json(
            dest / "archive.json",
            {
                "job_id": job_id,
                "slug": slug,
                "archived_at": datetime.now().isoformat(timespec="seconds"),
                "source_name": source.name,
                **(metadata or {}),
            },
        )
        log.info("Archived %s -> %s", source.name, dest)
        return dest

    def list_archives(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted((p for p in self.root.glob("*/*") if p.is_dir()), reverse=True)
