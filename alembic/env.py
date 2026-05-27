from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
# `sync_dsn` resuelve un DSN pyodbc tanto desde un DATABASE_URL_SYNC explícito
# (override del operador) como derivado automáticamente desde DATABASE_URL.
# NO usamos `set_main_option("sqlalchemy.url", ...)` porque el DSN puede traer
# `%` URL-encoded (passwords con caracteres especiales) y ConfigParser
# intenta interpolarlos como variables. Lo pasamos directo a create_engine.
DSN = settings.sync_dsn

target_metadata = None  # set when ORM models are wired in


def run_migrations_offline() -> None:
    context.configure(
        url=DSN,
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        # SQL Server: la tabla alembic_version vive en el schema `dbo` por default
        # (sin necesidad de un schema neutro como `public` en PG).
        version_table_schema="dbo",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(DSN, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema="dbo",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
