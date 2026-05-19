"""Fixtures de pytest para el backend.

Los tests usan la base real (presupuesto2027) en READ-ONLY o con operaciones
idempotentes. NO tocan datos sensibles ni crean usuarios. Si en el futuro se
necesita una BD aislada, esto se debería migrar a `presupuesto2027_test` con
fixture `create_all` + truncate por test.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Forzar entorno de tests antes de importar app (desactiva rate-limit).
os.environ["APP_ENV"] = "test"

from app.config import get_settings  # noqa: E402
get_settings.cache_clear()  # por si algo ya importó config con APP_ENV viejo

from app.main import app  # noqa: E402

assert get_settings().app_env == "test", "tests deben correr con APP_ENV=test"


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _desactivar_requiere_cambio_password_para_test_users():
    """En la BD seed los 17 usuarios tienen requiere_cambio_password=true
    (forzar reset al primer login). En tests eso bloquearía cualquier
    operación detrás del wizard. Apagamos el flag para los usuarios que los
    tests usan (idempotente; no toca al resto)."""
    from sqlalchemy import text
    from app.db import SessionLocal

    USUARIOS_DE_TEST = ["mmednik", "vmoreira", "lbotafogo", "mcalvino", "ajustiniano"]
    async with SessionLocal() as db:
        await db.execute(
            text("""UPDATE core.usuario
                    SET requiere_cambio_password = false
                    WHERE username = ANY(:u)"""),
            {"u": USUARIOS_DE_TEST},
        )
        await db.commit()
    yield


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """Cliente httpx que llama a la app FastAPI in-process (sin red)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _login(client: AsyncClient, usuario: str, password: str) -> str:
    """Hace login y devuelve el access_token. Falla con AssertionError si !=200."""
    r = await client.post("/auth/login", json={"usuario": usuario, "password": password})
    assert r.status_code == 200, f"login {usuario} -> {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest_asyncio.fixture
async def token_vpf_todo(client: AsyncClient) -> str:
    """Token de mmednik (VPF, ver_todo=true)."""
    return await _login(client, "mmednik", "Matias2026!")


@pytest_asyncio.fixture
async def token_vpd(client: AsyncClient) -> str:
    """Token de vmoreira (VPD, ver_todo=false). Usado para validar negación cross-VP."""
    return await _login(client, "vmoreira", "Virginia2026!")
