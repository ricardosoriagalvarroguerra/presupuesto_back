"""Tests E2E de las 6 features de comparación/trazabilidad de ajustes.

Cubre:
  - GET /lineas/{lid}/historia          (Timeline por línea)
  - GET /solicitudes/{sid}/diff         (Diff entre snapshots)
  - POST /observaciones/{oid}/respuestas (Hilo de respuestas)

Setup común: crea un escenario reproducible con (ciclo 2099, solicitud VPF,
1 línea editada, 1 snapshot, 1 observación) y lo limpia al final del módulo.
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text

from .conftest import _login


def H(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


# ============================================================
# Fixture: escenario completo con línea + snapshot + observación
# ============================================================

@pytest_asyncio.fixture(scope="module", autouse=True)
async def _asegurar_tabla_respuesta():
    """Garantiza que `planificacion.observacion_respuesta` existe.

    Permite que los tests corran antes de que el operador haya aplicado la
    migración Alembic 002. Es exactamente el mismo DDL que en
    `alembic/versions/002_observacion_respuesta.py`.
    """
    from app.db import SessionLocal
    async with SessionLocal() as db:
        await db.execute(text("""
            IF NOT EXISTS (SELECT 1 FROM sys.tables t
                            JOIN sys.schemas s ON s.schema_id = t.schema_id
                            WHERE s.name = 'planificacion' AND t.name = 'observacion_respuesta')
            BEGIN
                CREATE TABLE planificacion.observacion_respuesta (
                    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
                    observacion_id  BIGINT NOT NULL,
                    autor_id        BIGINT NULL,
                    texto           NVARCHAR(MAX) NOT NULL,
                    created_at      DATETIMEOFFSET NOT NULL CONSTRAINT df_obs_resp_created_at DEFAULT SYSDATETIMEOFFSET(),
                    CONSTRAINT fk_obs_resp_observacion
                        FOREIGN KEY (observacion_id)
                        REFERENCES planificacion.observacion(id)
                        ON DELETE CASCADE,
                    CONSTRAINT fk_obs_resp_autor
                        FOREIGN KEY (autor_id)
                        REFERENCES core.usuario(id)
                        ON DELETE SET NULL
                );
                CREATE INDEX ix_obs_resp_observacion
                    ON planificacion.observacion_respuesta(observacion_id, created_at);
            END
        """))
        await db.commit()
    yield


@pytest_asyncio.fixture
async def escenario_ajustes():
    """Crea una solicitud con 1 línea, 1 snapshot previo, 1 observación
    abierta sobre la línea. Devuelve los IDs útiles. Limpia al terminar."""
    from app.db import SessionLocal
    async with SessionLocal() as db:
        # Limpieza previa idempotente
        await db.execute(text("""
            DELETE FROM planificacion.observacion_respuesta
             WHERE observacion_id IN (
               SELECT o.id FROM planificacion.observacion o
               JOIN planificacion.solicitud s ON s.id=o.solicitud_id
               JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id
               WHERE cp.anio=2099)
        """))
        await db.execute(text("""
            DELETE FROM planificacion.evento_solicitud WHERE solicitud_id IN (SELECT s.id FROM planificacion.solicitud s JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id WHERE cp.anio=2099);
            DELETE FROM planificacion.observacion       WHERE solicitud_id IN (SELECT s.id FROM planificacion.solicitud s JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id WHERE cp.anio=2099);
            DELETE FROM planificacion.snapshot_linea    WHERE snapshot_id IN (SELECT ss.id FROM planificacion.snapshot_solicitud ss JOIN planificacion.solicitud s ON s.id=ss.solicitud_id JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id WHERE cp.anio=2099);
            DELETE FROM planificacion.snapshot_solicitud WHERE solicitud_id IN (SELECT s.id FROM planificacion.solicitud s JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id WHERE cp.anio=2099);
            DELETE FROM planificacion.linea_solicitud   WHERE solicitud_id IN (SELECT s.id FROM planificacion.solicitud s JOIN core.ciclo_presupuestario cp ON cp.id=s.ciclo_id WHERE cp.anio=2099);
            DELETE FROM planificacion.solicitud         WHERE ciclo_id IN (SELECT id FROM core.ciclo_presupuestario WHERE anio=2099)
        """))
        await db.execute(text("DELETE FROM core.ciclo_presupuestario WHERE anio=2099"))

        # Ciclo + solicitud
        await db.execute(text("""
            INSERT INTO core.ciclo_presupuestario (anio, nombre, estado, created_by)
            VALUES (2099, 'Ciclo 2099 (test ajustes)', 'planificacion',
                   (SELECT id FROM core.usuario WHERE username='mmednik'))
        """))
        cid = (await db.execute(text("SELECT id FROM core.ciclo_presupuestario WHERE anio=2099"))).scalar()
        uid = (await db.execute(text("SELECT id FROM core.usuario WHERE username='mmednik'"))).scalar()

        sid = (await db.execute(text("""
            INSERT INTO planificacion.solicitud
              (ciclo_id, vp_codigo, nombre, etapa_actual, estado_workflow, monto_total, created_by)
            OUTPUT INSERTED.id
            VALUES (:c, 'VPF', 'Test ajustes', 1, 'en_revision_presidencia', 12500, :u)
        """), {"c": cid, "u": uid})).scalar()

        # Línea: tomamos un item × cuenta que exista en relacion_item_cuenta.
        pair = (await db.execute(text("""
            SELECT TOP 1 ric.item_id, ric.cuenta_id,
                   (SELECT id FROM catalogo.planilla_template WHERE codigo='PL-MISIONES-SERV') AS pt_id,
                   (SELECT id FROM catalogo.plan_presupuestario WHERE codigo LIKE 'PRESUPDEGASTOS%') AS plan_id
              FROM catalogo.relacion_item_cuenta ric
              JOIN catalogo.item_planificacion i ON i.id=ric.item_id
              WHERE i.codigo LIKE '02.05.%'
        """))).mappings().first()
        assert pair, "No se encontró par item×cuenta para VPF en relacion_item_cuenta"

        lid = (await db.execute(text("""
            INSERT INTO planificacion.linea_solicitud
              (solicitud_id, planilla_template_id, item_id, cuenta_id, plan_id,
               modalidad, monto_solicitado, parametros, created_by)
            OUTPUT INSERTED.id
            VALUES (:s, :pt, :i, :c, :p, 'directa', 12500, '{}', :u)
        """), {"s": sid, "pt": pair["pt_id"], "i": pair["item_id"],
               "c": pair["cuenta_id"], "p": pair["plan_id"], "u": uid})).scalar()

        # Evento de creación (simulado para que historia/timeline tenga algo)
        # Pasamos el JSON como parámetro porque `:` dentro de strings literales
        # SQL es interpretado por SQLAlchemy como bind-name si no se escapa.
        await db.execute(text("""
            INSERT INTO planificacion.evento_solicitud
              (solicitud_id, linea_id, accion, payload, usuario_id, created_at)
            VALUES (:s, :l, 'agregar_linea',
                    :payload,
                    :u, DATEADD(MINUTE, -30, SYSDATETIMEOFFSET()))
        """), {"s": sid, "l": lid, "u": uid, "payload": '{"monto_solicitado":"12500"}'})

        # Snapshot del estado "devuelto con observaciones" (monto = 12500).
        # Es el motivo que sí está en el CHECK constraint y es semánticamente
        # el caso real: Presidencia devolvió y a partir de acá el VP edita.
        snap_id = (await db.execute(text("""
            INSERT INTO planificacion.snapshot_solicitud
              (solicitud_id, etapa, motivo, monto_total, created_by, created_at)
            OUTPUT INSERTED.id
            VALUES (:s, 2, 'devuelto_con_observaciones', 12500, :u,
                    DATEADD(MINUTE, -25, SYSDATETIMEOFFSET()))
        """), {"s": sid, "u": uid})).scalar()
        await db.execute(text("""
            INSERT INTO planificacion.snapshot_linea
              (snapshot_id, linea_id, item_codigo, cuenta_codigo, plan_codigo,
               parametros, monto_solicitado)
            VALUES (:sn, :l,
                    (SELECT codigo FROM catalogo.item_planificacion WHERE id=:i),
                    (SELECT codigo FROM catalogo.cuenta_planificacion WHERE id=:c),
                    'PRESUPDEGASTOS', '{}', 12500)
        """), {"sn": snap_id, "l": lid, "i": pair["item_id"], "c": pair["cuenta_id"]})

        # Observación abierta sobre la línea (Presidencia sugiere bajar a 10000)
        oid = (await db.execute(text("""
            INSERT INTO planificacion.observacion
              (solicitud_id, linea_id, alcance, texto, accion_sugerida,
               valor_sugerido, etapa_origen, created_by, created_at)
            OUTPUT INSERTED.id
            VALUES (:s, :l, 'linea', 'Bajar el monto solicitado', 'modificar_monto',
                    :vs,
                    2, :u, DATEADD(MINUTE, -20, SYSDATETIMEOFFSET()))
        """), {"s": sid, "l": lid, "u": uid,
               "vs": '{"monto_original":12500,"nuevo_monto":10000}'})).scalar()

        # Evento "modificar_linea" simulando que el VP bajó el monto a 10200
        await db.execute(text("UPDATE planificacion.linea_solicitud SET monto_solicitado = 10200 WHERE id=:l"), {"l": lid})
        await db.execute(text("""
            INSERT INTO planificacion.evento_solicitud
              (solicitud_id, linea_id, accion, payload, usuario_id, created_at)
            VALUES (:s, :l, 'modificar_linea',
                    :payload,
                    :u, DATEADD(MINUTE, -10, SYSDATETIMEOFFSET()))
        """), {"s": sid, "l": lid, "u": uid,
               "payload": '{"cambios":{"monto_solicitado":"10200"}}'})

        await db.commit()

    yield {"sid": sid, "lid": lid, "snap_id": snap_id, "oid": oid}

    # Cleanup
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM planificacion.observacion_respuesta WHERE observacion_id IN (SELECT id FROM planificacion.observacion WHERE solicitud_id=:s)"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.evento_solicitud WHERE solicitud_id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.observacion WHERE solicitud_id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.snapshot_linea WHERE snapshot_id IN (SELECT id FROM planificacion.snapshot_solicitud WHERE solicitud_id=:s)"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.snapshot_solicitud WHERE solicitud_id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.linea_solicitud WHERE solicitud_id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM planificacion.solicitud WHERE id=:s"), {"s": sid})
        await db.execute(text("DELETE FROM core.ciclo_presupuestario WHERE anio=2099"))
        await db.commit()


# ============================================================
# 1. Timeline por línea — GET /lineas/{lid}/historia
# ============================================================

@pytest.mark.asyncio
async def test_historia_linea_devuelve_eventos_ordenados(client, escenario_ajustes):
    tok = await _login(client, "mmednik", "Matias2026!")
    lid = escenario_ajustes["lid"]
    r = await client.get(f"/planificacion/lineas/{lid}/historia", headers=H(tok))
    assert r.status_code == 200, r.text
    items = r.json()

    # Debe contener: agregar_linea, observacion (kind=observacion), modificar_linea
    kinds = [it["kind"] for it in items]
    acciones = [it.get("accion") for it in items if it["kind"] == "evento"]
    assert "agregar_linea" in acciones, f"Falta agregar_linea en {acciones}"
    assert "modificar_linea" in acciones, f"Falta modificar_linea en {acciones}"
    assert "observacion" in kinds, f"Falta entry de observación en {kinds}"

    # Orden cronológico ascendente
    fechas = [it["created_at"] for it in items if it.get("created_at")]
    assert fechas == sorted(fechas), "Timeline no está ordenado por created_at ASC"

    # El payload del modificar_linea trae el nuevo monto
    mod = next((it for it in items if it["kind"] == "evento" and it["accion"] == "modificar_linea"), None)
    assert mod is not None
    assert mod["payload"]["cambios"]["monto_solicitado"] == "10200"


@pytest.mark.asyncio
async def test_historia_linea_404_si_no_existe(client):
    tok = await _login(client, "mmednik", "Matias2026!")
    r = await client.get("/planificacion/lineas/99999999/historia", headers=H(tok))
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_historia_linea_403_cross_vp(client, escenario_ajustes):
    """vmoreira (VPD) no puede ver la historia de una línea de VPF."""
    tok = await _login(client, "vmoreira", "Virginia2026!")
    r = await client.get(f"/planificacion/lineas/{escenario_ajustes['lid']}/historia", headers=H(tok))
    assert r.status_code == 403, r.text


# ============================================================
# 2. Diff entre snapshots — GET /solicitudes/{sid}/diff
# ============================================================

@pytest.mark.asyncio
async def test_diff_snapshot_vs_current(client, escenario_ajustes):
    tok = await _login(client, "mmednik", "Matias2026!")
    sid = escenario_ajustes["sid"]
    snap = escenario_ajustes["snap_id"]
    r = await client.get(f"/planificacion/solicitudes/{sid}/diff?from={snap}&to=current", headers=H(tok))
    assert r.status_code == 200, r.text
    diff = r.json()

    # El snapshot tenía 12500, el current tiene 10200 → 1 línea modificada con -2300
    assert diff["totales"]["n_modificadas"] == 1
    assert diff["totales"]["n_agregadas"] == 0
    assert diff["totales"]["n_eliminadas"] == 0
    assert abs(diff["totales"]["monto_antes"] - 12500) < 0.01
    assert abs(diff["totales"]["monto_ahora"] - 10200) < 0.01
    assert abs(diff["totales"]["delta"] - (-2300)) < 0.01

    # Detalle de la línea modificada
    assert len(diff["lineas"]) == 1
    fila = diff["lineas"][0]
    assert fila["tipo"] == "modificada"
    assert abs(fila["monto_antes"] - 12500) < 0.01
    assert abs(fila["monto_ahora"] - 10200) < 0.01


@pytest.mark.asyncio
async def test_diff_falla_sin_from(client, escenario_ajustes):
    tok = await _login(client, "mmednik", "Matias2026!")
    sid = escenario_ajustes["sid"]
    r = await client.get(f"/planificacion/solicitudes/{sid}/diff", headers=H(tok))
    assert r.status_code == 400, r.text
    assert "from" in r.text.lower()


@pytest.mark.asyncio
async def test_diff_snapshot_no_pertenece_a_solicitud(client, escenario_ajustes):
    """Pasar un snapshot_id que no pertenece a la solicitud devuelve 404."""
    tok = await _login(client, "mmednik", "Matias2026!")
    sid = escenario_ajustes["sid"]
    r = await client.get(f"/planificacion/solicitudes/{sid}/diff?from=99999999", headers=H(tok))
    assert r.status_code == 404, r.text


# ============================================================
# 3. Hilo de respuestas — POST/GET /observaciones/{oid}/respuestas
# ============================================================

@pytest.mark.asyncio
async def test_crear_y_listar_respuestas(client, escenario_ajustes):
    tok = await _login(client, "mmednik", "Matias2026!")
    oid = escenario_ajustes["oid"]

    # POST 1
    r = await client.post(
        f"/planificacion/observaciones/{oid}/respuestas",
        headers=H(tok),
        json={"texto": "No puedo bajar a 10k porque hay 2 misiones comprometidas."},
    )
    assert r.status_code == 200, r.text
    rid1 = r.json()["id"]
    assert rid1 > 0

    # POST 2
    r = await client.post(
        f"/planificacion/observaciones/{oid}/respuestas",
        headers=H(tok),
        json={"texto": "Igual ya bajé a 10.2k que es el máximo posible."},
    )
    assert r.status_code == 200, r.text

    # GET
    r = await client.get(f"/planificacion/observaciones/{oid}/respuestas", headers=H(tok))
    assert r.status_code == 200, r.text
    respuestas = r.json()
    assert len(respuestas) == 2
    # Orden ascendente por created_at
    assert respuestas[0]["texto"].startswith("No puedo bajar")
    assert respuestas[1]["texto"].startswith("Igual ya bajé")
    # Autor poblado
    assert respuestas[0]["autor"]["nombre"] is not None


@pytest.mark.asyncio
async def test_respuesta_404_si_obs_no_existe(client):
    tok = await _login(client, "mmednik", "Matias2026!")
    r = await client.post(
        "/planificacion/observaciones/99999999/respuestas",
        headers=H(tok),
        json={"texto": "test"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_respuesta_403_cross_vp(client, escenario_ajustes):
    """vmoreira (VPD) no puede responder en una observación de VPF."""
    tok = await _login(client, "vmoreira", "Virginia2026!")
    r = await client.post(
        f"/planificacion/observaciones/{escenario_ajustes['oid']}/respuestas",
        headers=H(tok),
        json={"texto": "no debería pasar"},
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_respuesta_texto_vacio_400(client, escenario_ajustes):
    tok = await _login(client, "mmednik", "Matias2026!")
    r = await client.post(
        f"/planificacion/observaciones/{escenario_ajustes['oid']}/respuestas",
        headers=H(tok),
        json={"texto": "   "},
    )
    # Pydantic min_length=1 antes del strip → puede dar 422; o si pasa, nuestro
    # check de vacío post-strip da 400. Ambos son válidos.
    assert r.status_code in (400, 422), r.text


# ============================================================
# 4. Integración: timeline incluye la respuesta recién creada
# ============================================================

@pytest.mark.asyncio
async def test_historia_incluye_respuesta_de_hilo(client, escenario_ajustes):
    tok = await _login(client, "mmednik", "Matias2026!")
    oid = escenario_ajustes["oid"]
    lid = escenario_ajustes["lid"]

    # Crear una respuesta en el hilo
    r = await client.post(
        f"/planificacion/observaciones/{oid}/respuestas",
        headers=H(tok),
        json={"texto": "Respuesta del hilo para timeline"},
    )
    assert r.status_code == 200, r.text

    # La historia debe incluirla con kind='respuesta'
    r = await client.get(f"/planificacion/lineas/{lid}/historia", headers=H(tok))
    items = r.json()
    respuestas = [it for it in items if it["kind"] == "respuesta"]
    assert len(respuestas) >= 1
    assert any("Respuesta del hilo para timeline" in it["texto"] for it in respuestas)
