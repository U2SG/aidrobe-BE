from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

from alembic import context
from app.config.settings import get_settings
from app.infrastructure.persistence.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    # The app runs on asyncpg, but alembic must run migrations synchronously:
    # asyncpg's prepared-statement protocol rejects multi-statement DDL and
    # alembic's autocommit_block (used by CONCURRENTLY index migrations like
    # 0018-0020) can't drive the async-bridged connection. psycopg2 (a project
    # dependency) handles both. Swap the driver for migrations only.
    return get_settings().DATABASE_URL.replace("+asyncpg", "+psycopg2")


def run_migrations_offline() -> None:
    url = _sync_url()
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
    # Commit the prep so the connection isn't left mid-transaction: otherwise
    # alembic sees an open connection transaction but its own _transaction is
    # None, and autocommit_block() (CONCURRENTLY migrations 0018-0020) asserts.
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Recommended when migrations contain autocommit blocks: each migration
        # gets its own transaction so an autocommit block can commit + restore.
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _sync_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        do_run_migrations(connection)

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
