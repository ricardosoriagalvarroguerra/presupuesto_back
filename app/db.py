"""Engine + Session factory async. Lo importan todos los routers como dependency.

Tres detalles de configuración que conviene tener presentes:

1) `pool_pre_ping=True` parece la opción obvia pero rompe con aioodbc en
   SQLAlchemy 2.x. El ping se ejecuta en contexto sync fuera del greenlet
   adapter y termina en `MissingGreenlet`. La alternativa es `pool_recycle=1800`
   (recicla conexiones cada 30 min), que cubre el caso de servers que matan
   conexiones idle.

2) `expire_on_commit=False` en el sessionmaker. Con el default (True), después
   de commit() todos los objetos quedan expirados y acceder a un atributo
   dispara una recarga; en async eso es un `await` automático que, si el
   código está en una función sync, falla con `MissingGreenlet`. Por eso lo
   apagamos.

3) `echo=` solo en dev. En prod hay que dejarlo False porque echo serializa
   cada query a stdout y satura los logs cuando hay tráfico real.
"""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    # Tomamos el DSN ya normalizado (aioodbc, no importa cómo lo seteó el user).
    settings.async_dsn,
    # NO pool_pre_ping — incompatible con aioodbc (ver docstring arriba).
    # Reciclamos cada 30min en cambio.
    pool_recycle=1800,
    pool_size=10,
    max_overflow=20,
    echo=settings.app_env == "development",
)

# expire_on_commit=False → ver punto 2 del docstring.
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """Base SQLAlchemy 2.0 declarativa. Cada modelo declara su schema vía __table_args__."""


async def get_db() -> AsyncIterator[AsyncSession]:
    """Dependency de FastAPI. Crea una sesión por request, la cierra al final.

    El `async with` se encarga de commit/rollback implícitos según el final
    de la función llamadora. Los endpoints suelen hacer commit explícito al
    final para mensajear éxito; si algo levanta excepción, el rollback va
    automático cuando el context se cierra.
    """
    async with SessionLocal() as session:
        yield session
