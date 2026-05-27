from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolvemos el .env por ruta absoluta (<repo>/backend/.env) en lugar de
# relativa. Si fuera relativa, dependería de DESDE DÓNDE se arranca uvicorn
# (`cd backend && uvicorn` vs `uvicorn --app-dir backend` toman .env de
# directorios distintos), y es difícil de debuggear cuando algo carga mal.
_ENV_PATH = (Path(__file__).resolve().parent.parent / ".env")


def _looks_like_raw_odbc(dsn: str) -> bool:
    """¿Esto huele a connection string ODBC nativa (Driver=...; Server=...;)?

    Las URL SQLAlchemy todas tienen `://`. Las ODBC nativas tienen `=` antes
    del primer `;`. Esta heurística simple alcanza para distinguirlas.
    """
    return "://" not in dsn and "=" in dsn


def _wrap_raw_odbc(dsn: str, driver: str) -> str:
    """Empaqueta una connection string ODBC dentro del formato que entiende SQLAlchemy.

    Sale: `mssql+<driver>:///?odbc_connect=<urlencoded>`

    Esto es útil cuando tenés una string copiada del SSMS o de docs de MS y
    no querés desarmarla a URL SQLAlchemy. Pasala raw en DATABASE_URL y este
    helper la envuelve.
    """
    return f"mssql+{driver}:///?odbc_connect={quote_plus(dsn)}"


def _normalize_async_dsn(dsn: str) -> str:
    """Resuelve la DSN final que va al engine async, sea cual sea el formato de entrada.

    Cuatro casos posibles según cómo te llegó la variable de entorno:
      - `mssql+aioodbc://...`  → ya está, devolvemos tal cual.
      - `mssql+pyodbc://...`   → cambiamos pyodbc por aioodbc (algunas
                                  herramientas de despliegue inyectan una y
                                  necesitamos la otra).
      - `mssql://...`          → le falta el driver, le pegamos `+aioodbc`.
      - `Driver={...};...`     → ODBC raw, lo envolvemos.
    """
    if dsn.startswith("mssql+aioodbc://"):
        return dsn
    if dsn.startswith("mssql+pyodbc://"):
        return "mssql+aioodbc://" + dsn[len("mssql+pyodbc://"):]
    if dsn.startswith("mssql://"):
        return "mssql+aioodbc://" + dsn[len("mssql://"):]
    if _looks_like_raw_odbc(dsn):
        return _wrap_raw_odbc(dsn, "aioodbc")
    return dsn


def _normalize_sync_dsn(dsn: str) -> str:
    """Variante sync (pyodbc) para Alembic / scripts."""
    if dsn.startswith("mssql+pyodbc://"):
        return dsn
    if dsn.startswith("mssql+aioodbc://"):
        return "mssql+pyodbc://" + dsn[len("mssql+aioodbc://"):]
    if dsn.startswith("mssql://"):
        return "mssql+pyodbc://" + dsn[len("mssql://"):]
    if _looks_like_raw_odbc(dsn):
        return _wrap_raw_odbc(dsn, "pyodbc")
    return dsn


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(...)
    # Si querés un override explícito para Alembic / scripts sync (ej. otro
    # cluster, otra credencial), seteá DATABASE_URL_SYNC. Si no, se deriva
    # automáticamente desde DATABASE_URL.
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
        """DSN normalizado para SQLAlchemy async (aioodbc)."""
        return _normalize_async_dsn(self.database_url)

    @property
    def sync_dsn(self) -> str:
        """DSN normalizado para Alembic / scripts sync (pyodbc). Si no se
        seteó DATABASE_URL_SYNC explícito, se deriva de DATABASE_URL."""
        return _normalize_sync_dsn(self.database_url_sync or self.database_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
