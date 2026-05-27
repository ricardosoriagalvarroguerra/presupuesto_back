"""Dashboards del módulo Ejecución.

Por cada VP hay un "dashboard" con KPIs (aprobado, vigente, comprometido,
devengado, pagado), gráfico mensual, top cuentas, top proveedores y
comparativo interanual contra el ciclo anterior.

Definiciones operativas que se repiten en todas las queries:
  inicial         PRESUPLIBERACIONPLAN   (presupuesto aprobado original)
  modificaciones  AJUSTECREDITOINICIAL   (ajustes posteriores)
  vigente         inicial + modificaciones  (saldo presupuestal hoy)
  ejecutado       compromiso + devengado + pagado  (consume vigente)
  disponible      vigente − ejecutado

"aprobado" es alias de "inicial"; ambos nombres viven en el contrato del
frontend.

VPs especiales — TRANSV (Transversal): planes institucionales que no
pertenecen a ninguna VP (PRESUPBUSO de Capital, PREFONESP de Fondo de
Terminación de Personal). En distintos cortes K2B aparecen con
vp_codigo='TRANSVERSAL' o con NULL; el filtro acepta los dos formatos.
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.security import CurrentUser, get_current_user

# Dashboards de ejecución: visibles para cualquier usuario autenticado.
# Los endpoints por VP (/vp/{vp}/...) re-chequean scope para limitar a la
# propia VP de usuarios sin ver_todo.
router = APIRouter(
    prefix="/ejecucion",
    tags=["ejecucion"],
    dependencies=[Depends(get_current_user)],
)

# Mapeo VP corto → nombre largo (como vive en ejecucion.movimiento.vp_codigo)
_CODIGO_VP = {
    "GOB": "GOBERNANZA INSTITUCIONAL",
    "PRE": "PRESIDENCIA EJECUTIVA",
    "VPE": "VICEPRESIDENCIA EJECUTIVA",
    "VPD": "VICEPRESIDENCIA DE DESARROLLO ESTRATÉGICO",
    "VPO": "VICEPRESIDENCIA DE OPERACIONES Y PAÍSES",
    "VPF": "VICEPRESIDENCIA DE FINANZAS",
    # TRANSVERSAL agrupa movimientos institucionales sin VP propia:
    # planes PRESUPBUSO (Bienes de uso / Capital) y PREFONESP (Fondo terminación de personal).
    # En el corte 2026_03 figuran con vp_codigo='TRANSVERSAL'; en el 2026_05 con NULL.
    "TRANSV": "TRANSVERSAL",
}

# Filtros SQL para VP — TRANSV usa OR (NULL o 'TRANSVERSAL') para cubrir ambos formatos
def _filtro_vp_sql(vp_corto: str) -> tuple[str, dict]:
    """Devuelve (cláusula_where, dict_params_adicionales) para filtrar por VP."""
    if vp_corto.upper() == "TRANSV":
        return ("(m.vp_codigo = 'TRANSVERSAL' OR m.vp_codigo IS NULL)", {})
    nombre = _CODIGO_VP.get(vp_corto.upper())
    if not nombre:
        return ("1=0", {})
    return ("m.vp_codigo = :vp", {"vp": nombre})

# Antes (PG): c.path <@ CAST('n5.n2' AS ltree). En SQL Server `path` es NVARCHAR
# y emulamos descendencia con comparación + LIKE prefijo.
_CATEGORIAS_SQL = """
  CASE
    WHEN c.path = 'n5.n2' OR c.path LIKE 'n5.n2.%' THEN 'Salarios y Beneficios'
    WHEN c.path = 'n5.n3' OR c.path LIKE 'n5.n3.%' THEN 'Consultores'
    WHEN c.path = 'n5.n4' OR c.path LIKE 'n5.n4.%' THEN 'Misiones de Servicio'
    WHEN c.path = 'n5.n5' OR c.path LIKE 'n5.n5.%' THEN 'Reuniones Gobernanza'
    WHEN c.path = 'n5.n6' OR c.path LIKE 'n5.n6.%' THEN 'Gastos Operativos'
    ELSE 'Otros'
  END
