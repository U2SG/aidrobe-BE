from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.config.settings import get_settings
from app.infrastructure.persistence.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = get_settings().DATABASE_URL
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


_PREPARE_VERSION_TABLE_SQL = """
-- Pre-create the version table with a wide column on fresh DBs so the
-- subsequent ALTER works for both fresh-and-legacy paths.
CREATE TABLE IF NOT EXISTS alembic_version (
    version_num VARCHAR(128) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'alembic_version'
          AND column_name = 'version_num'
          AND character_maximum_length < 128
    ) THEN
        EXECUTE 'ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)';
    END IF;
END $$;
"""


def do_run_migrations(connection: Connection) -> None:
    # Default alembic_version.version_num is VARCHAR(32); revision IDs in this
    # project (e.g. "0018_add_daily_recommendation_versioning") exceed that.
    # Pre-create with the wider column on fresh DBs and widen defensively on
    # legacy DBs so both paths work.
    connection.exec_driver_sql(_PREPARE_VERSION_TABLE_SQL)
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_settings().DATABASE_URL
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    import asyncio

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
