"""Per-job processing context: paths, artefacts and persisted state.

The context is serialised to ``<work_dir>/state.json`` after every step so
an interrupted run can resume from the last completed step.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contentforge.utils import atomic_write_json, file_sha1, read_json, slugify


@dataclass
class JobContext:
    job_id: str
    source: Path
    work_dir: Path
    slug: str
    # Populated by steps; all values JSON-serialisable (paths stored as str)
    artifacts: dict[str, str] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    completed_steps: list[str] = field(default_factory=list)
    skipped_steps: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ paths
    @property
    def state_file(self) -> Path:
        return self.work_dir / "state.json"

    def path(self, name: str) -> Path:
        return self.work_dir / name

    def artifact(self, key: str) -> Path | None:
        v = self.artifacts.get(key)
        return Path(v) if v else None

    def set_artifact(self, key: str, path: Path | str) -> Path:
        self.artifacts[key] = str(path)
        return Path(path)

    def has_artifact(self, key: str) -> bool:
        p = self.artifact(key)
        return bool(p and p.exists())

    # ------------------------------------------------------------ state
    def save(self) -> None:
        atomic_write_json(
            self.state_file,
            {
                "job_id": self.job_id,
                "source": str(self.source),
                "work_dir": str(self.work_dir),
                "slug": self.slug,
                "artifacts": self.artifacts,
                "data": self.data,
                "completed_steps": self.completed_steps,
                "skipped_steps": self.skipped_steps,
            },
        )

    @classmethod
    def load(cls, work_dir: Path) -> JobContext | None:
        data = read_json(Path(work_dir) / "state.json")
        if not data:
            return None
        return cls(
            job_id=data["job_id"],
            source=Path(data["source"]),
            work_dir=Path(data["work_dir"]),
            slug=data["slug"],
            artifacts=data.get("artifacts", {}),
            data=data.get("data", {}),
            completed_steps=data.get("completed_steps", []),
            skipped_steps=data.get("skipped_steps", []),
        )

    @classmethod
    def create(cls, source: Path, work_root: Path, job_id: str | None = None) -> JobContext:
        source = Path(source)
        job_id = job_id or uuid.uuid4().hex[:12]
        slug = f"{slugify(source.stem)}-{job_id[:6]}"
        work_dir = Path(work_root) / slug
        work_dir.mkdir(parents=True, exist_ok=True)
        ctx = cls(job_id=job_id, source=source, work_dir=work_dir, slug=slug)
        ctx.data["source_hash"] = file_sha1(source, max_bytes=64 << 20)
        return ctx