"""


async def _resolver_snapshot(snapshot: str | None, db: AsyncSession) -> str:
    """Si snapshot es None, devuelve el más reciente disponible."""
    if snapshot:
        return snapshot
    # SQL Server: TOP 1 + ORDER BY (en lugar de LIMIT). NULLs en DESC vienen
    # primero por default; el CASE los relega al final.
    r = (await db.execute(text("""
        SELECT TOP 1 snapshot_label FROM ejecucion.movimiento
        GROUP BY snapshot_label
        ORDER BY CASE WHEN MAX(fecha_movimiento) IS NULL THEN 1 ELSE 0 END,
                 MAX(fecha_movimiento) DESC
    """))).scalar()
    return r or "corte_2026_03"


@router.get("/snapshots")
async def listar_snapshots(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    """Lista los snapshots/cortes de ejecución disponibles.

    El frontend usa esto para mostrar un selector de "corte" en la UI.
    """
    # SQL Server: STRING_AGG no acepta DISTINCT inline → deduplicamos con
    # subquery antes de agregar; ARRAY_AGG (PG) no existe.
    rows = (await db.execute(text("""
        SELECT snapshot_label AS label,
               COUNT(*) AS movimientos,
               CAST(MIN(fecha_movimiento) AS date) AS desde,
               CAST(MAX(fecha_movimiento) AS date) AS hasta,
               COUNT(DISTINCT ciclo_id) AS ciclos,
               STUFF((
                 SELECT ',' + CAST(anio AS varchar(4))
                 FROM (
                   SELECT DISTINCT YEAR(fecha_movimiento) AS anio
                   FROM ejecucion.movimiento mi
                   WHERE mi.snapshot_label = m.snapshot_label
                 ) y
                 ORDER BY anio
                 FOR XML PATH(''), TYPE
               ).value('.', 'NVARCHAR(MAX)'), 1, 1, '') AS anios
        FROM ejecucion.movimiento m
        GROUP BY snapshot_label
        ORDER BY MAX(fecha_movimiento) DESC
    """))).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        # `anios` viene como '2024,2025,2026' (string CSV). Convertimos a lista
        # de ints para que el frontend lo consuma igual que antes.
        if d.get("anios"):
            d["anios"] = [int(x) for x in d["anios"].split(",") if x]
        else:
            d["anios"] = []
        out.append(d)
    return out


@router.get("/comparativo")
async def comparativo_anual(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Resumen comparativo de ejecución por año.

    Devuelve totales, distribución por VP y top conceptos para todos los ciclos
    cargados en `ejecucion.movimiento`.
    """
    totales_sql = text("""
        SELECT
          cp.anio AS anio,
          cp.estado AS ciclo_estado,
          ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS aprobado,
          ROUND(SUM(CASE WHEN tm.categoria='modificacion' THEN m.monto_vigente ELSE 0 END), 0) AS modificaciones,
          ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS ejecutado,
          ROUND(100.0 * SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END) /
            NULLIF(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0), 1) AS pct_ejecucion,
          COUNT(m.id) AS movimientos
        FROM core.ciclo_presupuestario cp
        LEFT JOIN ejecucion.movimiento_dedup m ON m.ciclo_id = cp.id
        LEFT JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        GROUP BY cp.anio, cp.estado
        HAVING COUNT(m.id) > 0
        ORDER BY cp.anio
    """)

    por_vp_sql = text("""
        SELECT
          cp.anio AS anio,
          m.vp_codigo AS vp,
          ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS aprobado,
          ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS ejecutado
        FROM ejecucion.movimiento_dedup m
        JOIN core.ciclo_presupuestario cp ON cp.id = m.ciclo_id
        JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        WHERE m.vp_codigo IS NOT NULL
        GROUP BY cp.anio, m.vp_codigo
        ORDER BY cp.anio, aprobado DESC
    """)

    top_conceptos_sql = text("""
        SELECT
          cp.anio AS anio,
          c.descripcion AS concepto,
          ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS aprobado,
          ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS ejecutado
        FROM ejecucion.movimiento_dedup m
        JOIN core.ciclo_presupuestario cp ON cp.id = m.ciclo_id
        JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
        WHERE c.imputable = 1
        GROUP BY cp.anio, c.descripcion
        HAVING SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END) > 0
        ORDER BY cp.anio, aprobado DESC
    """)

    totales = (await db.execute(totales_sql)).mappings().all()
    por_vp = (await db.execute(por_vp_sql)).mappings().all()
    top = (await db.execute(top_conceptos_sql)).mappings().all()

    return {
        "totales_por_anio": [dict(r) for r in totales],
        "por_vp": [dict(r) for r in por_vp],
        "top_conceptos": [dict(r) for r in top],
    }


