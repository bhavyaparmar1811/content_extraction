"""SOP records SQLite data store for persistence, versioning, and smart cleanup."""

import sqlite3
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Any
from loguru import logger

from app.config.settings import Settings


class SopStore:
    """Manages persistence of SOP metadata, version history, and review states."""

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
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sop_records (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id           TEXT    NOT NULL,
                    document_uid     TEXT    NOT NULL,
                    document_number  TEXT,
                    document_name    TEXT,
                    document_title   TEXT,
                    document_version TEXT,
                    document_type    TEXT,
                    file_type        TEXT,
                    language         TEXT    DEFAULT 'en',
                    page_count       INTEGER DEFAULT 0,
                    gpdat_version    INTEGER DEFAULT 0,
                    status           TEXT    NOT NULL DEFAULT 'in_review'
                                     CHECK(status IN ('in_review','approved','rejected')),
                    source_filename  TEXT,
                    output_path      TEXT,
                    created_at       TEXT    NOT NULL,
                    updated_at       TEXT    NOT NULL,
                    UNIQUE(document_uid, gpdat_version)
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sop_uid ON sop_records(document_uid);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sop_status ON sop_records(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sop_job ON sop_records(job_id);")
            # Additive migrations for databases created before a column existed.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sop_records)")}
            if "units_path" not in columns:
                conn.execute("ALTER TABLE sop_records ADD COLUMN units_path TEXT")  # v2 SourceDocument JSON
            for column in ("migrated_path", "migrated_job_id", "migrated_at"):  # v2 final export (Phase 12)
                if column not in columns:
                    conn.execute(f"ALTER TABLE sop_records ADD COLUMN {column} TEXT")
            conn.commit()

    def get_next_version(self, document_uid: str) -> int:
        """Return the next gpdat_version for a document_uid."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT MAX(gpdat_version) AS max_ver FROM sop_records WHERE document_uid = ?",
                (document_uid,),
            )
            row = cursor.fetchone()
            if row and row["max_ver"] is not None:
                return row["max_ver"] + 1
            return 1

    def upsert_record(
        self,
        job_id: str,
        document_uid: str,
        document_number: Optional[str] = None,
        document_name: Optional[str] = None,
        document_title: Optional[str] = None,
        document_version: Optional[str] = None,
        document_type: Optional[str] = None,
        file_type: Optional[str] = None,
        language: str = "en",
        page_count: int = 0,
        source_filename: Optional[str] = None,
        output_path: Optional[str] = None,
        status: Optional[str] = None,
        units_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """Insert a new version or update an existing record."""
        now = datetime.utcnow().isoformat() + "Z"
        next_ver = self.get_next_version(document_uid)
        record_status = status or "in_review"

        with self._get_connection() as conn:
            # Check if there is an existing record with the same uid to update metadata
            cursor = conn.execute(
                """
                INSERT INTO sop_records (
                    job_id, document_uid, document_number, document_name,
                    document_title, document_version, document_type, file_type,
                    language, page_count, gpdat_version, status,
                    source_filename, output_path, units_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    document_uid,
                    document_number,
                    document_name,
                    document_title,
                    document_version,
                    document_type,
                    file_type,
                    language,
                    page_count,
                    next_ver,
                    record_status,
                    source_filename,
                    output_path,
                    units_path,
                    now,
                    now,
                ),
            )
            record_id = cursor.lastrowid
            conn.commit()
            return self.get_record_by_id(record_id)

    def get_records(
        self,
        status: Optional[str] = None,
        document_uid: Optional[str] = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List SOP records ordered by newest first."""
        query = "SELECT * FROM sop_records WHERE 1=1"
        params: list[Any] = []

        if status:
            query += " AND status = ?"
            params.append(status)
        if document_uid:
            query += " AND document_uid = ?"
            params.append(document_uid)

        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._get_connection() as conn:
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]

    def get_record_by_id(self, record_id: int) -> Optional[dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM sop_records WHERE id = ?", (record_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_dict(row)
            return None

    def get_record_by_uid(self, document_uid: str) -> Optional[dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM sop_records WHERE document_uid = ? ORDER BY gpdat_version DESC LIMIT 1",
                (document_uid,),
            )
            row = cursor.fetchone()
            if row:
                return self._row_to_dict(row)
            return None

    def update_status(self, record_id: int, new_status: str) -> Optional[dict[str, Any]]:
        now = datetime.utcnow().isoformat() + "Z"
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE sop_records SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, now, record_id),
            )
            conn.commit()
            return self.get_record_by_id(record_id)

    def mark_migrated(self, record_id: int, job_id: str, path: str) -> Optional[dict[str, Any]]:
        """This SOP version was exported by a v2 migration job: the final Word file it produced."""
        now = datetime.utcnow().isoformat() + "Z"
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE sop_records SET migrated_path = ?, migrated_job_id = ?, migrated_at = ?, updated_at = ? WHERE id = ?",
                (path, job_id, now, now, record_id),
            )
            conn.commit()
            return self.get_record_by_id(record_id)

    def delete_record(self, record_id: int) -> bool:
        """Delete an SOP record. If no sibling versions remain, perform smart cleanup."""
        record = self.get_record_by_id(record_id)
        if not record:
            return False

        doc_uid = record["document_Uid"]

        with self._get_connection() as conn:
            conn.execute("DELETE FROM sop_records WHERE id = ?", (record_id,))
            conn.commit()

            # Check remaining siblings
            cursor = conn.execute("SELECT COUNT(*) AS cnt FROM sop_records WHERE document_uid = ?", (doc_uid,))
            remaining = cursor.fetchone()["cnt"]

        if remaining == 0:
            self._smart_cleanup(doc_uid)

        return True

    def _smart_cleanup(self, document_id: str):
        """Best-effort file cleanup for deleted document with no sibling versions."""
        logger.info(f"Performing smart cleanup for document {document_id}")
        try:
            # Delete upload files
            for ext in self.settings.allowed_extensions:
                upload_file = self.settings.upload_dir / f"{document_id}{ext}"
                if upload_file.exists():
                    upload_file.unlink(missing_ok=True)

            # Delete v2 json
            v2_json = self.settings.output_dir / f"{document_id}_v2.json"
            if v2_json.exists():
                v2_json.unlink(missing_ok=True)

            # Delete legacy json
            legacy_json = self.settings.output_dir / f"{document_id}.json"
            if legacy_json.exists():
                legacy_json.unlink(missing_ok=True)

            # Delete extracted images
            img_dir = self.settings.extracted_images_dir / document_id
            if img_dir.exists() and img_dir.is_dir():
                shutil.rmtree(img_dir, ignore_errors=True)

            # Delete extracted icons
            icon_dir = self.settings.extracted_icons_dir / document_id
            if icon_dir.exists() and icon_dir.is_dir():
                shutil.rmtree(icon_dir, ignore_errors=True)

        except Exception as e:
            logger.warning(f"Error during smart cleanup of {document_id}: {e}")

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """Format row with casing compatible with both spec and camelCase/snake_case."""
        d = dict(row)
        # normalize document_uid to document_Uid for frontend compatibility
        d["document_Uid"] = d.pop("document_uid")
        return d
