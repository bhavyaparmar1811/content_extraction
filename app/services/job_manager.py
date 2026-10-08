"""Background job manager for asynchronous document extraction."""

import asyncio
import uuid
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from app.config.settings import Settings
from app.core.exceptions import AppError
from app.schemas.jobs import BatchJob, DocumentJob, JobStatus
from app.schemas.output import DocumentExtractionOutput, ExtractionStats, AssetManifest, AssetReference
from app.services.parser.parser_factory import ParserFactory
from app.services.extraction import TableExtractor, CrossPageTableStitcher, IconExtractor, CaptionExtractor
from app.services.hierarchy.ast_builder import ASTBuilder
from app.services.chunking.hierarchical import HierarchicalChunker
from app.services.chunking.semantic import SemanticChunker
from app.services.export.migration_exporter import MigrationExporter
from app.services.export.source_unit_exporter import SourceUnitExporter
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.stores.sop_store import SopStore


class JobManager:
    """Manages background processing of document extraction jobs."""

    def __init__(self, settings: Settings, concurrency: int = 2, sop_store: Optional["SopStore"] = None):
        self.settings = settings
        self.concurrency = concurrency
        self.sop_store = sop_store
        self.jobs: dict[str, BatchJob] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.workers: list[asyncio.Task] = []
        
    async def start(self):
        """Start the background worker pool."""
        logger.info(f"Starting JobManager with {self.concurrency} workers.")
        for i in range(self.concurrency):
            task = asyncio.create_task(self._worker(f"worker-{i}"))
            self.workers.append(task)
            
    async def stop(self):
        """Stop the background workers."""
        logger.info("Stopping JobManager workers...")
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()

    def create_batch_job(
        self,
        document_ids: list[str],
        user_id: Optional[str] = None,
        user_email: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> BatchJob:
        """Create a new batch job and queue its documents."""
        job_id = str(uuid.uuid4())
        docs = []
        for did in document_ids:
            doc = DocumentJob(document_id=did, filename=f"{did}")
            docs.append(doc)

        job = BatchJob(
            job_id=job_id,
            documents=docs,
            user_id=user_id,
            user_email=user_email,
            correlation_id=correlation_id,
        )
        self.jobs[job_id] = job

        for doc in docs:
            self.queue.put_nowait((job_id, doc.document_id))

        return job
        
    def get_job(self, job_id: str) -> Optional[BatchJob]:
        """Retrieve a job by ID."""
        return self.jobs.get(job_id)

    async def _worker(self, name: str):
        """Background worker loop to process documents."""
        while True:
            try:
                job_id, doc_id = await self.queue.get()
                await self._process_document(job_id, doc_id)
                self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Worker {} encountered error", name)

    async def _process_document(self, job_id: str, document_id: str):
        """Process a single document through the extraction pipeline."""
        job = self.jobs.get(job_id)
        if not job:
            return
            
        doc_job = next((d for d in job.documents if d.document_id == document_id), None)
        if not doc_job:
            return
            
        doc_job.status = JobStatus.PROCESSING
        doc_job.started_at = datetime.utcnow()
        doc_job.message = "Started processing"
        if job.status == JobStatus.QUEUED:
            job.status = JobStatus.PROCESSING
            job.started_at = datetime.utcnow()
            
        try:
            with logger.contextualize(
                job_id=job_id,
                document_id=document_id,
                user_id=job.user_id,
                user_email=job.user_email,
                correlation_id=job.correlation_id,
                stage="parse",
            ):
                # 1. Locate file
                upload_path = None
                for ext in self.settings.allowed_extensions:
                    candidate = self.settings.upload_dir / f"{document_id}{ext}"
                    if candidate.exists():
                        upload_path = candidate
                        break

                if not upload_path:
                    raise FileNotFoundError(f"File for document {document_id} not found.")

                doc_job.filename = upload_path.name
                doc_job.progress_percentage = 20

                # 2. Parse
                doc_job.message = "Parsing document..."
                factory = ParserFactory(settings=self.settings)
                parser = factory.get_parser(str(upload_path))
                raw_document = await asyncio.to_thread(parser.parse, str(upload_path), document_id=document_id)
                doc_job.progress_percentage = 40

                # 3. Extraction
                doc_job.message = "Running extraction pipeline..."
                table_ext = TableExtractor(settings=self.settings)
                stitch_ext = CrossPageTableStitcher(settings=self.settings)
                icon_ext = IconExtractor(settings=self.settings)
                caption_ext = CaptionExtractor(settings=self.settings)

                with logger.contextualize(stage="tables"):
                    raw_document = await asyncio.to_thread(table_ext.extract, raw_document)
                    raw_document = await asyncio.to_thread(stitch_ext.extract, raw_document)

                with logger.contextualize(stage="icons"):
                    raw_document = await asyncio.to_thread(icon_ext.extract, raw_document, document_id=document_id)
                    raw_document = await asyncio.to_thread(caption_ext.extract, raw_document)
                doc_job.progress_percentage = 65

                # 4. AST Build
                doc_job.message = "Building Abstract Syntax Tree..."
                ast_builder = ASTBuilder(settings=self.settings)
                with logger.contextualize(stage="ast"):
                    ast = await asyncio.to_thread(ast_builder.build, raw_document)
                doc_job.progress_percentage = 80

                # 5. Chunking
                doc_job.message = "Generating hierarchical and semantic chunks..."
                hierarchical_chunker = HierarchicalChunker(settings=self.settings)
                semantic_chunker = SemanticChunker(settings=self.settings)
                with logger.contextualize(stage="chunk"):
                    chunks = await asyncio.to_thread(hierarchical_chunker.chunk, ast)
                    chunks = await asyncio.to_thread(semantic_chunker.chunk, chunks)
                doc_job.progress_percentage = 90

                # 6. Output packaging
                doc_job.message = "Finalizing output..."
                with logger.contextualize(stage="export"):
                    assets_manifest = self._build_asset_manifest(document_id, ast)
                    migration_output = MigrationExporter.export(
                        document_id=document_id,
                        ast=ast,
                        assets_manifest=assets_manifest,
                    )

                    output_dir = self.settings.output_dir
                    output_dir.mkdir(parents=True, exist_ok=True)
                    out_file = output_dir / f"{document_id}_v2.json"

                    clean_json = json.dumps(migration_output.to_clean_dict(), indent=2, ensure_ascii=False)
                    await asyncio.to_thread(out_file.write_text, clean_json, encoding="utf-8")

                    # v2 source units, versioned so a re-upload never overwrites them.
                    units_file = None
                    try:
                        source_doc = SourceUnitExporter.export(
                            document_id=document_id,
                            ast=ast,
                            source_file=upload_path.name,
                            file_type=upload_path.suffix.lstrip(".").lower() or None,
                        )
                        version = getattr(raw_document.metadata, "gpdat_version", None) or 1
                        units_file = output_dir / f"{document_id}_v{version}_units.json"
                        units_json = json.dumps(source_doc.to_clean_dict(), indent=2, ensure_ascii=False)
                        await asyncio.to_thread(units_file.write_text, units_json, encoding="utf-8")
                    except Exception as units_err:  # v2 output must never fail the v3.1 job
                        logger.warning(f"Source-unit export failed for {document_id}: {units_err}")
                        units_file = None

                    if self.sop_store:
                        try:
                            meta = migration_output.metadata
                            await asyncio.to_thread(
                                self.sop_store.upsert_record,
                                job_id=job_id,
                                document_uid=document_id,
                                document_number=getattr(meta, "document_number", None),
                                document_name=getattr(meta, "document_name", None),
                                document_title=getattr(meta, "document_title", None),
                                document_version=getattr(meta, "document_version", None),
                                document_type=getattr(meta, "document_type", None),
                                file_type=getattr(meta, "file_type", "pdf"),
                                language=getattr(meta, "language", "en") or "en",
                                page_count=getattr(meta, "page_count", 0) or 0,
                                source_filename=upload_path.name if upload_path else None,
                                output_path=str(out_file),
                                units_path=str(units_file) if units_file else None,
                                status="in_review",
                            )
                        except Exception as store_err:
                            logger.warning(f"Failed to upsert SOP record for {document_id}: {store_err}")

            doc_job.status = JobStatus.COMPLETED
            doc_job.progress_percentage = 100
            doc_job.message = "Successfully extracted document."
            doc_job.completed_at = datetime.utcnow()

        except Exception as e:
            logger.exception(f"Job {job_id} failed on doc {document_id}")
            safe = e.message if isinstance(e, AppError) else "Extraction failed"
            doc_job.status = JobStatus.FAILED
            doc_job.error = safe
            doc_job.message = f"Failed: {safe}"
            doc_job.completed_at = datetime.utcnow()
            
        # Check if entire batch is complete
        all_done = all(d.status in (JobStatus.COMPLETED, JobStatus.FAILED) for d in job.documents)
        if all_done:
            job.status = JobStatus.COMPLETED if job.failed_documents == 0 else JobStatus.FAILED
            job.completed_at = datetime.utcnow()

    def _build_asset_manifest(self, document_id: str, ast) -> AssetManifest:
        """Build an asset manifest for images and icons in the document's asset directories."""
        node_meta = {}
        def _traverse_nodes(node):
            if getattr(node, "node_type", None) == "image":
                fname = Path(getattr(node, "asset_path", "")).name
                if fname:
                    node_meta[fname] = {
                        "width": getattr(node, "width", 0) or 0,
                        "height": getattr(node, "height", 0) or 0,
                    }
            elif getattr(node, "node_type", None) == "icon":
                fname = Path(getattr(node, "asset_path", "")).name
                if fname:
                    node_meta[fname] = {
                        "width": getattr(node, "width", 0) or 0,
                        "height": getattr(node, "height", 0) or 0,
                        "semantic_meaning": getattr(node, "semantic_meaning", None),
                    }
            for child in getattr(node, "children", []):
                _traverse_nodes(child)

        _traverse_nodes(ast)

        images = []
        icons = []
        img_dir = self.settings.get_document_image_dir(document_id)
        icon_dir = self.settings.get_document_icon_dir(document_id)

        if img_dir.exists():
            for f in img_dir.iterdir():
                if f.is_file():
                    meta = node_meta.get(f.name, {})
                    images.append(AssetReference(
                        asset_id=f.stem,
                        filename=f.name,
                        size_bytes=f.stat().st_size,
                        width=meta.get("width", 0),
                        height=meta.get("height", 0),
                    ))
        if icon_dir.exists():
            for f in icon_dir.iterdir():
                if f.is_file():
                    meta = node_meta.get(f.name, {})
                    icons.append(AssetReference(
                        asset_id=f.stem,
                        filename=f.name,
                        size_bytes=f.stat().st_size,
                        width=meta.get("width", 0),
                        height=meta.get("height", 0),
                        semantic_meaning=meta.get("semantic_meaning"),
                    ))

        return AssetManifest(
            base_path=str(self.settings.project_root / "data"),
            images=images,
            icons=icons,
        )