@router.get("/vp/{vp_codigo}/dashboard")
async def ejecucion_vp_dashboard(
    vp_codigo: str,
    ciclo_anio: int | None = Query(None, description="Año del ciclo. Si null, usa el último con datos."),
    snapshot: str | None = Query(None, description="Corte de ejecución. Si null, el más reciente."),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # Scope: si no tiene ver_todo ni planillas_extra, solo puede ver su propia VP.
    if not current.ver_todo and not current.planillas_extra and current.vp_codigo != vp_codigo.upper():
        raise HTTPException(403, f"Tu rol solo puede ver ejecución de {current.vp_codigo or '(ninguna VP)'}.")
    """Dashboard de ejecución específico de una VP (o GOB / PRE).

    Devuelve en una sola llamada todo lo que la página de ejecución necesita:
      - KPIs (aprobado, ejecutado, comprometido, % ejec, movimientos)
      - Evolución mensual (ejecutado)
      - Distribución por categoría (5.2 / 5.3 / 5.4 / 5.5 / 5.6)
      - Distribución por tipo de movimiento K2B (Compromiso / Devengado / Pagado / Reverso)
      - Top 10 cuentas con ejecución
      - Top 10 proveedores/personas
      - Comparativo con ciclo anterior (interanual)
    """
    vp_nombre = _CODIGO_VP.get(vp_codigo.upper())
    if not vp_nombre:
        raise HTTPException(404, f"VP {vp_codigo} no reconocida")

    snap = await _resolver_snapshot(snapshot, db)

    # Resolver ciclo dentro del snapshot
    if ciclo_anio:
        ciclo = (await db.execute(
            text("SELECT id, anio, estado FROM core.ciclo_presupuestario WHERE anio=:a"),
            {"a": ciclo_anio},
        )).mappings().first()
    else:
        ciclo = (await db.execute(text("""
            SELECT TOP 1 cp.id, cp.anio, cp.estado FROM core.ciclo_presupuestario cp
            WHERE EXISTS (SELECT 1 FROM ejecucion.movimiento_dedup m
                          WHERE m.ciclo_id = cp.id AND m.snapshot_label = :s)
            ORDER BY cp.anio DESC
        """), {"s": snap})).mappings().first()
    if not ciclo:
        raise HTTPException(404, "Ciclo no encontrado")
    ciclo_id = ciclo["id"]

    # Ciclo anterior (para Δ interanual)
    ciclo_ant = (await db.execute(
        text("""SELECT id, anio FROM core.ciclo_presupuestario
                WHERE anio = :a - 1 AND EXISTS (SELECT 1 FROM ejecucion.movimiento_dedup m
                                                WHERE m.ciclo_id = id AND m.snapshot_label = :s)"""),
        {"a": ciclo["anio"], "s": snap},
    )).mappings().first()

    params = {"cid": ciclo_id, "vp": vp_nombre, "snap": snap}

    # ───── KPIs ─────
    # Definiciones operativas DPP / K2B:
    #   inicial        = PRESUPLIBERACIONPLAN (aprobado original)
    #   modificaciones = AJUSTECREDITOINICIAL
    #   vigente        = inicial + modificaciones (saldo presupuestal hoy)
    #   ejecutado      = compromiso + devengado + pagado (consume vigente)
    #   disponible     = vigente − ejecutado
    # `aprobado` se mantiene como alias de `inicial` para compatibilidad con el frontend.
    kpis = dict((await db.execute(text("""
        SELECT
          ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS inicial,
          ROUND(SUM(CASE WHEN tm.categoria='modificacion' THEN m.monto_vigente ELSE 0 END), 0) AS modificaciones,
          ROUND(SUM(CASE WHEN tm.categoria IN ('inicial','modificacion') THEN m.monto_vigente ELSE 0 END), 0) AS vigente,
          ROUND(SUM(CASE WHEN tm.categoria='compromiso' THEN m.monto_comprometido ELSE 0 END), 0) AS comprometido,
          ROUND(SUM(CASE WHEN tm.categoria='devengado' THEN m.monto_ejecutado ELSE 0 END), 0) AS devengado,
          ROUND(SUM(CASE WHEN tm.categoria='pagado' THEN m.monto_pagado ELSE 0 END), 0) AS pagado,
          ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS ejecutado,
          ROUND(SUM(CASE WHEN tm.categoria IN ('inicial','modificacion') THEN m.monto_vigente ELSE 0 END)
              - SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS disponible,
          ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS aprobado,
          COUNT(*) AS movimientos
        FROM ejecucion.movimiento_dedup m
        JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        WHERE m.ciclo_id = :cid AND (m.vp_codigo = :vp OR (:vp = 'TRANSVERSAL' AND m.vp_codigo IS NULL)) AND m.snapshot_label = :snap
    """), params)).mappings().first() or {})

    # ───── Evolución mensual ─────
    evol = [dict(r) for r in (await db.execute(text("""
        SELECT MONTH(m.fecha_movimiento) AS mes,
               ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS ejecutado,
               COUNT(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN 1 END) AS movimientos
        FROM ejecucion.movimiento_dedup m
        JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        WHERE m.ciclo_id = :cid AND (m.vp_codigo = :vp OR (:vp = 'TRANSVERSAL' AND m.vp_codigo IS NULL)) AND m.snapshot_label = :snap
        GROUP BY MONTH(m.fecha_movimiento)
        ORDER BY mes
    """), params)).mappings().all()]

    # ───── Por categoría ─────
    por_categoria = [dict(r) for r in (await db.execute(text(f"""
        SELECT categoria,
               ROUND(SUM(aprobado), 0)  AS aprobado,
               ROUND(SUM(ejecutado), 0) AS ejecutado
        FROM (
          SELECT
            {_CATEGORIAS_SQL} AS categoria,
            CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END AS aprobado,
            CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END AS ejecutado
          FROM ejecucion.movimiento_dedup m
          JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
          LEFT JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
          WHERE m.ciclo_id = :cid AND (m.vp_codigo = :vp OR (:vp = 'TRANSVERSAL' AND m.vp_codigo IS NULL)) AND m.snapshot_label = :snap
        ) base
        GROUP BY categoria
        HAVING SUM(aprobado) + SUM(ejecutado) > 0
        ORDER BY CASE WHEN SUM(aprobado) IS NULL THEN 1 ELSE 0 END, SUM(aprobado) DESC
    """), params)).mappings().all()]

    # ───── Por tipo de movimiento ─────
    por_tipo_mov = [dict(r) for r in (await db.execute(text("""
        SELECT tm.categoria AS categoria,
               tm.nombre AS tipo,
               ROUND(SUM(
                 CASE
                   WHEN tm.categoria = 'compromiso' THEN m.monto_comprometido
                   WHEN tm.categoria = 'devengado'  THEN m.monto_ejecutado
                   WHEN tm.categoria = 'pagado'     THEN m.monto_pagado
                   WHEN tm.categoria = 'reverso'    THEN m.monto_ejecutado
                   ELSE 0
                 END), 0) AS monto,
               COUNT(*) AS movimientos
        FROM ejecucion.movimiento_dedup m
        JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        WHERE m.ciclo_id = :cid AND (m.vp_codigo = :vp OR (:vp = 'TRANSVERSAL' AND m.vp_codigo IS NULL)) AND m.snapshot_label = :snap
          AND tm.categoria IN ('compromiso','devengado','pagado','reverso')
        GROUP BY tm.categoria, tm.nombre
        ORDER BY tm.categoria, monto DESC
    """), params)).mappings().all()]

    # ───── Top cuentas (con datos para drilldown) ─────
    top_cuentas = [dict(r) for r in (await db.execute(text("""
        SELECT TOP 10 c.codigo AS cuenta_codigo,
               c.descripcion AS cuenta_descripcion,
               ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS aprobado,
               ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS ejecutado,
               COUNT(*) AS movimientos
        FROM ejecucion.movimiento_dedup m
        JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
        WHERE m.ciclo_id = :cid AND (m.vp_codigo = :vp OR (:vp = 'TRANSVERSAL' AND m.vp_codigo IS NULL)) AND m.snapshot_label = :snap AND c.imputable = 1
        GROUP BY c.codigo, c.descripcion
        HAVING SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END) > 0
        ORDER BY ejecutado DESC
    """), params)).mappings().all()]

    # ───── Top proveedores / personas ─────
    top_personas = [dict(r) for r in (await db.execute(text("""
        SELECT TOP 10 m.persona,
               ROUND(SUM(m.monto_ejecutado), 0) AS monto,
               COUNT(*) AS operaciones
        FROM ejecucion.movimiento_dedup m
        JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        WHERE m.ciclo_id = :cid AND (m.vp_codigo = :vp OR (:vp = 'TRANSVERSAL' AND m.vp_codigo IS NULL)) AND m.snapshot_label = :snap
          AND m.persona IS NOT NULL AND m.persona <> ''
          AND tm.categoria NOT IN ('inicial','modificacion')
        GROUP BY m.persona
        ORDER BY CASE WHEN SUM(m.monto_ejecutado) IS NULL THEN 1 ELSE 0 END, monto DESC
    """), params)).mappings().all()]

    # ───── Comparativo interanual (KPI total) ─────
    interanual = None
    if ciclo_ant:
        prev = dict((await db.execute(text("""
            SELECT
              ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS aprobado,
              ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS ejecutado
            FROM ejecucion.movimiento_dedup m
            JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
            WHERE m.ciclo_id = :cid AND (m.vp_codigo = :vp OR (:vp = 'TRANSVERSAL' AND m.vp_codigo IS NULL)) AND m.snapshot_label = :snap
        """), {"cid": ciclo_ant["id"], "vp": vp_nombre, "snap": snap})).mappings().first() or {})
        interanual = {
            "anio": ciclo_ant["anio"],
            "aprobado": prev.get("aprobado"),
            "ejecutado": prev.get("ejecutado"),
        }

    return {
        "vp_codigo": vp_codigo.upper(),
        "vp_nombre": vp_nombre,
        "anio": ciclo["anio"],
        "ciclo_estado": ciclo["estado"],
        "snapshot": snap,
        "kpis": kpis,
        "interanual": interanual,
        "evolucion_mensual": evol,
        "por_categoria": por_categoria,
        "por_tipo_movimiento": por_tipo_mov,
        "top_cuentas": top_cuentas,
        "top_personas": top_personas,
    }


@router.get("/vp/{vp_codigo}/desglose")
async def ejecucion_vp_desglose(
    vp_codigo: str,
    ciclo_anio: int | None = Query(None),
    snapshot: str | None = Query(None),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not current.ver_todo and not current.planillas_extra and current.vp_codigo != vp_codigo.upper():
        raise HTTPException(403, f"Tu rol solo puede ver ejecución de {current.vp_codigo or '(ninguna VP)'}.")
    """Desglose jerárquico para una VP: Item × Cuenta N2 × Cuenta N3 × Cuenta imputable.

    Devuelve filas planas con todos los códigos/descripciones y los montos
    (vigente, ejecutado). El frontend agrupa y calcula subtotales.
    """
    vp_nombre = _CODIGO_VP.get(vp_codigo.upper())
    if not vp_nombre:
        raise HTTPException(404, f"VP {vp_codigo} no reconocida")

    snap = await _resolver_snapshot(snapshot, db)
    if ciclo_anio:
        ciclo = (await db.execute(
            text("SELECT id, anio FROM core.ciclo_presupuestario WHERE anio=:a"),
            {"a": ciclo_anio},
        )).mappings().first()
    else:
        ciclo = (await db.execute(text("""
            SELECT TOP 1 cp.id, cp.anio FROM core.ciclo_presupuestario cp
            WHERE EXISTS (SELECT 1 FROM ejecucion.movimiento_dedup m
                          WHERE m.ciclo_id = cp.id AND m.snapshot_label = :s)
            ORDER BY cp.anio DESC
        """), {"s": snap})).mappings().first()
    if not ciclo:
        raise HTTPException(404, "Ciclo no encontrado")

    # Para cada (item, cuenta imputable) → monto vigente y monto ejecutado.
    # Los niveles 2 y 3 se derivan vía path (antes ltree, ahora NVARCHAR).
    sql = text("""
        SELECT
          i.codigo AS item_codigo,
          i.descripcion AS item_descripcion,
          c2.codigo AS cuenta_n2_codigo,
          c2.descripcion AS cuenta_n2,
          c3.codigo AS cuenta_n3_codigo,
          c3.descripcion AS cuenta_n3,
          c.codigo AS cuenta_codigo,
          c.descripcion AS cuenta_imputable,
          ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 2) AS vigente,
          ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 2) AS ejecutado
        FROM ejecucion.movimiento_dedup m
        JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        JOIN catalogo.item_planificacion i ON i.id = m.item_id
        JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
        LEFT JOIN catalogo.cuenta_planificacion c2
               ON c2.nivel = 2 AND (c.path = c2.path OR c.path LIKE c2.path + '.%')
        LEFT JOIN catalogo.cuenta_planificacion c3
               ON c3.nivel = 3 AND (c.path = c3.path OR c.path LIKE c3.path + '.%')
        WHERE m.ciclo_id = :cid AND (m.vp_codigo = :vp OR (:vp = 'TRANSVERSAL' AND m.vp_codigo IS NULL)) AND m.snapshot_label = :snap
          AND c.imputable = 1
        GROUP BY i.codigo, i.descripcion, c2.codigo, c2.descripcion,
                 c3.codigo, c3.descripcion, c.codigo, c.descripcion
        HAVING SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END) <> 0
            OR SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END) <> 0
        ORDER BY i.codigo, c2.codigo, c3.codigo, c.codigo
    """)
    rows = (await db.execute(sql, {"cid": ciclo["id"], "vp": vp_nombre, "snap": snap})).mappings().all()

    return {
        "vp_codigo": vp_codigo.upper(),
        "vp_nombre": vp_nombre,
        "anio": ciclo["anio"],
        "snapshot": snap,
        "filas": [dict(r) for r in rows],
    }
