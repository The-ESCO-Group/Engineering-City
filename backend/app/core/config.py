"""Application settings.

Everything is environment-driven so the same image runs in demo mode (SQLite,
MockProvider) and in an internal multi-user deployment (PostgreSQL, Ollama).

External model providers are **off unless an administrator explicitly turns them
on**: enabling one requires both adding it to ``ESCO_ENABLED_PROVIDERS`` and
supplying its credentials. There is no implicit fallback to a cloud provider.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

from pydantic import EmailStr, Field, SecretStr, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.enums import ProviderKind

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Runtime configuration, read from the environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="ESCO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- Application ------------------------------------------------------
    app_name: str = "ESCO Engineering City"
    environment: Literal["demo", "development", "production", "test"] = "demo"
    debug: bool = False
    api_prefix: str = "/api"

    #: Origins allowed to call the API from a browser. The local frontend dev
    #: server and the packaged desktop shell only.
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:1420",
            "http://127.0.0.1:1420",
        ]
    )

    #: Refuse to start if the app is reachable from a non-loopback interface
    #: while ``allow_remote_binding`` is false. This product is local-only.
    allow_remote_binding: bool = False

    # -- Database ---------------------------------------------------------
    #: SQLAlchemy URL. Defaults to on-disk SQLite for demonstration mode.
    database_url: str = "sqlite+pysqlite:///./data/esco_city.sqlite3"
    database_echo: bool = False
    #: Guards against a rogue query holding a connection forever.
    database_pool_size: int = 5
    database_max_overflow: int = 10

    # -- Storage ----------------------------------------------------------
    #: Root of the local artifact store. Originals are written read-only.
    data_dir: Path = Path("./data")
    max_upload_bytes: int = 200 * 1024 * 1024  # 200 MiB per file
    max_project_bytes: int = 5 * 1024 * 1024 * 1024  # 5 GiB per project

    # -- Archive safety ---------------------------------------------------
    max_archive_entries: int = 5_000
    max_archive_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB
    #: Reject an archive whose uncompressed:compressed ratio exceeds this
    #: (zip-bomb guard).
    max_archive_compression_ratio: float = 200.0
    max_archive_depth: int = 2

    # -- XML safety -------------------------------------------------------
    max_xml_bytes: int = 512 * 1024 * 1024
    max_xml_depth: int = 100

    # -- Authentication ---------------------------------------------------
    #: HS256 signing key. MUST be overridden outside demo/test; the app refuses
    #: to start in production with the default.
    jwt_secret: SecretStr = SecretStr("dev-only-insecure-secret-change-me")
    jwt_algorithm: Literal["HS256"] = "HS256"
    access_token_ttl_seconds: int = 12 * 60 * 60
    #: Name of the httpOnly cookie carrying the access token.
    session_cookie_name: str = "esco_session"
    session_cookie_secure: bool = False  # local http:// by default

    #: Seed an administrator on first boot so a fresh demo is usable.
    bootstrap_admin_email: str = "admin@esco.internal"
    bootstrap_admin_password: SecretStr = SecretStr("esco-demo-admin")
    bootstrap_demo_users: bool = True

    # -- Model providers --------------------------------------------------
    #: Only providers listed here may be selected by a ModelConfiguration.
    enabled_providers: list[ProviderKind] = Field(default_factory=lambda: [ProviderKind.MOCK])
    default_provider: ProviderKind = ProviderKind.MOCK

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.1:8b"

    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-sonnet-5"

    provider_timeout_seconds: float = 120.0
    provider_max_retries: int = 3

    # -- Agent run limits -------------------------------------------------
    agent_run_timeout_seconds: float = 300.0
    agent_max_actions_per_run: int = 50
    agent_max_iterations_per_run: int = 8
    agent_max_tokens_per_run: int = 200_000
    project_token_budget: int = 2_000_000
    project_cost_budget_usd: float = 25.0

    # -- Workflow / worker ------------------------------------------------
    worker_enabled: bool = True
    worker_poll_interval_seconds: float = 0.5
    worker_concurrency: int = 2
    task_max_attempts: int = 3
    task_retry_base_delay_seconds: float = 2.0
    #: A task whose lease is older than this is considered orphaned by a crashed
    #: worker and is requeued on startup / during sweeps.
    task_lease_seconds: float = 900.0

    # -- Events / WebSocket ----------------------------------------------
    event_schema_version: int = 1
    #: Events replayed to a client that connects with ``?since_seq=``.
    ws_replay_limit: int = 500

    # -- Logging ----------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    # ---------------------------------------------------------------- utils
    @field_validator("data_dir", mode="after")
    @classmethod
    def _resolve_data_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else (REPO_ROOT / value).resolve()

    @field_validator("cors_origins", "enabled_providers", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Allow ``ESCO_CORS_ORIGINS=a,b`` in addition to JSON lists."""
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("bootstrap_admin_email", mode="after")
    @classmethod
    def _admin_email_must_be_loginable(cls, value: str) -> str:
        """Refuse to seed an account the login endpoint would reject.

        ``POST /api/auth/login`` validates ``email`` as an ``EmailStr``, and
        ``email-validator`` rejects reserved special-use domains — ``.local``,
        ``.test``, ``.localhost``, ``.invalid``. Seeding ``admin@esco.local``
        therefore produced a fresh install with an administrator nobody could
        ever sign in as. Applying the same check the endpoint applies turns that
        into a startup error instead of an unusable account.
        """
        from pydantic import TypeAdapter

        try:
            TypeAdapter(EmailStr).validate_python(value)
        except PydanticValidationError as exc:
            raise ValueError(
                f"ESCO_BOOTSTRAP_ADMIN_EMAIL={value!r} would be rejected by the "
                f"login endpoint, so the seeded administrator could never sign "
                f"in: {exc.errors()[0]['msg']}"
            ) from exc
        return value

    @model_validator(mode="after")
    def _validate_provider_wiring(self) -> Settings:
        """Fail fast on a configuration that promises a capability it lacks."""
        if ProviderKind.MOCK not in self.enabled_providers:
            # The mock provider is what makes the offline demo and the test
            # suite work; it is always available.
            self.enabled_providers = [ProviderKind.MOCK, *self.enabled_providers]

        if self.default_provider not in self.enabled_providers:
            raise ValueError(
                f"default_provider={self.default_provider!r} is not in "
                f"enabled_providers={[p.value for p in self.enabled_providers]}"
            )

        if ProviderKind.OPENAI in self.enabled_providers and not self.openai_api_key:
            raise ValueError("openai provider enabled but ESCO_OPENAI_API_KEY is unset")
        if ProviderKind.ANTHROPIC in self.enabled_providers and not self.anthropic_api_key:
            raise ValueError("anthropic provider enabled but ESCO_ANTHROPIC_API_KEY is unset")

        if self.environment == "production":
            if self.jwt_secret.get_secret_value() == "dev-only-insecure-secret-change-me":
                raise ValueError("ESCO_JWT_SECRET must be set outside demo/development")
            if self.bootstrap_admin_password.get_secret_value() == "esco-demo-admin":
                raise ValueError("ESCO_BOOTSTRAP_ADMIN_PASSWORD must be set in production")
        return self

    # -- Derived paths ----------------------------------------------------
    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    def project_dir(self, project_id: object) -> Path:
        return self.projects_dir / str(project_id)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def provider_enabled(self, kind: ProviderKind) -> bool:
        return kind in self.enabled_providers


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Tests call ``get_settings.cache_clear()`` after mutating the environment.
    """
    return Settings()
