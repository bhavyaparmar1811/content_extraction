"""GWP guide records: one row per uploaded guide version, with its status and artifact paths.

Same SQLite pattern as ``TemplateStore``. The rules themselves live in the
JSON file at ``rules_path``, which stays the source of truth after review.
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.config.settings import Settings

GUIDE_STATUSES = ("uploaded", "parsed", "extracting", "in_review", "approved", "failed")

# Columns update_fields may change.
_UPDATABLE = frozenset({
    "guide_name", "units_path", "rules_path", "report_path", "status", "total_units",
    "total_rules", "approved_rules", "candidate_rules", "prompt_version", "model", "error",
})


class GwpStore:
    def __init__(self, db_path: Path, settings: Settings):
        self.db_path = db_path
        self.settings = settings
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        statuses = ",".join(f"'{s}'" for s in GUIDE_STATUSES)
        with self._get_connection() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS gwp_guides (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    guide_id         TEXT    NOT NULL,
                    guide_name       TEXT    NOT NULL,
                    version          INTEGER NOT NULL DEFAULT 1,
                    file_type        TEXT,
                    file_size_bytes  INTEGER DEFAULT 0,
                    source_filename  TEXT,
                    upload_path      TEXT,
                    units_path       TEXT,
                    rules_path       TEXT,
                    report_path      TEXT,
                    total_units      INTEGER DEFAULT 0,
                    total_rules      INTEGER DEFAULT 0,
                    approved_rules   INTEGER DEFAULT 0,
                    candidate_rules  INTEGER DEFAULT 0,
                    prompt_version   TEXT,
                    model            TEXT,
                    error            TEXT,
                    status           TEXT    NOT NULL DEFAULT 'uploaded'
                                     CHECK(status IN ({statuses})),
                    created_at       TEXT    NOT NULL,
                    updated_at       TEXT    NOT NULL,
                    UNIQUE(guide_id, version)
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_gwp_guide ON gwp_guides(guide_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_gwp_status ON gwp_guides(status);")
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().isoformat() + "Z"

    def get_next_version(self, guide_id: str) -> int:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS max_ver FROM gwp_guides WHERE guide_id = ?", (guide_id,)
            ).fetchone()
            return (row["max_ver"] or 0) + 1

    def create_record(
        self,
        guide_id: str,
        guide_name: str,
        file_type: str,
        file_size_bytes: int = 0,
        source_filename: Optional[str] = None,
        upload_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """Insert the next version of a guide."""
        now = self._now()
        version = self.get_next_version(guide_id)
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO gwp_guides (
                    guide_id, guide_name, version, file_type, file_size_bytes,
                    source_filename, upload_path, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'uploaded', ?, ?)
                """,
                (guide_id, guide_name, version, file_type, file_size_bytes, source_filename, upload_path, now, now),
            )
            record_id = cursor.lastrowid
            conn.commit()
        return self.get_record_by_id(record_id)

    def get_record_by_id(self, record_id: int) -> Optional[dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM gwp_guides WHERE id = ?", (record_id,)).fetchone()
            return dict(row) if row else None

    def get_latest(self, guide_id: str) -> Optional[dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM gwp_guides WHERE guide_id = ? ORDER BY version DESC LIMIT 1", (guide_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_records(self, guide_id: Optional[str] = None, status: Optional[str] = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM gwp_guides WHERE 1=1"
        params: list[Any] = []
        if guide_id:
            query += " AND guide_id = ?"
            params.append(guide_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY id DESC"
        with self._get_connection() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def get_active(self) -> Optional[dict[str, Any]]:
        """The most recently updated approved guide version (a migration still has to name it)."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM gwp_guides WHERE status = 'approved' ORDER BY updated_at DESC, id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def update_fields(self, record_id: int, **fields: Any) -> Optional[dict[str, Any]]:
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"cannot update gwp_guides columns: {sorted(unknown)}")
        if "status" in fields and fields["status"] not in GUIDE_STATUSES:
            raise ValueError(f"invalid guide status {fields['status']!r}")
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            with self._get_connection() as conn:
                conn.execute(
                    f"UPDATE gwp_guides SET {assignments}, updated_at = ? WHERE id = ?",
                    (*fields.values(), self._now(), record_id),
                )
                conn.commit()
        return self.get_record_by_id(record_id)

    def delete_record(self, record_id: int) -> bool:
        """Delete one guide version and its upload, units, rules and report files."""
        record = self.get_record_by_id(record_id)
        if not record:
            return False
        with self._get_connection() as conn:
            conn.execute("DELETE FROM gwp_guides WHERE id = ?", (record_id,))
            conn.commit()
        for key in ("upload_path", "units_path", "rules_path", "report_path"):
            if record.get(key):
                Path(record[key]).unlink(missing_ok=True)
        return True
