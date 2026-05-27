import os

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text

from app.config import get_settings  # noqa: F401  (settings.cache_clear ya corrido en conftest)


async def _login(client: AsyncClient, user: str, pwd: str) -> str:
    r = await client.post("/auth/login", json={"usuario": user, "password": pwd})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def H(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


# Usuarios reales del seed (passwords fijas, ver migración 016 + memoria del proyecto).
USERS = {
    "vpf_loader": ("mmednik", "Matias2026!"),   # ver_todo + vp_codigo='VPF' (también es el VP titular)
    "vpf_vp":     ("mmednik", "Matias2026!"),   # mismo: en este seed mmednik tiene rol vicepresidente
    "vpd_vp":     ("vmoreira", "Virginia2026!"),  # rol jefe_unidad VPD (NO es VP titular)
    "presidente": ("lbotafogo", "Luciana2026!"),
}


@pytest_asyncio.fixture
async def solicitud_descartable(client):
    """Crea una solicitud nueva para el test y la borra al final.

    Usa un ciclo_anio futuro (2099) para no chocar con la solicitud real (#13)
    en ciclo 2027 y respetar UNIQUE(ciclo_id, vp_codigo).
    """
    # Necesitamos un ciclo 2099 para no chocar con el UNIQUE (ciclo_id, vp_codigo).
    # Insertamos a mano y borramos al final.
    from app.db import SessionLocal
    async with SessionLocal() as db:
        # Limpieza preventiva por si quedó algo de una corrida anterior fallida.
        await db.execute(text("""
            DELETE FROM planificacion.evento_solicitud WHERE solicitud_id IN (SELECT s.id FROM planificacion.solicitud s JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id WHERE cp.anio=2099);
            DELETE FROM planificacion.linea_solicitud WHERE solicitud_id IN (SELECT s.id FROM planificacion.solicitud s JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id WHERE cp.anio=2099);
            DELETE FROM planificacion.snapshot_linea WHERE snapshot_id IN (SELECT ss.id FROM planificacion.snapshot_solicitud ss JOIN planificacion.solicitud s ON s.id=ss.solicitud_id JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id WHERE cp.anio=2099);
            DELETE FROM planificacion.snapshot_solicitud WHERE solicitud_id IN (SELECT s.id FROM planificacion.solicitud s JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id WHERE cp.anio=2099);
            DELETE FROM planificacion.observacion WHERE solicitud_id IN (SELECT s.id FROM planificacion.solicitud s JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id WHERE cp.anio=2099);
            DELETE FROM planificacion.solicitud WHERE ciclo_id IN (SELECT id FROM core.ciclo_presupuestario WHERE anio=2099)
        """))
        await db.execute(text("DELETE FROM core.ciclo_presupuestario WHERE anio = 2099"))
        await db.execute(text("""
            INSERT INTO core.ciclo_presupuestario (anio, nombre, estado, created_by)
            OUTPUT INSERTED.id
            VALUES (2099, 'Ciclo 2099 (test)', 'planificacion',
                   (SELECT id FROM core.usuario WHERE username='mmednik'))
        """))
        ciclo_id = (await db.execute(
            text("SELECT id FROM core.ciclo_presupuestario WHERE anio=2099")
        )).scalar()
        sid = (await db.execute(text("""
            INSERT INTO planificacion.solicitud
              (ciclo_id, vp_codigo, nombre, etapa_actual, estado_workflow, created_by)
            OUTPUT INSERTED.id
            VALUES (:c, 'VPF', 'Test workflow VPF', 0, 'en_elaboracion',
                   (SELECT id FROM core.usuario WHERE username='mmednik'))
        """), {"c": ciclo_id})).scalar()
        await db.commit()

    yield sid

    # Cleanup
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM planificacion.evento_solicitud WHERE solicitud_id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.snapshot_linea WHERE snapshot_id IN (SELECT id FROM planificacion.snapshot_solicitud WHERE solicitud_id=:s)"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.snapshot_solicitud WHERE solicitud_id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.observacion WHERE solicitud_id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.linea_solicitud WHERE solicitud_id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.solicitud WHERE id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM core.ciclo_presupuestario WHERE anio=2099"))
        await db.commit()


# -----------------------------------------------------------------------------
# Happy path completo (VPF → VP → Presidencia → aprobado)
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_workflow_happy_path_vpf(client, solicitud_descartable):
    sid = solicitud_descartable
    tok_loader = await _login(client, *USERS["vpf_loader"])
    tok_pres = await _login(client, *USERS["presidente"])

    # 1. Cargador envía a revisión del VP
    r = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                          json={"accion": "enviar_a_revision_vp"}, headers=H(tok_loader))
    assert r.status_code == 200, r.text
    assert r.json()["etapa_nueva"] == 1
    assert r.json()["estado_nuevo"] == "en_revision_vp"

    # 2. VP (mmednik tiene rol vicepresidente VPF) aprueba
    r = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                          json={"accion": "aprobar_vp"}, headers=H(tok_loader))
    assert r.status_code == 200, r.text
    assert r.json()["etapa_nueva"] == 2
    assert r.json()["estado_nuevo"] == "en_revision_presidencia"

    # 3. Presidencia aprueba
    r = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                          json={"accion": "aprobar_presidencia"}, headers=H(tok_pres))
    assert r.status_code == 200, r.text
    assert r.json()["etapa_nueva"] == 3
    assert r.json()["estado_nuevo"] == "aprobado"


