"""Application settings loaded from environment variables / `.env`."""

from functools import lru_cache
from typing import Annotated, List, Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly typed application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application -------------------------------------------------------
    PROJECT_NAME: str = "Niche Podcast Clipper"
    API_V1_PREFIX: str = "/api/v1"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    # NoDecode skips pydantic-settings' JSON decoding of the dotenv string so
    # the `mode="before"` validator below can split the CSV value instead.
    FRONTEND_ORIGINS: Annotated[List[str], NoDecode] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    # Disable /docs, /redoc and /openapi.json outside development.
    ENABLE_DOCS: bool = True
    # Emit one JSON object per log record (for Loki/Datadog/ELK aggregation)
    # instead of the human-readable timestamped line. Recommended in production.
    LOG_FORMAT_JSON: bool = False

    # --- Database ----------------------------------------------------------
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/podcast_clipper"
    # Create the schema on a fresh database and apply pending Alembic
    # migrations automatically during application startup. Set to `false` to
    # manage migrations manually (`alembic upgrade head`), e.g. behind an
    # orchestrator that owns the migration step.
    AUTO_MIGRATE_DB: bool = True

    # --- Redis -------------------------------------------------------------
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"
    CACHE_TTL_SECONDS: int = 3600
    # Prefork pool size for the Celery worker. Keep this LOW: every child
    # process loads its own copy of Whisper + Flan-T5 and applies the numerical
    # thread budget, so a value matching all cores (Celery's default) forks a
    # process per core and each then spawns more threads — the machine thrashes
    # at 100% CPU and appears frozen. Override with CELERY_CONCURRENCY or an
    # explicit ``--concurrency N`` when you have the headroom.
    CELERY_CONCURRENCY: int = 2

    # --- Security ----------------------------------------------------------
    SECRET_KEY: str = "change-me-super-secret-jwt-key"
    REFRESH_SECRET_KEY: str = "change-me-super-secret-refresh-key"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7

    # --- Storage -----------------------------------------------------------
    STORAGE_DIR: str = "./media_storage"
    MAX_UPLOAD_SIZE_MB: int = 2048
    ALLOWED_MEDIA_EXTENSIONS: Annotated[List[str], NoDecode] = [
        ".mp3",
        ".mp4",
        ".wav",
        ".m4a",
        ".mov",
        ".mkv",
        ".webm",
    ]

    # --- Local AI ----------------------------------------------------------
    ASR_MODEL_NAME: str = "openai/whisper-base"
    HIGHLIGHT_MODEL_NAME: str = "google/flan-t5-base"
    AI_DEVICE: str = "cpu"
    # Cap the number of threads the numerical libraries may spin up. On CPU-only
    # boxes torch (Whisper/Flan-T5), OpenCV (OpenMP) and NumPy (OpenBLAS/MKL)
    # each default to `num_cores` threads; with two API workers and two Celery
    # workers that is 4x core-count threads contending for every scheduling
    # slice, which presents as a "hung" 100%-CPU machine. Bounding each process
    # removes the oversubscription and, counter-intuitively, speeds inference.
    AI_NUM_THREADS: int = 0  # 0 = auto (physical cores); set to cap explicitly
    ASR_CHUNK_LENGTH_S: int = 30
    # Overlap, in seconds, on BOTH sides of each chunk. transformers' chunker
    # advances by `chunk_length - 2 * stride`, so the library-default ratio
    # (chunk/6 = 5s) re-decoded ~40% of the audio twice. A short 2s stride
    # keeps boundary context for word timestamps while cutting that waste to
    # a few percent.
    ASR_STRIDE_S: int = 2
    HF_HOME: str = "./models"
    TRANSFORMERS_OFFLINE: bool = False

    # --- Rendering ---------------------------------------------------------
    OUTPUT_WIDTH: int = 1080
    OUTPUT_HEIGHT: int = 1920
    OUTPUT_FPS: int = 30

    # --- Rate limiting -----------------------------------------------------
    RATE_LIMIT_REQUESTS: int = 120
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    # Stricter per-IP budget for credential endpoints (login/register/refresh)
    # so password guessing cannot iterate at the general API pace.
    AUTH_RATE_LIMIT_REQUESTS: int = 10
    AUTH_RATE_LIMIT_WINDOW_SECONDS: int = 60

    @field_validator("FRONTEND_ORIGINS", "ALLOWED_MEDIA_EXTENSIONS", mode="before")
    @classmethod
    def _split_csv(cls, value):
        """Allow comma separated strings in `.env` for list fields."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _reject_default_secrets_in_production(self) -> "Settings":
        """Fail fast when production runs with the template JWT secrets.

        This checks the *parsed* ``ENVIRONMENT`` rather than ``os.environ``:
        pydantic-settings reads ``.env`` without exporting it to the process
        environment, so the previous ``os.environ`` lookup silently allowed the
        template secrets whenever ``ENVIRONMENT=production`` lived only in the
        dotenv file.
        """
        if self.ENVIRONMENT.strip().lower() not in {"production", "prod", "staging"}:
            return self
        for field_name in ("SECRET_KEY", "REFRESH_SECRET_KEY"):
            value = (getattr(self, field_name) or "").strip()
            lowered = value.lower()
            if not value or "change-me" in lowered or "your-super-secret" in lowered:
                raise ValueError(
                    f"{field_name} still contains a template value; generate a strong "
                    "random secret (e.g. `openssl rand -hex 32`) before deploying to "
                    "production."
                )
        return self

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    """Return a cached `Settings` instance."""
    return Settings()


settings = get_settings()
