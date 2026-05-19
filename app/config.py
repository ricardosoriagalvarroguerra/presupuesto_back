from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Ruta absoluta al .env: <repo>/backend/.env, resuelto desde la ubicación de este archivo.
# Esto evita que el backend dependa de desde dónde se lance uvicorn (raíz repo vs backend/).
_ENV_PATH = (Path(__file__).resolve().parent.parent / ".env")


def _normalize_async_dsn(dsn: str) -> str:
    """Railway/Heroku entregan `postgresql://`; SQLAlchemy async necesita
    `postgresql+asyncpg://`. Normalizamos aquí para que el deploy no requiera
    setear dos variables idénticas con prefijos distintos."""
    if dsn.startswith("postgresql+"):
        return dsn
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://"):]
    return dsn


def _normalize_sync_dsn(dsn: str) -> str:
    """Variante sync para Alembic (usa psycopg2)."""
    if dsn.startswith("postgresql+psycopg2://"):
        return dsn
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]
    if dsn.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg2://" + dsn[len("postgresql+asyncpg://"):]
    if dsn.startswith("postgresql://"):
        return "postgresql+psycopg2://" + dsn[len("postgresql://"):]
    return dsn


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(...)
    # En Railway alcanza con setear DATABASE_URL — el sync DSN se deriva
    # automáticamente. Si querés override explícito (ej. diferente cluster
    # para migraciones), seteá DATABASE_URL_SYNC también.
    database_url_sync: str = Field(default="")

    app_env: str = "development"
    app_secret_key: str = Field(..., min_length=32)
    app_cors_origins: str = "http://localhost:5173"

    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 480

    attachment_storage: str = "local"
    attachment_local_path: str = "./storage/adjuntos"

    k2b_api_base_url: str = ""
    k2b_api_token: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.app_cors_origins.split(",") if o.strip()]

    @property
    def async_dsn(self) -> str:
        """DSN normalizado para SQLAlchemy async (asyncpg)."""
        return _normalize_async_dsn(self.database_url)

    @property
    def sync_dsn(self) -> str:
        """DSN normalizado para Alembic / scripts sync (psycopg2). Si no se
        seteó DATABASE_URL_SYNC explícito, se deriva de DATABASE_URL."""
        return _normalize_sync_dsn(self.database_url_sync or self.database_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