# -----------------------------------------------------------------------------
# RBAC: jefe_unidad de OTRA VP no puede actuar como VP de VPF
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vpd_no_puede_aprobar_vpf(client, solicitud_descartable):
    sid = solicitud_descartable
    tok_loader = await _login(client, *USERS["vpf_loader"])
    tok_otra_vp = await _login(client, *USERS["vpd_vp"])

    # Cargador VPF envía
    r = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                          json={"accion": "enviar_a_revision_vp"}, headers=H(tok_loader))
    assert r.status_code == 200

    # vmoreira (VPD) intenta aprobar VPF → 403
    r = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                          json={"accion": "aprobar_vp"}, headers=H(tok_otra_vp))
    assert r.status_code == 403, r.text


# -----------------------------------------------------------------------------
# RBAC: cargador (no presidencia) no puede aprobar en etapa 2
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_loader_no_puede_aprobar_presidencia(client, solicitud_descartable):
    sid = solicitud_descartable
    tok_loader = await _login(client, *USERS["vpf_loader"])

    # Empujamos hasta etapa 2
    await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                      json={"accion": "enviar_a_revision_vp"}, headers=H(tok_loader))
    await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                      json={"accion": "aprobar_vp"}, headers=H(tok_loader))

    # mmednik no es Presidente ni Jefe Gabinete → 403
    r = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                          json={"accion": "aprobar_presidencia"}, headers=H(tok_loader))
    assert r.status_code == 403, r.text


# -----------------------------------------------------------------------------
# Devolución del VP: vuelve a etapa 0 con estado devuelto_vp
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vp_devuelve_y_loader_reenvia(client, solicitud_descartable):
    sid = solicitud_descartable
    tok_loader = await _login(client, *USERS["vpf_loader"])

    await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                      json={"accion": "enviar_a_revision_vp"}, headers=H(tok_loader))
    r = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                          json={"accion": "devolver_vp",
                                "comentario": "Revisar partidas de misiones"},
                          headers=H(tok_loader))
    assert r.status_code == 200, r.text
    assert r.json()["etapa_nueva"] == 0
    assert r.json()["estado_nuevo"] == "devuelto_vp"

    # Reenviar después de devolución
    r = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                          json={"accion": "enviar_a_revision_vp"}, headers=H(tok_loader))
    assert r.status_code == 200
    assert r.json()["estado_nuevo"] == "en_revision_vp"


# -----------------------------------------------------------------------------
# Devolución de Presidencia: vuelve a 0 y debe re-pasar por VP
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_presidencia_devuelve_vuelve_por_vp(client, solicitud_descartable):
    sid = solicitud_descartable
    tok_loader = await _login(client, *USERS["vpf_loader"])
    tok_pres = await _login(client, *USERS["presidente"])

    await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                      json={"accion": "enviar_a_revision_vp"}, headers=H(tok_loader))
    await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                      json={"accion": "aprobar_vp"}, headers=H(tok_loader))

    # Presidencia devuelve
    r = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                          json={"accion": "devolver_presidencia",
                                "comentario": "Ajustar monto de servicios"},
                          headers=H(tok_pres))
    assert r.status_code == 200, r.text
    assert r.json()["etapa_nueva"] == 0
    assert r.json()["estado_nuevo"] == "devuelto_presidencia"

    # NO se puede saltar al VP directo: hay que reenviar etapa 0 → 1.
    # NO se puede usar enviar_a_revision_presidencia (es solo PRE/GOB).
    r = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                          json={"accion": "enviar_a_revision_presidencia"},
                          headers=H(tok_loader))
    assert r.status_code == 409, "PRE/GOB-only debe rechazar para VPF"

    # Reenvío correcto: etapa 0 → 1 (VP de nuevo)
    r = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                          json={"accion": "enviar_a_revision_vp"}, headers=H(tok_loader))
    assert r.status_code == 200
    assert r.json()["etapa_nueva"] == 1


# -----------------------------------------------------------------------------
# Estados inválidos rechazados
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_aprobar_vp_sin_estar_en_revision(client, solicitud_descartable):
    """En etapa 0 (en_elaboracion) no se puede aprobar VP — primero hay que enviar."""
    sid = solicitud_descartable
    tok_loader = await _login(client, *USERS["vpf_loader"])
    r = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                          json={"accion": "aprobar_vp"}, headers=H(tok_loader))
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_accion_inexistente_400(client, solicitud_descartable):
    tok = await _login(client, *USERS["vpf_loader"])
    r = await client.post(f"/planificacion/solicitudes/{solicitud_descartable}/transicion",
                          json={"accion": "inexistente"}, headers=H(tok))
    assert r.status_code == 400
