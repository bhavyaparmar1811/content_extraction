"""Immutable, versioned job artifacts: ``data/migrations/{job_id}/{kind}[_{scope}]_v{n}.json``
(``.docx`` for the rendered document).

A write never overwrites. Each one gets the next version for its (kind, scope),
a sha256 of the exact bytes on disk, and an ``ArtifactRef`` in the store.
Readers verify the hash, so an edited or corrupted file is caught.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

from app.schemas.v2 import ArtifactKind, ArtifactRef, MigrationJob
from app.stores.migration_store import MigrationStore

M = TypeVar("M", bound=BaseModel)


class ArtifactCorrupted(RuntimeError):
    pass


def _safe(scope: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", scope).strip("_") or "scope"


class ArtifactWriter:
    def __init__(self, base_dir: Path, store: MigrationStore):
        self.base_dir = Path(base_dir)
        self.store = store

    def job_dir(self, job_id: str) -> Path:
        return self.base_dir / job_id

    def write(
        self,
        job_id: str,
        kind: ArtifactKind,
        payload: BaseModel | dict,
        scope: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> ArtifactRef:
        data = payload.to_clean_dict() if hasattr(payload, "to_clean_dict") else (
            payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        )
        raw = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        version = self.store.next_artifact_version(job_id, kind, scope)
        name = f"{kind.value}{'_' + _safe(scope) if scope else ''}_v{version}.json"
        path = self.job_dir(job_id) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():  # never overwrite, even if the store and the disk disagree
            raise FileExistsError(path)
        path.write_bytes(raw)
        ref = ArtifactRef(
            kind=kind, version=version, path=str(path), sha256=hashlib.sha256(raw).hexdigest(),
            scope=scope, created_at=datetime.now(timezone.utc), created_by=created_by,
        )
        self.store.add_artifact(job_id, ref)
        return ref

    def write_file(
        self,
        job_id: str,
        kind: ArtifactKind,
        data: bytes,
        suffix: str,
        scope: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> ArtifactRef:
        """A binary artifact (the rendered ``.docx``): ``{kind}[_{scope}]_v{n}{suffix}``, same rules as ``write``."""
        version = self.store.next_artifact_version(job_id, kind, scope)
        path = self.job_dir(job_id) / f"{kind.value}{'_' + _safe(scope) if scope else ''}_v{version}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(path)
        path.write_bytes(data)
        ref = ArtifactRef(
            kind=kind, version=version, path=str(path), sha256=hashlib.sha256(data).hexdigest(),
            scope=scope, created_at=datetime.now(timezone.utc), created_by=created_by,
        )
        self.store.add_artifact(job_id, ref)
        return ref

    @staticmethod
    def read_bytes(ref: ArtifactRef) -> bytes:
        raw = Path(ref.path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref.sha256:
            raise ArtifactCorrupted(f"{ref.path} does not match its recorded sha256")
        return raw

    @staticmethod
    def read(ref: ArtifactRef) -> dict:
        raw = Path(ref.path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref.sha256:
            raise ArtifactCorrupted(f"{ref.path} does not match its recorded sha256")
        return json.loads(raw.decode("utf-8"))

    def read_model(self, ref: ArtifactRef, model: Type[M]) -> M:
        return model.model_validate(self.read(ref))

    def latest(self, job: MigrationJob, kind: ArtifactKind, model: Type[M], scope: Optional[str] = None) -> Optional[M]:
        ref = job.latest_artifact(kind, scope)
        return self.read_model(ref, model) if ref else None
