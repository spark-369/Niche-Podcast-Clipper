"""Bootstrap helper: create or update the database schema automatically.

``ensure_database()`` runs during application startup so `uvicorn main:app`
works against a bare PostgreSQL instance with zero manual steps: a fresh
database is built from the Alembic migrations and an existing one receives
any pending migrations — no manual `alembic upgrade head` required.

Schema only: no application data is ever inserted; accounts are created
through the regular `/auth/register` flow.
"""

import logging
from pathlib import Path

from sqlalchemy import inspect

from core.config import settings
from db.session import Base, engine

import models  # noqa: F401  (registers every table on Base.metadata)

logger = logging.getLogger(__name__)

# backend/ — used to locate alembic.ini and the migration scripts regardless
# of the process working directory.
BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config():
    """Build a programmatic Alembic configuration bound to this project."""
    from alembic.config import Config

    ini_path = BACKEND_ROOT / "alembic.ini"
    config = Config(str(ini_path)) if ini_path.exists() else Config()
    # Override the CLI's relative `script_location` with an absolute path so
    # migrations resolve even when the process was started elsewhere.
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    # alembic/env.py honours this flag: in-process migrations must not
    # reconfigure logging over the application's `configure_logging()` setup.
    config.attributes["configure_logger"] = False
    return config


def _run_alembic_upgrade() -> None:
    """Apply every pending migration (the in-process `alembic upgrade head`)."""
    from alembic import command

    command.upgrade(_alembic_config(), "head")


def _stamp_head() -> None:
    """Record `head` in `alembic_version` without executing any revision."""
    from alembic import command

    command.stamp(_alembic_config(), "head")


def ensure_database() -> None:
    """Create or update the database schema automatically at startup.

    Schema only — no data is ever seeded or modified.

    Strategy:

    * empty database — apply every Alembic migration from scratch;
    * migrated database (`alembic_version` present) — apply pending
      migrations so an existing schema is brought up to date;
    * legacy ``create_all()`` database (tables, no version row) — create any
      missing tables, then stamp ``head`` to adopt the migration history;
    * any Alembic failure — fall back to ``Base.metadata.create_all()`` so
      the API still boots, mirroring the lifespan's never-abort policy.
    """
    inspector = inspect(engine)
    has_version_table = inspector.has_table("alembic_version")
    existing_tables = inspector.get_table_names()

    try:
        if has_version_table:
            logger.info("Checking for pending database migrations")
            _run_alembic_upgrade()
        elif not existing_tables:
            logger.info("Empty database detected: creating schema via Alembic migrations")
            _run_alembic_upgrade()
        else:
            Base.metadata.create_all(bind=engine)
            _stamp_head()
            logger.info(
                "Unversioned database adopted at head; run `alembic upgrade head` "
                "manually if column-level changes might be missing"
            )
    except Exception as exc:  # pragma: no cover - infra dependent
        logger.warning(
            "Alembic auto-migration failed (%s); falling back to create_all", exc
        )
        Base.metadata.create_all(bind=engine)

    logger.info("Database schema ensured")


if __name__ == "__main__":  # pragma: no cover - manual bootstrap entry point
    logging.basicConfig(level=logging.INFO)
    with engine.connect() as _:
        logger.info("Connected to %s", settings.DATABASE_URL)
    ensure_database()