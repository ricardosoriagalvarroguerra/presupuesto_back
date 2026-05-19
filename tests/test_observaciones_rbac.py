"""Tests del RBAC reforzado en observaciones (cierre de hallazgo P1-3 de Codex).

Antes: cualquier usuario con acceso a la solicitud (incluido un usuario
cross-VP por planilla) podía crear observaciones o resolverlas.
Ahora:
  - Crear: solo el revisor de la etapa (VP titular en en_revision_vp;
    Presidencia/Gabinete en en_revision_presidencia).
  - Resolver: solo cargadores de la VP de la solicitud (o admin).
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text


async def _login(client: AsyncClient, user: str, pwd: str) -> str:
    r = await client.post("/auth/login", json={"usuario": user, "password": pwd})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def H(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


@pytest_asyncio.fixture
async def sid_en_revision_vp():
    """Crea solicitud VPF, la lleva a estado en_revision_vp, y limpia al final."""
    from app.db import SessionLocal
    async with SessionLocal() as db:
        await db.execute(text("""
            DELETE FROM planificacion.solicitud
            WHERE ciclo_id IN (SELECT id FROM core.ciclo_presupuestario WHERE anio=2098)
        """))
        await db.execute(text("DELETE FROM core.ciclo_presupuestario WHERE anio=2098"))
        await db.execute(text("""
            INSERT INTO core.ciclo_presupuestario (anio, nombre, estado, created_by)
            VALUES (2098, 'Ciclo 2098 (test)', 'planificacion',
                   (SELECT id FROM core.usuario WHERE username='mmednik'))
        """))
        cid = (await db.execute(
            text("SELECT id FROM core.ciclo_presupuestario WHERE anio=2098")
        )).scalar()
        sid = (await db.execute(text("""
            INSERT INTO planificacion.solicitud
              (ciclo_id, vp_codigo, nombre, etapa_actual, estado_workflow, created_by)
            VALUES (:c, 'VPF', 'Test obs RBAC', 1, 'en_revision_vp',
                   (SELECT id FROM core.usuario WHERE username='mmednik'))
            RETURNING id
        """), {"c": cid})).scalar()
        await db.commit()

    yield sid

    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM planificacion.solicitud WHERE id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM core.ciclo_presupuestario WHERE anio=2098"))
        await db.commit()


@pytest.mark.asyncio
async def test_vp_titular_si_puede_crear_observacion_en_etapa_vp(client, sid_en_revision_vp):
    """mmednik es VP titular de VPF → puede observar en en_revision_vp."""
    tok = await _login(client, "mmednik", "Matias2026!")
    r = await client.post(
        f"/planificacion/solicitudes/{sid_en_revision_vp}/observaciones",
        headers=H(tok),
        json={"alcance": "general", "texto": "Falta detallar partidas",
              "accion_sugerida": "solo_comentario"},
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_vp_de_otra_vp_no_puede_crear_observacion(client, sid_en_revision_vp):
    """vmoreira es jefe_unidad de VPD → NO debe observar una solicitud VPF.
    Falla 403 — antes del fix daba 200 si tenía acceso lateral."""
    tok = await _login(client, "vmoreira", "Virginia2026!")
    r = await client.post(
        f"/planificacion/solicitudes/{sid_en_revision_vp}/observaciones",
        headers=H(tok),
        json={"alcance": "general", "texto": "intento ajeno",
              "accion_sugerida": "solo_comentario"},
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_presidencia_no_puede_crear_obs_durante_revision_vp(client, sid_en_revision_vp):
    """Presidencia NO interviene en etapa 1 — solo el VP titular puede observar
    en en_revision_vp. lbotafogo tiene scope global pero NO en esta etapa."""
    # Aseguramos que lbotafogo tenga el flag de password apagado (conftest ya lo hace).
    tok = await _login(client, "lbotafogo", "Luciana2026!")
    r = await client.post(
        f"/planificacion/solicitudes/{sid_en_revision_vp}/observaciones",
        headers=H(tok),
        json={"alcance": "general", "texto": "no es mi etapa todavía",
              "accion_sugerida": "solo_comentario"},
    )
    assert r.status_code == 403, r.text
