import logging
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from .models import Base

logger = logging.getLogger(__name__)

# MySQL database configuration (override with DATABASE_URL for local testing)
MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")
MYSQL_USER = os.getenv("MYSQL_USER", "browseruse")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "password")
MYSQL_DB = os.getenv("MYSQL_DB", "browseruse")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"mysql+asyncmy://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}"
)

engine = create_async_engine(DATABASE_URL, echo=False)

AsyncSessionLocal = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)


async def migrate_schema():
    """
    Idempotent schema migration for new columns.

    Adds container-tracking columns to sessions table if they don't exist.
    Safe to run multiple times — ALTER TABLE failures are caught and logged.
    """
    new_columns = [
        ("container_id", "VARCHAR(64) NULL"),
        ("container_name", "VARCHAR(128) NULL"),
        ("novnc_port", "INTEGER NULL"),
        ("cdp_url", "VARCHAR(256) NULL"),
    ]

    async with engine.begin() as conn:
        for col_name, col_def in new_columns:
            try:
                await conn.execute(
                    text(
                        f"ALTER TABLE sessions ADD COLUMN {col_name} {col_def}"
                    )
                )
                logger.info("Added column sessions.%s", col_name)
            except Exception:
                # Column already exists or DB doesn't support this DDL — safe to ignore
                logger.debug("Column sessions.%s may already exist, skipping.", col_name)

        # Ensure messages.content can hold large payloads (base64 screenshots)
        try:
            await conn.execute(
                text("ALTER TABLE messages MODIFY COLUMN content LONGTEXT NULL")
            )
            logger.info("Modified messages.content to LONGTEXT.")
        except Exception:
            logger.debug("messages.content may already be LONGTEXT, skipping.")


async def init_db():
    """Initialize database tables and run migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await migrate_schema()


async def get_session() -> AsyncSession:
    """Get a database session."""
    async with AsyncSessionLocal() as session:
        yield session