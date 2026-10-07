"""Template records SQLite data store for persistence, versioning, and smart cleanup."""

import sqlite3
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Any
from loguru import logger

from app.config.settings import Settings


class TemplateStore:
    """Manages persistence of Template metadata, version history, extraction state, and global rules."""

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
                CREATE TABLE IF NOT EXISTS template_records (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_uid       TEXT    NOT NULL,
                    template_name      TEXT    NOT NULL,
                    template_version   INTEGER DEFAULT 1,
                    file_type          TEXT    DEFAULT 'docx',
                    file_size_bytes    INTEGER DEFAULT 0,
                    source_filename    TEXT,
                    upload_path        TEXT,
                    output_path        TEXT,
                    total_sections     INTEGER DEFAULT 0,
                    total_elements     INTEGER DEFAULT 0,
                    total_instructions INTEGER DEFAULT 0,
                    total_icons        INTEGER DEFAULT 0,
                    total_callouts     INTEGER DEFAULT 0,
                    global_rules_json  TEXT,
                    status             TEXT    NOT NULL DEFAULT 'uploaded'
                                       CHECK(status IN ('uploaded','extracting','ready','failed')),
                    created_at         TEXT    NOT NULL,
                    updated_at         TEXT    NOT NULL,
                    UNIQUE(template_uid, template_version)
                );
                """
            )
            existing_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(template_records);")
            }
            if "total_callouts" not in existing_columns:
                conn.execute(
                    "ALTER TABLE template_records ADD COLUMN total_callouts INTEGER DEFAULT 0;"
                )
            # Migration v2 template model (Phase 3)
            for column in ("model_path", "normalized_path", "readiness_status"):
                if column not in existing_columns:
                    conn.execute(f"ALTER TABLE template_records ADD COLUMN {column} TEXT;")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tpl_uid ON template_records(template_uid);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tpl_status ON template_records(status);")
            conn.commit()

    def get_next_version(self, template_uid: str) -> int:
        """Return the next template_version for a template_uid."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT MAX(template_version) AS max_ver FROM template_records WHERE template_uid = ?",
                (template_uid,),
            )
            row = cursor.fetchone()
            if row and row["max_ver"] is not None:
                return row["max_ver"] + 1
            return 1

    def upsert_record(
        self,
        template_uid: str,
        template_name: str,
        file_type: str = "docx",
        file_size_bytes: int = 0,
        source_filename: Optional[str] = None,
        upload_path: Optional[str] = None,
        output_path: Optional[str] = None,
        status: str = "uploaded",
    ) -> dict[str, Any]:
        """Insert a new version record for a template."""
        now = datetime.utcnow().isoformat() + "Z"
        next_ver = self.get_next_version(template_uid)

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO template_records (
                    template_uid, template_name, template_version, file_type,
                    file_size_bytes, source_filename, upload_path, output_path,
                    total_sections, total_elements, total_instructions, total_icons,
                    global_rules_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, NULL, ?, ?, ?)
                """,
                (
                    template_uid,
                    template_name,
                    next_ver,
                    file_type,
                    file_size_bytes,
                    source_filename,
                    upload_path,
                    output_path,
                    status,
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
        template_uid: Optional[str] = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List template records ordered by newest first."""
        query = "SELECT * FROM template_records WHERE 1=1"
        params: list[Any] = []

        if status:
            query += " AND status = ?"
            params.append(status)
        if template_uid:
            query += " AND template_uid = ?"
            params.append(template_uid)

        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._get_connection() as conn:
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]

    def get_record_by_id(self, record_id: int) -> Optional[dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM template_records WHERE id = ?", (record_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_dict(row)
            return None

    def get_record_by_uid(self, template_uid: str) -> Optional[dict[str, Any]]:
        """Fetch the latest version of a template by UID."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM template_records WHERE template_uid = ? ORDER BY template_version DESC LIMIT 1",
                (template_uid,),
            )
            row = cursor.fetchone()
            if row:
                return self._row_to_dict(row)
            return None

    def get_all_ready_templates(self) -> list[dict[str, Any]]:
        """All templates with status='ready' — for migration dropdown."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM template_records WHERE status = 'ready' ORDER BY template_name ASC, template_version DESC"
            )
            rows = cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]

    def update_status(self, record_id: int, new_status: str) -> Optional[dict[str, Any]]:
        now = datetime.utcnow().isoformat() + "Z"
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE template_records SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, now, record_id),
            )
            conn.commit()
            return self.get_record_by_id(record_id)

    def update_extraction_results(
        self,
        record_id: int,
        output_path: str,
        total_sections: int,
        total_elements: int,
        total_instructions: int,
        total_icons: int,
        total_callouts: int = 0,
        global_rules_json: Optional[str] = None,
        status: str = "ready",
    ) -> Optional[dict[str, Any]]:
        """Update extraction results post-pipeline."""
        now = datetime.utcnow().isoformat() + "Z"
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE template_records
                SET output_path = ?,
                    total_sections = ?,
                    total_elements = ?,
                    total_instructions = ?,
                    total_icons = ?,
                    total_callouts = ?,
                    global_rules_json = ?,
                    status = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    output_path,
                    total_sections,
                    total_elements,
                    total_instructions,
                    total_icons,
                    total_callouts,
                    global_rules_json,
                    status,
                    now,
                    record_id,
                ),
            )
            conn.commit()
            return self.get_record_by_id(record_id)

    def update_template_model(
        self,
        record_id: int,
        model_path: Optional[str],
        normalized_path: Optional[str],
        readiness_status: Optional[str],
    ) -> Optional[dict[str, Any]]:
        """Record the v2 TemplateModel files and readiness status of a template version."""
        now = datetime.utcnow().isoformat() + "Z"
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE template_records
                SET model_path = ?, normalized_path = ?, readiness_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (model_path, normalized_path, readiness_status, now, record_id),
            )
            conn.commit()
            return self.get_record_by_id(record_id)

    def delete_record(self, record_id: int) -> bool:
        """Delete a template record. If no sibling versions remain, perform smart cleanup."""
        record = self.get_record_by_id(record_id)
        if not record:
            return False

        template_uid = record["template_uid"]
        upload_path = record.get("upload_path")
        output_path = record.get("output_path")
        model_path = record.get("model_path")
        normalized_path = record.get("normalized_path")

        with self._get_connection() as conn:
            conn.execute("DELETE FROM template_records WHERE id = ?", (record_id,))
            conn.commit()

            cursor = conn.execute(
                "SELECT COUNT(*) AS cnt FROM template_records WHERE template_uid = ?",
                (template_uid,),
            )
            remaining = cursor.fetchone()["cnt"]

        # Delete specific version files
        if upload_path:
            Path(upload_path).unlink(missing_ok=True)
        if output_path:
            p = Path(output_path)
            p.unlink(missing_ok=True)
            p.with_name(f"{p.stem}_migration.json").unlink(missing_ok=True)
        if model_path:
            p = Path(model_path)
            p.unlink(missing_ok=True)
            p.with_name(p.name.replace("_model.json", "_readiness.json")).unlink(missing_ok=True)
        if normalized_path:
            Path(normalized_path).unlink(missing_ok=True)

        # If no siblings left, clean image/icon directories and any leftover files
        if remaining == 0:
            self._smart_cleanup(template_uid)

        return True

    def _smart_cleanup(self, template_uid: str):
        """Best-effort file cleanup for deleted template with no remaining versions."""
        logger.info(f"Performing smart cleanup for template {template_uid}")
        try:
            # Delete any remaining files matching prefix
            for upload_file in self.settings.template_upload_dir.glob(f"{template_uid}*"):
                upload_file.unlink(missing_ok=True)

            for output_file in self.settings.template_output_dir.glob(f"{template_uid}*"):
                output_file.unlink(missing_ok=True)

            # Delete extracted images
            img_dir = self.settings.template_extracted_images_dir / template_uid
            if img_dir.exists() and img_dir.is_dir():
                shutil.rmtree(img_dir, ignore_errors=True)

            # Delete extracted icons
            icon_dir = self.settings.template_extracted_icons_dir / template_uid
            if icon_dir.exists() and icon_dir.is_dir():
                shutil.rmtree(icon_dir, ignore_errors=True)

        except Exception as e:
            logger.warning(f"Error during smart cleanup of template {template_uid}: {e}")

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert sqlite3.Row to standard dict."""
        return dict(row)
