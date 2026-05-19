"""Tests de autorización por scope (cross-VP).

Estos son los tests que cierran la regresión más grave que detectamos hoy:
un usuario VPD no debe poder ver ni modificar solicitudes VPF. Si estos
tests pasan, el aislamiento por scope sigue intacto.

Crea su propia solicitud VPF descartable (ciclo 2096) — antes dependía de la
#13 sembrada manualmente, lo cual rompía si alguien la borraba para hacer
pruebas (es lo que pasó).
"""
import pytest
import pytest_asyncio
from sqlalchemy import text


@pytest_asyncio.fixture(scope="module")
async def SID_VPF():
    """Crea una solicitud VPF en ciclo 2096 que persiste durante todos los tests
    del módulo y se borra al final."""
    from app.db import SessionLocal
    async with SessionLocal() as db:
        await db.execute(text("""
            DELETE FROM planificacion.solicitud
            WHERE ciclo_id IN (SELECT id FROM core.ciclo_presupuestario WHERE anio=2096)
        """))
        await db.execute(text("DELETE FROM core.ciclo_presupuestario WHERE anio=2096"))
        await db.execute(text("""
            INSERT INTO core.ciclo_presupuestario (anio, nombre, estado, created_by)
            VALUES (2096, 'Ciclo 2096 (test scope)', 'planificacion',
                   (SELECT id FROM core.usuario WHERE username='mmednik'))
        """))
        cid = (await db.execute(
            text("SELECT id FROM core.ciclo_presupuestario WHERE anio=2096")
        )).scalar()
        sid = (await db.execute(text("""
            INSERT INTO planificacion.solicitud
              (ciclo_id, vp_codigo, nombre, etapa_actual, estado_workflow, created_by)
            VALUES (:c, 'VPF', 'Test scope cross-VP', 0, 'en_elaboracion',
                   (SELECT id FROM core.usuario WHERE username='mmednik'))
            RETURNING id
        """), {"c": cid})).scalar()
        await db.commit()

    yield sid

    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM planificacion.solicitud WHERE id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM core.ciclo_presupuestario WHERE anio=2096"))
        await db.commit()


@pytest.mark.asyncio
async def test_vpd_no_ve_solicitudes_de_otras_vps(client, token_vpd):
    """vmoreira (VPD, ver_todo=false) NO debe ver la solicitud VPF en la lista."""
    r = await client.get(
        "/planificacion/solicitudes",
        headers={"Authorization": f"Bearer {token_vpd}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    vps = {s["vp_codigo"] for s in data}
    assert "VPF" not in vps, f"VPD vio solicitudes de {vps}"


@pytest.mark.asyncio
async def test_vpd_no_abre_detalle_de_solicitud_de_otra_vp(client, token_vpd, SID_VPF):
    r = await client.get(
        f"/planificacion/solicitudes/{SID_VPF}",
        headers={"Authorization": f"Bearer {token_vpd}"},
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_vpd_no_modifica_solicitud_de_otra_vp(client, token_vpd, SID_VPF):
    """El bug original: VPD podía PATCH la solicitud VPF y persistía."""
    r = await client.patch(
        f"/planificacion/solicitudes/{SID_VPF}",
        headers={"Authorization": f"Bearer {token_vpd}"},
        json={"nombre": "deberia-ser-rechazado"},
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_vpd_no_ve_observaciones_ni_snapshots_ajenos(client, token_vpd, SID_VPF):
    for path in (
        f"/planificacion/solicitudes/{SID_VPF}/observaciones",
        f"/planificacion/solicitudes/{SID_VPF}/snapshots",
    ):
        r = await client.get(path, headers={"Authorization": f"Bearer {token_vpd}"})
        assert r.status_code == 403, f"{path} -> {r.status_code} {r.text}"


@pytest.mark.asyncio
async def test_vpd_no_ve_ejecucion_de_otra_vp(client, token_vpd):
    """VPD pidiendo dashboard de VPF debe rechazarse (ni con planillas_extra)."""
    r = await client.get(
        "/ejecucion/vp/VPF/dashboard",
        headers={"Authorization": f"Bearer {token_vpd}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_vpf_si_ve_su_solicitud(client, token_vpf_todo, SID_VPF):
    """Smoke: el dueño/admin sigue pudiendo abrirla. Si esto falla, los
    fixes rompieron el happy-path."""
    r = await client.get(
        f"/planificacion/solicitudes/{SID_VPF}",
        headers={"Authorization": f"Bearer {token_vpf_todo}"},
    )
    assert r.status_code == 200
    assert r.json()["solicitud"]["vp_codigo"] == "VPF"


@pytest.mark.asyncio
async def test_endpoints_publicos_legacy_ahora_exigen_auth(client):
    """Antes /catalogo /analisis /ejecucion /planificacion respondían 200 sin
    token. Ahora todos deben dar 401."""
    paths = [
        "/catalogo/items",
        "/catalogo/cuentas",
        "/catalogo/mapa-relaciones.xlsx",
        "/analisis/cuadros",
        "/analisis/dashboard/institucional",
        "/ejecucion/snapshots",
        "/ejecucion/comparativo",
        "/planificacion/workflows",
        "/planificacion/workflows-institucionales",
        "/planificacion/avance?ciclo_anio=2026",
    ]
    for p in paths:
        r = await client.get(p)
        assert r.status_code == 401, f"{p} -> {r.status_code}"
