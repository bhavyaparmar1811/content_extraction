"""SOP Migration System — FastAPI Application Entrypoint.

Run with:  uvicorn app.main:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.config.settings import get_settings
from app.core.exception_handlers import register_exception_handlers
from app.core.logging_config import setup_logging
from app.core.logging_middleware import LoggingContextMiddleware
from app.api import health, upload, extract, documents, jobs, migration, sops, review, auth, templates, gwp, migrations_v2
from app.services.job_manager import JobManager
from app.stores.sop_store import SopStore
from app.stores.auth_store import AuthStore
from app.stores.template_store import TemplateStore
from app.stores.gwp_store import GwpStore


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup / shutdown lifecycle hook."""
    settings = get_settings()
    setup_logging(settings)
    logger.info(
        "SOP Migration System starting  |  log_level={level}  |  upload_dir={dir}",
        level=settings.log_level,
        dir=settings.upload_dir,
    )
    settings.ensure_directories()

    sop_db_path = settings.project_root / "data" / "sop_records.db"
    sop_store = SopStore(sop_db_path, settings)
    application.state.sop_store = sop_store

    template_db_path = settings.project_root / "data" / "template_records.db"
    template_store = TemplateStore(template_db_path, settings)
    application.state.template_store = template_store

    application.state.gwp_store = GwpStore(settings.project_root / "data" / "gwp_guides.db", settings)

    auth_db_path = settings.auth_db_path or (settings.project_root / "data" / "auth.db")
    auth_store = AuthStore(auth_db_path, settings)
    application.state.auth_store = auth_store

    job_manager = JobManager(settings, concurrency=1, sop_store=sop_store)
    application.state.job_manager = job_manager
    await job_manager.start()

    from app.services.llm.chain_factory import ChainFactory
    chain_factory = ChainFactory(settings)
    application.state.chain_factory = chain_factory

    # Migration v2: persisted jobs, resumed after a restart.
    from app.services.llm.rate_limiter import LLMRateLimiter
    from app.services.migration_v2.artifacts import ArtifactWriter
    from app.services.migration_v2.inputs import InputResolver
    from app.services.migration_v2.orchestrator import Orchestrator
    from app.stores.migration_store import MigrationStore

    migration_store = MigrationStore(settings.project_root / "data" / "migrations.db", settings)
    orchestrator = Orchestrator(
        store=migration_store,
        artifacts=ArtifactWriter(settings.migration_v2_dir, migration_store),
        inputs=InputResolver(sop_store, template_store, application.state.gwp_store),
        settings=settings,
        rate_limiter=LLMRateLimiter(),
        chain_factory=chain_factory,
    )
    application.state.migration_store = migration_store
    application.state.migration_orchestrator = orchestrator
    await orchestrator.start()

    yield

    logger.info("SOP Migration System shutting down.")
    await application.state.migration_orchestrator.stop()
    await job_manager.stop()
    from app.core.logging_config import reset_logging_state
    reset_logging_state()


app = FastAPI(
    title="SOP Migration System",
    description=(
        "Extract document structure, text, tables, images, icons, and metadata "
        "from PDF / DOCX SOPs. Performs hybrid chunking and outputs structured "
        "JSON for migration into new templates."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-Correlation-ID", "Idempotency-Key", "Authorization"],
)
app.add_middleware(LoggingContextMiddleware)
register_exception_handlers(app)

app.include_router(health.router)
app.include_router(upload.router)
app.include_router(extract.router)
app.include_router(documents.router)
app.include_router(jobs.router)
app.include_router(migration.router)
app.include_router(sops.router)
app.include_router(review.router)
app.include_router(auth.router)
app.include_router(templates.router)
app.include_router(gwp.router)
app.include_router(migrations_v2.router)
