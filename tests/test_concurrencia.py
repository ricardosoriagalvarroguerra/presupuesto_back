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
async def sid_listo_para_enviar():
    """Crea una solicitud VPF en en_elaboracion y devuelve sid + ciclo_id."""
    from app.db import SessionLocal
    async with SessionLocal() as db:
        await db.execute(text("""
            DELETE FROM planificacion.evento_solicitud WHERE solicitud_id IN (SELECT s.id FROM planificacion.solicitud s JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id WHERE cp.anio=2097);
            DELETE FROM planificacion.linea_solicitud WHERE solicitud_id IN (SELECT s.id FROM planificacion.solicitud s JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id WHERE cp.anio=2097);
            DELETE FROM planificacion.snapshot_linea WHERE snapshot_id IN (SELECT ss.id FROM planificacion.snapshot_solicitud ss JOIN planificacion.solicitud s ON s.id=ss.solicitud_id JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id WHERE cp.anio=2097);
            DELETE FROM planificacion.snapshot_solicitud WHERE solicitud_id IN (SELECT s.id FROM planificacion.solicitud s JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id WHERE cp.anio=2097);
            DELETE FROM planificacion.observacion WHERE solicitud_id IN (SELECT s.id FROM planificacion.solicitud s JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id WHERE cp.anio=2097);
            DELETE FROM planificacion.solicitud WHERE ciclo_id IN (SELECT id FROM core.ciclo_presupuestario WHERE anio=2097)
        """))
        await db.execute(text("DELETE FROM core.ciclo_presupuestario WHERE anio=2097"))
        await db.execute(text("""
            INSERT INTO core.ciclo_presupuestario (anio, nombre, estado, created_by)
            OUTPUT INSERTED.id
            VALUES (2097, 'Ciclo 2097 (test)', 'planificacion',
                   (SELECT id FROM core.usuario WHERE username='mmednik'))
        """))
        cid = (await db.execute(
            text("SELECT id FROM core.ciclo_presupuestario WHERE anio=2097")
        )).scalar()
        sid = (await db.execute(text("""
            INSERT INTO planificacion.solicitud
              (ciclo_id, vp_codigo, nombre, etapa_actual, estado_workflow, created_by)
            OUTPUT INSERTED.id
            VALUES (:c, 'VPF', 'Test concurrencia', 0, 'en_elaboracion',
                   (SELECT id FROM core.usuario WHERE username='mmednik'))
        """), {"c": cid})).scalar()
        await db.commit()

    yield sid

    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM planificacion.evento_solicitud WHERE solicitud_id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.snapshot_linea WHERE snapshot_id IN (SELECT id FROM planificacion.snapshot_solicitud WHERE solicitud_id=:s)"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.snapshot_solicitud WHERE solicitud_id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.observacion WHERE solicitud_id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.linea_solicitud WHERE solicitud_id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.solicitud WHERE id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM core.ciclo_presupuestario WHERE anio=2097"))
        await db.commit()


@pytest.mark.asyncio
async def test_no_se_pueden_agregar_lineas_a_solicitud_ya_en_revision(client, sid_listo_para_enviar):
    """Caso TOCTOU: alguien envió a revisión; un POST de línea posterior debe 409."""
    sid = sid_listo_para_enviar
    tok = await _login(client, "mmednik", "Matias2026!")

    # Transicionamos a en_revision_vp
    r = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                          json={"accion": "enviar_a_revision_vp"}, headers=H(tok))
    assert r.status_code == 200

    # Intentamos agregar una línea — debe ser rechazada
    r = await client.post(
        f"/planificacion/solicitudes/{sid}/lineas",
        headers=H(tok),
        json={
            "planilla_template_id": 1,  # cualquier id válido del seed
            "item_id": 1,
            "cuenta_id": 1,
            "modalidad": "directa",
            "monto_solicitado": "100.00",
        },
    )
    # Puede ser 409 (estado no editable) o 400 (item/cuenta inválidos). Lo
    # importante: NUNCA 200 — la línea no debe entrar.
    assert r.status_code in (409, 400), f"línea coló en estado en_revision_vp: {r.status_code} {r.text}"
    if r.status_code == 409:
        assert "en_revision_vp" in r.text or "estado" in r.text.lower()


@pytest.mark.asyncio
async def test_doble_envio_a_revision_segundo_rechazado(client, sid_listo_para_enviar):
    """Si un usuario clickea 'Enviar' dos veces (o dos usuarios disparan en paralelo),
    el segundo POST de transición ve estado != en_elaboracion y devuelve 409."""
    sid = sid_listo_para_enviar
    tok = await _login(client, "mmednik", "Matias2026!")

    r1 = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                           json={"accion": "enviar_a_revision_vp"}, headers=H(tok))
    assert r1.status_code == 200

    r2 = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                           json={"accion": "enviar_a_revision_vp"}, headers=H(tok))
    assert r2.status_code == 409, r2.text


@pytest.mark.asyncio
async def test_modificar_linea_falla_si_solicitud_paso_a_revision(client, sid_listo_para_enviar):
    """Cargás una línea, alguien envía a revisión, intentás editarla → 409."""
    sid = sid_listo_para_enviar
    tok = await _login(client, "mmednik", "Matias2026!")

    # Insert directo de línea para no depender del catálogo (válido en este test).
    from app.db import SessionLocal
    async with SessionLocal() as db:
        lid = (await db.execute(text("""
            INSERT INTO planificacion.linea_solicitud
              (solicitud_id, planilla_template_id, item_id, cuenta_id, plan_id,
               modalidad, monto_solicitado, created_by)
            OUTPUT INSERTED.id
            VALUES (:s,
                    (SELECT TOP 1 id FROM catalogo.planilla_template),
                    (SELECT TOP 1 id FROM catalogo.item_planificacion),
                    (SELECT TOP 1 id FROM catalogo.cuenta_planificacion),
                    (SELECT TOP 1 id FROM catalogo.plan_presupuestario),
                    'directa', 500,
                    (SELECT id FROM core.usuario WHERE username='mmednik'))
        """), {"s": sid})).scalar()
        await db.commit()

    # Envío a revisión
    r = await client.post(f"/planificacion/solicitudes/{sid}/transicion",
                          json={"accion": "enviar_a_revision_vp"}, headers=H(tok))
    assert r.status_code == 200

    # Ahora PATCH a la línea debe fallar
    r = await client.patch(
        f"/planificacion/lineas/{lid}",
        headers=H(tok),
        json={"monto_solicitado": "999.00"},
    )
    assert r.status_code == 409, r.text
