# SOP Migration System — Project Configuration
"""Application-wide configuration loaded from environment or defaults."""

from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from pydantic import Field, AliasChoices, model_validator
from pydantic_settings import BaseSettings

# Automatically load .env into os.environ on startup
load_dotenv()


class Settings(BaseSettings):
    """Central configuration — injected into all services via constructor."""

    # --- Paths ---
    project_root: Path = Path(__file__).resolve().parent.parent.parent
    upload_dir: Path = Path("data/uploads")
    extracted_images_dir: Path = Path("data/extracted_images")
    extracted_icons_dir: Path = Path("data/extracted_icons")
    temp_dir: Path = Path("data/temp")
    output_dir: Path = Path("data/output")

    # --- Processing ---
    max_upload_size_mb: int = 50
    allowed_extensions: list[str] = [".pdf", ".docx"]
    ocr_enabled: bool = False
    ocr_language: str = "eng"

    # --- Semantic Chunking ---
    embedding_model: str = "all-MiniLM-L6-v2"
    similarity_threshold: float = 0.4

    # --- Watermark Filtering ---
    watermark_keywords: list[str] = [
        "WORKING COPY",
        "DRAFT",
        "CONFIDENTIAL",
        "DO NOT DISTRIBUTE",
        "WATERMARK"
    ]

    # --- Table Stitching ---
    table_stitch_enabled: bool = True
    table_stitch_score_threshold: float = 0.7
    table_stitch_column_tolerance_pt: float = 15.0
    table_stitch_bottom_zone_pct: float = 0.75
    table_stitch_top_zone_pct: float = 0.20

    # --- Logging ---
    log_level: str = "INFO"
    log_dir: Path = Path("data/logs")
    log_file_path: Path = Path("data/logs/app.log")
    error_file_path: Path = Path("data/logs/error.log")
    log_db_path: Path = Path("data/logs/logs.db")

    # --- Migration ---
    migration_output_dir: Path = Path("data/migrated")
    migration_template_dir: Path = Path("data/templates")
    skip_preamble_migration: bool = True       # Don't touch cover page / Section 0

    # --- Template Management ---
    template_upload_dir: Path = Path("data/template_uploads")
    template_output_dir: Path = Path("data/template_output")
    template_extracted_images_dir: Path = Path("data/template_images")
    template_extracted_icons_dir: Path = Path("data/template_icons")
    template_config_dir: Path = Path("data/template_config")  # human slot/region overrides (v2)
    template_allowed_extensions: list[str] = [".docx"]

    # --- GWP (Good Writing Practice) guides, migration v2 ---
    gwp_dir: Path = Path("data/gwp")                  # units, rules and report JSON per guide version
    gwp_upload_dir: Path = Path("data/gwp/uploads")
    gwp_allowed_extensions: list[str] = [".docx", ".pdf"]

    # --- Migration v2 jobs ---
    migration_v2_dir: Path = Path("data/migrations")  # data/migrations/{job_id}/{kind}_v{n}.json
    section_planner_llm: str = "confirm"   # "confirm": one compact LLM call checks the rule plan; "off": rules only
    section_planner_preview_chars: int = 100  # unit preview length sent for split candidates / unsure sections
    slot_planner_llm: str = "confirm"      # "confirm": LLM checks doubtful slot placements and proposes callouts; "off": rules only
    slot_planner_preview_chars: int = 160  # passage preview length in the slot planner prompt
    slot_planner_block_tokens: int = 5000  # passage tokens per slot planner call; larger sections are split
    drafter_llm: str = "on"                # "on": rewrite (GWP) and split shared passages via the LLM; "off": copy every passage
    drafter_block_tokens: int = 1500       # source passage tokens per rewrite call (larger blocks made gpt-4o skip passages)
    drafter_max_rules: int = 40            # STY + PRES rules sent per rewrite call (deduplicated)
    drafter_memory_tokens: int = 600       # cap on the bounded memory (role names, abbreviations, reference targets)
    critic_llm: str = "on"                 # "on": the semantic critic reads every reworded claim; "off": deterministic checks only
    critic_block_tokens: int = 3000        # claim + source tokens per critic call; larger sections are split
    repair_max_attempts: int = 2           # repair rounds per section before it needs human review
    reconcile_llm: str = "gwp"             # "gwp": cross-section LLM check when the job rewrites (a GWP); "on": always; "off"
    reconcile_max_chars: int = 60_000      # the document's claims sent in the one reconciliation call; larger: skipped
    render_toc_pages: str = "word"         # "word": TOC page numbers measured by Microsoft Word when installed; "off": Word fills them on opening

    # --- LLM (LangChain) ---
    use_llm_section_summarizer: bool = False  # false = Mode A (programmatic), true = Mode B (LLM semantic)
    llm_planner_model: str = "gemini/gemini-2.5-flash"
    llm_summarizer_model: str = "gemini/gemini-2.5-flash"
    llm_temperature: float = 0.1
    llm_planner_max_tokens: int = 16384
    llm_summarizer_max_tokens: int = 4096

    # --- Azure OpenAI (optional — overrides llm_planner_model / llm_summarizer_model if set) ---
    # Each value also accepts the standard Azure variable name (AZURE_OPENAI_ENDPOINT,
    # AZURE_OPENAI_API_VERSION, AZURE_OPENAI_DEPLOYMENT_NAME); the SOP_ name wins when both are set.
    # Left unset, use_azure_openai turns on when an endpoint and a key are configured.
    use_azure_openai: Optional[bool] = None
    azure_openai_endpoint: Optional[str] = Field(
        default=None,  # e.g. "https://<your-resource>.openai.azure.com/"
        validation_alias=AliasChoices("SOP_AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_ENDPOINT", "azure_openai_endpoint"),
    )
    azure_openai_api_version: str = Field(
        default="2024-12-01-preview",
        validation_alias=AliasChoices(
            "SOP_AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_API_VERSION", "azure_openai_api_version"
        ),
    )
    azure_openai_planner_deployment: str = Field(
        default="gpt-4o",  # Deployment name for planner (Phase 2)
        validation_alias=AliasChoices(
            "SOP_AZURE_OPENAI_PLANNER_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT_NAME", "azure_openai_planner_deployment"
        ),
    )
    azure_openai_summarizer_deployment: str = Field(
        default="gpt-4o-mini",  # Deployment name for summarizer (Mode B)
        validation_alias=AliasChoices(
            "SOP_AZURE_OPENAI_SUMMARIZER_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT_NAME", "azure_openai_summarizer_deployment"
        ),
    )
    azure_openai_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("AZURE_OPENAI_API_KEY", "SOP_AZURE_OPENAI_API_KEY", "azure_openai_api_key")
    )

    # --- LLM Rate Limiting ---
    llm_max_concurrent: int = 3
    llm_min_delay_seconds: float = 0.5
    llm_max_retries: int = 5
    llm_base_backoff_seconds: float = 2.0

    # --- Authentication & JWT ---
    jwt_secret_key: str = "sop-governance-auth-secret-key-development-2026"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_seconds: int = 900
    jwt_issuer: str = "governance-sop-api"
    jwt_audience: str = "governance-sop-web"
    auth_db_path: Optional[Path] = None
    auth_refresh_token_expire_days: int = 1
    auth_remember_me_expire_days: int = 30
    auth_max_failed_attempts: int = 5
    auth_lockout_duration_minutes: int = 15
    auth_ip_rate_limit_max_attempts: int = 10
    auth_ip_rate_limit_window_minutes: int = 15
    auth_cookie_secure: bool = False

    model_config = {
        "env_prefix": "SOP_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @model_validator(mode="after")
    def _default_azure_switch(self) -> "Settings":
        if self.use_azure_openai is None:
            self.use_azure_openai = bool(self.azure_openai_endpoint and self.azure_openai_api_key)
        return self

    def resolve_paths(self, base: Path) -> None:
        """Resolve all relative paths against a base directory."""
        self.upload_dir = base / self.upload_dir
        self.extracted_images_dir = base / self.extracted_images_dir
        self.extracted_icons_dir = base / self.extracted_icons_dir
        self.temp_dir = base / self.temp_dir
        self.output_dir = base / self.output_dir
        self.migration_output_dir = base / self.migration_output_dir
        self.migration_template_dir = base / self.migration_template_dir
        self.template_upload_dir = base / self.template_upload_dir
        self.template_output_dir = base / self.template_output_dir
        self.template_extracted_images_dir = base / self.template_extracted_images_dir
        self.template_extracted_icons_dir = base / self.template_extracted_icons_dir
        self.template_config_dir = base / self.template_config_dir
        self.gwp_dir = base / self.gwp_dir
        self.gwp_upload_dir = base / self.gwp_upload_dir
        self.migration_v2_dir = base / self.migration_v2_dir
        self.log_dir = base / self.log_dir
        self.log_file_path = base / self.log_file_path
        self.error_file_path = base / self.error_file_path
        self.log_db_path = base / self.log_db_path

    def ensure_directories(self) -> None:
        """Create all required data directories if they don't exist."""
        for dir_path in [
            self.upload_dir,
            self.extracted_images_dir,
            self.extracted_icons_dir,
            self.temp_dir,
            self.output_dir,
            self.migration_output_dir,
            self.migration_template_dir,
            self.template_upload_dir,
            self.template_output_dir,
            self.template_extracted_images_dir,
            self.template_extracted_icons_dir,
            self.gwp_dir,
            self.gwp_upload_dir,
            self.migration_v2_dir,
            self.log_dir,
        ]:
            dir_path.mkdir(parents=True, exist_ok=True)

    def get_document_image_dir(self, document_id: str) -> Path:
        """Return (and create) the image directory for a specific document."""
        p = self.extracted_images_dir / str(document_id)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_document_icon_dir(self, document_id: str) -> Path:
        """Return (and create) the icon directory for a specific document."""
        p = self.extracted_icons_dir / str(document_id)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_template_image_dir(self, template_id: str) -> Path:
        """Return (and create) the image directory for a specific template."""
        p = self.template_extracted_images_dir / str(template_id)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_template_icon_dir(self, template_id: str) -> Path:
        """Return (and create) the icon directory for a specific template."""
        p = self.template_extracted_icons_dir / str(template_id)
        p.mkdir(parents=True, exist_ok=True)
        return p


def get_settings() -> Settings:
    """Factory function for dependency injection in FastAPI."""
    settings = Settings()
    settings.resolve_paths(settings.project_root)
    settings.ensure_directories()
    return settings
