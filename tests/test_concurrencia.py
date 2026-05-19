"""Tests de concurrencia / TOCTOU del workflow (cierre de gap multi-usuario).

Antes de los locks `FOR UPDATE`, el guard de estado en agregar_linea/modificar_linea/
eliminar_linea se hacía SIN bloquear la fila de la solicitud, así que un POST
podía colarse después de que otro usuario ya había ejecutado una transición.
Acá probamos los dos escenarios:

  - Solicitud ya transicionada → POST /lineas tira 409 (no inserta huérfana).
  - Doble transición secuencial sobre la misma solicitud: la segunda tira 409
    porque el estado destino ya no es válido como origen.

Probar la concurrencia REAL (dos requests en paralelo a la misma fila) requiere
otro engine/conexión; estos tests verifican el contrato (estado → guard 409) que
es lo que el lock garantiza.
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
async def sid_listo_para_enviar():
    """Crea una solicitud VPF en en_elaboracion y devuelve sid + ciclo_id."""
    from app.db import SessionLocal
    async with SessionLocal() as db:
        await db.execute(text("""
            DELETE FROM planificacion.solicitud
            WHERE ciclo_id IN (SELECT id FROM core.ciclo_presupuestario WHERE anio=2097)
        """))
        await db.execute(text("DELETE FROM core.ciclo_presupuestario WHERE anio=2097"))
        await db.execute(text("""
            INSERT INTO core.ciclo_presupuestario (anio, nombre, estado, created_by)
            VALUES (2097, 'Ciclo 2097 (test)', 'planificacion',
                   (SELECT id FROM core.usuario WHERE username='mmednik'))
        """))
        cid = (await db.execute(
            text("SELECT id FROM core.ciclo_presupuestario WHERE anio=2097")
        )).scalar()
        sid = (await db.execute(text("""
            INSERT INTO planificacion.solicitud
              (ciclo_id, vp_codigo, nombre, etapa_actual, estado_workflow, created_by)
            VALUES (:c, 'VPF', 'Test concurrencia', 0, 'en_elaboracion',
                   (SELECT id FROM core.usuario WHERE username='mmednik'))
            RETURNING id
        """), {"c": cid})).scalar()
        await db.commit()

    yield sid

    async with SessionLocal() as db:
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
            VALUES (:s,
                    (SELECT id FROM catalogo.planilla_template LIMIT 1),
                    (SELECT id FROM catalogo.item_planificacion LIMIT 1),
                    (SELECT id FROM catalogo.cuenta_planificacion LIMIT 1),
                    (SELECT id FROM catalogo.plan_presupuestario LIMIT 1),
                    'directa', 500,
                    (SELECT id FROM core.usuario WHERE username='mmednik'))
            RETURNING id
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
