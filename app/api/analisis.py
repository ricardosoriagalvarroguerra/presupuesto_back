"""Cuadros DPP — el módulo Análisis.

9 cuadros oficiales que se generan en vivo desde la BDR:
  Cuadro 4 (Gastos Admin), 6 (Gobernanza), 8 (Misiones), 9 (Consultores),
  10 (Personal), 12 (Operativos), 14/15 (TI), 16 (Ejecución).

Datos: salen de `ejecucion.movimiento` vía la vista `ejecucion.movimiento_dedup`.
Las planillas de carga solo afectan la entrada (planificación); para
reportería trabajamos sobre los movimientos K2B.

Semántica de las categorías de tipo de movimiento (catalogo.tipo_movimiento):
  - inicial         → presupuesto liberado (PRESUPLIBERACIONPLAN)
  - modificacion    → ajuste de crédito inicial (AJUSTECREDITOINICIAL)
  - compromiso      → reserva contra ejecución
  - devengado       → gasto reconocido
  - pagado          → desembolsado
  - reverso         → anulación (se resta del ejecutado)
  - especial        → casos puntuales no-presupuestales

Fórmulas universales:
  APROBADO  = SUM(monto_vigente) WHERE categoria='inicial'
  VIGENTE   = APROBADO + SUM(monto_vigente) WHERE categoria='modificacion'
  EJECUTADO = SUM(monto_ejecutado) WHERE categoria NOT IN ('inicial','modificacion')
              (la categoria 'reverso' ya viene con signo negativo en m.signo)

Dedup: `ejecucion.movimiento_dedup` filtra al snapshot más reciente por año.
El mismo movimiento K2B puede aparecer en varios cortes (ej. `corte_2026_03`
y `corte_2026_05`); sin dedup los SUM se duplicarían.
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.security import get_current_user

# Dashboards institucionales — cualquier usuario autenticado puede verlos.
# NO se filtra por VP: los cuadros DPP son consolidados (toda la organización),
# no se desglosan por scope del usuario. Un cargador de VPE que entre al
# cuadro 10 (Personal) ve datos de toda la institución, no solo de su VP.
# Si en algún momento hace falta filtrar por rol, el guard va acá a nivel
# router.
router = APIRouter(
    prefix="/analisis",
    tags=["analisis"],
    dependencies=[Depends(get_current_user)],
)


# Macros SQL reusables — strings con CASE que se inyectan con f-strings para
# no repetir el mismo CASE 30 veces. Son constantes del código (no llegan
# datos del cliente) así que el uso de f-string no abre inyección SQL.
SQL_APROBADO = """
  COALESCE(SUM(CASE WHEN tm.categoria = 'inicial' THEN m.monto_vigente ELSE 0 END), 0)
"""
SQL_EJECUTADO = """
  COALESCE(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0)
"""

# Orden canónico de las VPs en los cuadros: GOB, PRE, VPE, VPD, VPO, VPF.
# Este orden es el que esperan los informes y los frontends que arman tablas.
VP_ALIAS_SQL = """CASE m.vp_codigo
  WHEN 'GOBERNANZA INSTITUCIONAL' THEN 'GOB'
  WHEN 'PRESIDENCIA EJECUTIVA' THEN 'PRE'
  WHEN 'VICEPRESIDENCIA EJECUTIVA' THEN 'VPE'
  WHEN 'VICEPRESIDENCIA DE DESARROLLO ESTRATÉGICO' THEN 'VPD'
  WHEN 'VICEPRESIDENCIA DE OPERACIONES Y PAÍSES' THEN 'VPO'
  WHEN 'VICEPRESIDENCIA DE FINANZAS' THEN 'VPF'
END"""

VP_ORDER_SQL = """CASE m.vp_codigo
  WHEN 'GOBERNANZA INSTITUCIONAL' THEN 1
  WHEN 'PRESIDENCIA EJECUTIVA' THEN 2
  WHEN 'VICEPRESIDENCIA EJECUTIVA' THEN 3
  WHEN 'VICEPRESIDENCIA DE DESARROLLO ESTRATÉGICO' THEN 4
  WHEN 'VICEPRESIDENCIA DE OPERACIONES Y PAÍSES' THEN 5
  WHEN 'VICEPRESIDENCIA DE FINANZAS' THEN 6
END"""

# Filtra movimientos que tienen VP "asignable" — los marcados TRANSVERSAL son
# gastos institucionales que no pertenecen a ninguna VP en particular
# (Capital, Fondo de Terminación de Personal, etc.). Los cuadros por VP los
# excluyen para no mostrar un bloque adicional "TRANSVERSAL" que confunde
# el lectura.
VP_FILTRO_SQL = "m.vp_codigo IS NOT NULL AND m.vp_codigo != 'TRANSVERSAL'"

# Originalmente este string traía un JOIN para deduplicar al último snapshot
# por año. La dedup ahora vive en la vista `ejecucion.movimiento_dedup`, así
# que quedó vacío. Lo dejo como placeholder por si vuelve a hacer falta un
# JOIN ad-hoc desde acá.
JOIN_ULTIMO_SNAPSHOT = """"""


# ────────────────────────────────────────────────────────────────────────────
# Definición de los 9 cuadros DPP.
# ────────────────────────────────────────────────────────────────────────────
#
# Cada entrada es una "ficha" del cuadro: título, descripción, columnas,
# fuente, y SQL completa. El endpoint genérico /analisis/cuadros/{codigo}
# ejecuta el SQL y devuelve un dict simple (cabecera + filas) que el
# frontend pinta como tabla.
#
# La SQL de cada cuadro suele tener la misma estructura:
#   1. CTE `base` con un SELECT plano filtrando movimientos del año + plan.
#   2. CTE por año (2025, 2026) agregando por VP / cuenta / item.
#   3. CTE `combinado` con UNION ALL de filas por VP + totales + variaciones.
#   4. SELECT final ordenando por la columna `_order`.
#
# La estructura está intencionalmente duplicada entre cuadros. Cada cuadro
# tiene su propia variación de columnas y agregados, y compartir CTEs hacía
# que un cambio puntual en un cuadro arrastrara cambios en otros.
CUADROS: dict[str, dict[str, Any]] = {
    "cuadro-4-presupuesto-gastos-admin": {
        "titulo": "Cuadro 4 — Presupuesto de Gastos Administrativos 2026",
        "descripcion": "VP × Reuniones / Misiones / Servicios Profesionales / Gastos en Personal / Gastos Operativos + Total (aprobado 2026), con totales institucionales 2025/2026 y variación.",
        "fuente": "planificacion",
        "columnas": ["vp", "reuniones", "misiones", "servicios_prof", "gastos_personal", "gastos_operativos", "total"],
        # Cuadro 4: matriz VP × categoría de gasto, con totales y variación 2026 vs 2025.
        # Las 5 categorías salen del path de cuenta (5.5 reuniones, 5.4 misiones, etc.) —
        # antes con operador ltree de PG; en MSSQL emulamos con LIKE prefijo.
        "sql": f"""
            WITH base AS (
              SELECT
                {VP_ALIAS_SQL} AS vp,
                {VP_ORDER_SQL} AS vp_orden,
                YEAR(m.fecha_movimiento) AS anio,
                CASE
                  WHEN (c.path = 'n5.n5' OR c.path LIKE 'n5.n5.%') THEN 'reuniones'
                  WHEN (c.path = 'n5.n4' OR c.path LIKE 'n5.n4.%') THEN 'misiones'
                  WHEN (c.path = 'n5.n3' OR c.path LIKE 'n5.n3.%') THEN 'servicios_prof'
                  WHEN (c.path = 'n5.n2' OR c.path LIKE 'n5.n2.%') THEN 'gastos_personal'
                  WHEN (c.path = 'n5.n6' OR c.path LIKE 'n5.n6.%') THEN 'gastos_operativos'
                  ELSE 'otros'
                END AS cat,
                CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END AS monto
              FROM ejecucion.movimiento_dedup m
              JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
              JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
              JOIN catalogo.plan_presupuestario p ON p.id = m.plan_id
              WHERE YEAR(m.fecha_movimiento) IN (2025, 2026)
                AND p.codigo = 'PRESUPDEGASTOS'
                AND {VP_FILTRO_SQL}
            ),
            por_vp_2026 AS (
              SELECT
                vp, vp_orden,
                SUM(CASE WHEN cat='reuniones'         AND anio=2026 THEN monto ELSE 0 END) AS reuniones,
                SUM(CASE WHEN cat='misiones'          AND anio=2026 THEN monto ELSE 0 END) AS misiones,
                SUM(CASE WHEN cat='servicios_prof'    AND anio=2026 THEN monto ELSE 0 END) AS servicios_prof,
                SUM(CASE WHEN cat='gastos_personal'   AND anio=2026 THEN monto ELSE 0 END) AS gastos_personal,
                SUM(CASE WHEN cat='gastos_operativos' AND anio=2026 THEN monto ELSE 0 END) AS gastos_operativos,
                SUM(CASE WHEN anio=2026 THEN monto ELSE 0 END) AS total
              FROM base GROUP BY vp, vp_orden
            ),
            totales AS (
              SELECT
                anio,
                SUM(CASE WHEN cat='reuniones' THEN monto ELSE 0 END)         AS reuniones,
                SUM(CASE WHEN cat='misiones' THEN monto ELSE 0 END)          AS misiones,
                SUM(CASE WHEN cat='servicios_prof' THEN monto ELSE 0 END)    AS servicios_prof,
                SUM(CASE WHEN cat='gastos_personal' THEN monto ELSE 0 END)   AS gastos_personal,
                SUM(CASE WHEN cat='gastos_operativos' THEN monto ELSE 0 END) AS gastos_operativos,
                SUM(monto) AS total
              FROM base GROUP BY anio
            ),
            t26 AS (SELECT * FROM totales WHERE anio=2026),
            t25 AS (SELECT * FROM totales WHERE anio=2025),
            combinado AS (
              -- Filas por VP (2026)
              SELECT vp, vp_orden, reuniones, misiones, servicios_prof,
                     gastos_personal, gastos_operativos, total
              FROM por_vp_2026
              UNION ALL
              SELECT 'Total Aprobado 2026', 7,
                     reuniones, misiones, servicios_prof, gastos_personal, gastos_operativos, total
              FROM t26
              UNION ALL
              SELECT 'Total Aprobado 2025', 8,
                     reuniones, misiones, servicios_prof, gastos_personal, gastos_operativos, total
              FROM t25
              UNION ALL
              SELECT 'Variación $', 9,
                     t26.reuniones - t25.reuniones,
                     t26.misiones - t25.misiones,
                     t26.servicios_prof - t25.servicios_prof,
                     t26.gastos_personal - t25.gastos_personal,
                     t26.gastos_operativos - t25.gastos_operativos,
                     t26.total - t25.total
              FROM t26 CROSS JOIN t25
              UNION ALL
              SELECT 'Variación %', 10,
                     100.0 * (t26.reuniones - t25.reuniones)         / NULLIF(t25.reuniones, 0),
                     100.0 * (t26.misiones - t25.misiones)           / NULLIF(t25.misiones, 0),
                     100.0 * (t26.servicios_prof - t25.servicios_prof) / NULLIF(t25.servicios_prof, 0),
                     100.0 * (t26.gastos_personal - t25.gastos_personal) / NULLIF(t25.gastos_personal, 0),
                     100.0 * (t26.gastos_operativos - t25.gastos_operativos) / NULLIF(t25.gastos_operativos, 0),
                     100.0 * (t26.total - t25.total)                 / NULLIF(t25.total, 0)
              FROM t26 CROSS JOIN t25
            )
            SELECT vp,
              ROUND(reuniones,         CASE WHEN vp_orden=10 THEN 1 ELSE 0 END) AS reuniones,
              ROUND(misiones,          CASE WHEN vp_orden=10 THEN 1 ELSE 0 END) AS misiones,
              ROUND(servicios_prof,    CASE WHEN vp_orden=10 THEN 1 ELSE 0 END) AS servicios_prof,
              ROUND(gastos_personal,   CASE WHEN vp_orden=10 THEN 1 ELSE 0 END) AS gastos_personal,
              ROUND(gastos_operativos, CASE WHEN vp_orden=10 THEN 1 ELSE 0 END) AS gastos_operativos,
              ROUND(total,             CASE WHEN vp_orden=10 THEN 1 ELSE 0 END) AS total
            FROM combinado
            ORDER BY vp_orden
        """,
    },
    "cuadro-6-gobernanza-institucional": {
        "titulo": "Cuadro 6 — Gobernanza Institucional",
        "descripcion": "Áreas de Gobernanza Institucional × monto aprobado 2026 (Reuniones, Auditoría, Evaluación, Ética, Ombuds).",
        "fuente": "planificacion",
        "columnas": ["area", "monto_aprobado"],
        "sql": """
            SELECT
              COALESCE(m.area, '(sin área)') AS area,
              ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS monto_aprobado
            FROM ejecucion.movimiento_dedup m
            JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
            WHERE m.vp_codigo = 'GOBERNANZA INSTITUCIONAL'
              AND YEAR(m.fecha_movimiento) = 2026
              AND m.area IS NOT NULL AND m.area NOT IN ('', 'NaN', 'nan')
            GROUP BY m.area
            HAVING SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END) > 0
            ORDER BY monto_aprobado DESC
        """,
    },
    "cuadro-8-misiones-comparativo": {
        "titulo": "Cuadro 8 — Misiones de Servicio 2026 vs 2025",
        "descripcion": "VP × Pasajes / Hospedaje / Viáticos / Otros (cuenta 5.4.1.*) — total 2026 vs 2025 con variación absoluta y %.",
        "fuente": "planificacion",
        "columnas": ["vp", "pasajes", "hospedaje", "viaticos", "otros", "total_2026", "total_2025", "variacion", "variacion_pct"],
        "sql": f"""
            WITH base AS (
              SELECT
                {VP_ALIAS_SQL} AS vp,
                {VP_ORDER_SQL} AS vp_orden,
                YEAR(m.fecha_movimiento) AS anio,
                c.codigo AS ccod,
                CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END AS monto
              FROM ejecucion.movimiento_dedup m
              JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
              JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
              JOIN catalogo.plan_presupuestario p ON p.id = m.plan_id
              WHERE (c.path = 'n5.n4.n1' OR c.path LIKE 'n5.n4.n1.%')
                AND p.codigo = 'PRESUPDEGASTOS'
                AND {VP_FILTRO_SQL}
                AND YEAR(m.fecha_movimiento) IN (2025, 2026)
            ),
            agg AS (
              SELECT vp, vp_orden,
                SUM(CASE WHEN anio=2026 AND ccod='5.4.1.01' THEN monto ELSE 0 END) AS pasajes,
                SUM(CASE WHEN anio=2026 AND ccod='5.4.1.03' THEN monto ELSE 0 END) AS hospedaje,
                SUM(CASE WHEN anio=2026 AND ccod='5.4.1.02' THEN monto ELSE 0 END) AS viaticos,
                SUM(CASE WHEN anio=2026 AND ccod NOT IN ('5.4.1.01','5.4.1.02','5.4.1.03') THEN monto ELSE 0 END) AS otros,
                SUM(CASE WHEN anio=2026 THEN monto ELSE 0 END) AS total_2026,
                SUM(CASE WHEN anio=2025 THEN monto ELSE 0 END) AS total_2025
              FROM base GROUP BY vp, vp_orden
            )
            SELECT
              vp,
              ROUND(pasajes, 0) AS pasajes,
              ROUND(hospedaje, 0) AS hospedaje,
              ROUND(viaticos, 0) AS viaticos,
              ROUND(otros, 0) AS otros,
              ROUND(total_2026, 0) AS total_2026,
              ROUND(total_2025, 0) AS total_2025,
              ROUND((total_2026 - total_2025), 0) AS variacion,
              ROUND((100.0 * (total_2026 - total_2025) / NULLIF(total_2025, 0)), 1) AS variacion_pct
            FROM agg
            ORDER BY vp_orden
        """,
    },
    "cuadro-9-servicios-profesionales-comparativo": {
        "titulo": "Cuadro 9 — Servicios Profesionales a Término 2026 vs 2025",
        "descripcion": "VP × monto aprobado 2025 vs 2026 (cuenta 5.3.*) con variación absoluta y porcentual.",
        "fuente": "planificacion",
        "columnas": ["vp", "monto_2025", "monto_2026", "variacion", "variacion_pct"],
        "sql": f"""
            WITH base AS (
              SELECT
                {VP_ALIAS_SQL} AS vp,
                {VP_ORDER_SQL} AS vp_orden,
                YEAR(m.fecha_movimiento) AS anio,
                CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END AS monto
              FROM ejecucion.movimiento_dedup m
              JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
              JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
              JOIN catalogo.plan_presupuestario p ON p.id = m.plan_id
              WHERE (c.path = 'n5.n3' OR c.path LIKE 'n5.n3.%')
                AND p.codigo = 'PRESUPDEGASTOS'
                AND {VP_FILTRO_SQL}
                AND YEAR(m.fecha_movimiento) IN (2025, 2026)
            ),
            agg AS (
              SELECT vp, vp_orden,
                SUM(CASE WHEN anio=2025 THEN monto ELSE 0 END) AS m25,
                SUM(CASE WHEN anio=2026 THEN monto ELSE 0 END) AS m26
              FROM base GROUP BY vp, vp_orden
            )
            SELECT
              vp,
              ROUND(m25, 0) AS monto_2025,
              ROUND(m26, 0) AS monto_2026,
              ROUND((m26 - m25), 0) AS variacion,
              ROUND((100.0 * (m26 - m25) / NULLIF(m25, 0)), 1) AS variacion_pct
            FROM agg
            ORDER BY vp_orden
        """,
    },
    "cuadro-10-personal-comparativo": {
        "titulo": "Cuadro 10 — Gastos en Personal 2026 vs 2025",
        "descripcion": "VP × Salarios (5.2.1.01) + Beneficios (5.2.1.02) — comparativo 2025 vs 2026. No incluye headcount (no en BDR).",
        "fuente": "planificacion",
        "columnas": ["vp", "salarios_2025", "beneficios_2025", "salarios_2026", "beneficios_2026", "total_2026", "total_2025", "variacion", "variacion_pct"],
        "sql": f"""
            WITH base AS (
              SELECT
                {VP_ALIAS_SQL} AS vp,
                {VP_ORDER_SQL} AS vp_orden,
                YEAR(m.fecha_movimiento) AS anio,
                c.codigo AS ccod,
                CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END AS monto
              FROM ejecucion.movimiento_dedup m
              JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
              JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
              JOIN catalogo.plan_presupuestario p ON p.id = m.plan_id
              WHERE (c.path = 'n5.n2' OR c.path LIKE 'n5.n2.%')
                AND p.codigo = 'PRESUPDEGASTOS'
                AND {VP_FILTRO_SQL}
                AND YEAR(m.fecha_movimiento) IN (2025, 2026)
            ),
            agg AS (
              SELECT vp, vp_orden,
                SUM(CASE WHEN anio=2025 AND ccod='5.2.1.01' THEN monto ELSE 0 END) AS sal25,
                SUM(CASE WHEN anio=2025 AND ccod='5.2.1.02' THEN monto ELSE 0 END) AS ben25,
                SUM(CASE WHEN anio=2026 AND ccod='5.2.1.01' THEN monto ELSE 0 END) AS sal26,
                SUM(CASE WHEN anio=2026 AND ccod='5.2.1.02' THEN monto ELSE 0 END) AS ben26
              FROM base GROUP BY vp, vp_orden
            )
            SELECT
              vp,
              ROUND(sal25, 0) AS salarios_2025,
              ROUND(ben25, 0) AS beneficios_2025,
              ROUND(sal26, 0) AS salarios_2026,
              ROUND(ben26, 0) AS beneficios_2026,
              ROUND((sal26 + ben26), 0) AS total_2026,
              ROUND((sal25 + ben25), 0) AS total_2025,
              ROUND(((sal26 + ben26) - (sal25 + ben25)), 0) AS variacion,
              ROUND((100.0 * ((sal26 + ben26) - (sal25 + ben25)) / NULLIF(sal25 + ben25, 0)), 1) AS variacion_pct
            FROM agg
            ORDER BY vp_orden
        """,
    },
    "cuadro-12-gastos-operativos-comparativo": {
        "titulo": "Cuadro 12 — Gastos Operativos propuestos para 2026",
        "descripcion": "Rubros operativos (5.6.*) con estructura jerárquica padre/subitem según DPP: TI, Comunicaciones (con desglose), Calificación de Riesgo, Auditoría, Servicios Financieros, Gastos Bancarios, Reclutamiento, Representación, Administración (con desglose), más totales 2025/2026 y variación.",
        "fuente": "planificacion",
        "columnas": ["rubro", "monto_2025", "monto_2026", "variacion", "variacion_pct"],
        "sql": """
            WITH base AS (
              SELECT
                c.codigo AS ccod,
                YEAR(m.fecha_movimiento) AS anio,
                CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END AS monto
              FROM ejecucion.movimiento_dedup m
              JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
              JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
              JOIN catalogo.plan_presupuestario p ON p.id = m.plan_id
              WHERE (c.path = 'n5.n6' OR c.path LIKE 'n5.n6.%')
                AND p.codigo = 'PRESUPDEGASTOS'
                AND YEAR(m.fecha_movimiento) IN (2025, 2026)
            ),
            m_x_cuenta AS (
              SELECT ccod,
                SUM(CASE WHEN anio=2025 THEN monto ELSE 0 END) AS m25,
                SUM(CASE WHEN anio=2026 THEN monto ELSE 0 END) AS m26
              FROM base GROUP BY ccod
            ),
            -- Helper para subtotal por LIKE
            tot_like AS (
              SELECT
                SUM(CASE WHEN ccod LIKE '5.6.1.%' THEN m25 ELSE 0 END) AS comu_25,
                SUM(CASE WHEN ccod LIKE '5.6.1.%' THEN m26 ELSE 0 END) AS comu_26,
                SUM(CASE WHEN ccod LIKE '5.6.3.%' THEN m25 ELSE 0 END) AS adm_25,
                SUM(CASE WHEN ccod LIKE '5.6.3.%' THEN m26 ELSE 0 END) AS adm_26,
                SUM(CASE WHEN ccod LIKE '5.6.5.%' THEN m25 ELSE 0 END) AS ti_25,
                SUM(CASE WHEN ccod LIKE '5.6.5.%' THEN m26 ELSE 0 END) AS ti_26,
                SUM(m25) AS tot_25,
                SUM(m26) AS tot_26
              FROM m_x_cuenta
            ),
            filas AS (
              -- Tecnología de la Información
              SELECT 1 AS orden, 'Tecnología de la Información' AS rubro, ti_25 AS m25, ti_26 AS m26 FROM tot_like
              UNION ALL
              -- Programa de Comunicaciones (padre)
              SELECT 2, 'Programa de Comunicaciones', comu_25, comu_26 FROM tot_like
              UNION ALL
              SELECT 3, '  ↳ Reuniones y Eventos', m25, m26 FROM m_x_cuenta WHERE ccod='5.6.1.03'
              UNION ALL
              SELECT 4, '  ↳ Publicaciones, Promoción y Difusión', m25, m26 FROM m_x_cuenta WHERE ccod='5.6.1.02'
              UNION ALL
              SELECT 5, '  ↳ Servicios Contratados Comunicaciones', m25, m26 FROM m_x_cuenta WHERE ccod='5.6.1.04'
              UNION ALL
              SELECT 6, '  ↳ Suscripciones y Afiliaciones', m25, m26 FROM m_x_cuenta WHERE ccod='5.6.1.05'
              UNION ALL
              SELECT 7, '  ↳ Donaciones y Contribuciones', m25, m26 FROM m_x_cuenta WHERE ccod='5.6.1.06'
              UNION ALL
              -- Conceptos individuales
              SELECT 8, 'Calificación de Riesgo Crediticio', m25, m26 FROM m_x_cuenta WHERE ccod='5.6.7.01'
              UNION ALL
              SELECT 9, 'Auditoría Externa', m25, m26 FROM m_x_cuenta WHERE ccod='5.6.8.01'
              UNION ALL
              SELECT 10, 'Servicios Financieros', m25, m26 FROM m_x_cuenta WHERE ccod='5.6.6.02'
              UNION ALL
              SELECT 11, 'Gastos Bancarios y Comisiones', m25, m26 FROM m_x_cuenta WHERE ccod='5.6.6.01'
              UNION ALL
              SELECT 12, 'Reclutamiento de Personal', m25, m26 FROM m_x_cuenta WHERE ccod='5.6.2.01'
              UNION ALL
              SELECT 13, 'Gastos de Representación', m25, m26 FROM m_x_cuenta WHERE ccod='5.6.4.01'
              UNION ALL
              -- Gastos de Administración (padre)
              SELECT 14, 'Gastos de Administración', adm_25, adm_26 FROM tot_like
              UNION ALL
              SELECT 15, '  ↳ Servicios Básicos', m25, m26 FROM m_x_cuenta WHERE ccod='5.6.3.10'
              UNION ALL
              SELECT 16, '  ↳ Expensas Comunes Condominio', m25, m26 FROM m_x_cuenta WHERE ccod='5.6.3.06'
              UNION ALL
              SELECT 17, '  ↳ Mantenimiento y Reparación', m25, m26 FROM m_x_cuenta WHERE ccod='5.6.3.08'
              UNION ALL
              SELECT 18, '  ↳ Materiales y Suministros', m25, m26 FROM m_x_cuenta WHERE ccod='5.6.3.07'
              UNION ALL
              SELECT 19, '  ↳ Seguros (Bienes / Viajes / Empleados)',
                     COALESCE((SELECT SUM(m25) FROM m_x_cuenta WHERE ccod IN ('5.6.3.01','5.6.3.02','5.6.3.03')), 0),
                     COALESCE((SELECT SUM(m26) FROM m_x_cuenta WHERE ccod IN ('5.6.3.01','5.6.3.02','5.6.3.03')), 0)
              UNION ALL
              SELECT 20, '  ↳ Otros Gastos de Administración',
                     COALESCE((SELECT SUM(m25) FROM m_x_cuenta WHERE ccod IN ('5.6.3.04','5.6.3.05','5.6.3.09','5.6.3.11')), 0),
                     COALESCE((SELECT SUM(m26) FROM m_x_cuenta WHERE ccod IN ('5.6.3.04','5.6.3.05','5.6.3.09','5.6.3.11')), 0)
              UNION ALL
              -- Total
              SELECT 90, 'Total', tot_25, tot_26 FROM tot_like
            )
            SELECT
              rubro,
              ROUND(m25, 0) AS monto_2025,
              ROUND(m26, 0) AS monto_2026,
              ROUND((m26 - m25), 0) AS variacion,
              ROUND((100.0 * (m26 - m25) / NULLIF(m25, 0)), 1) AS variacion_pct
            FROM filas
            ORDER BY orden
        """,
    },
    "cuadro-14-inversion-ti-2025": {
        "titulo": "Cuadro 14 — Inversión de TI 2025 (Aprobado vs Ejecutado)",
        "descripcion": "Plan PRESUPBUSO 2025 (Software, Equipos, Instalaciones, Muebles, Equipos de oficina, Obras) — aprobado, ejecutado y % de ejecución.",
        "fuente": "ejecucion",
        "columnas": ["concepto", "aprobado", "ejecutado", "pct_ejecucion"],
        "sql": """
            SELECT
              INITCAP(c.descripcion) AS concepto,
              ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS aprobado,
              ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS ejecutado,
              ROUND((100.0 * SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END) /
                NULLIF(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0)), 1) AS pct_ejecucion
            FROM ejecucion.movimiento_dedup m
            JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
            JOIN catalogo.plan_presupuestario p ON p.id = m.plan_id
            JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
            WHERE p.codigo = 'PRESUPBUSO'
              AND YEAR(m.fecha_movimiento) = 2025
            GROUP BY c.descripcion
            HAVING SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END) > 0
                OR SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END) > 0
            ORDER BY aprobado DESC
        """,
    },
    "cuadro-15-inversion-ti-2026": {
        "titulo": "Cuadro 15 — Inversión de TI 2026 (Solicitado)",
        "descripcion": "Plan PRESUPBUSO 2026 — montos aprobados por concepto contable (Software, Equipos, Instalaciones, etc.).",
        "fuente": "planificacion",
        "columnas": ["concepto", "monto"],
        "sql": """
            SELECT
              INITCAP(c.descripcion) AS concepto,
              ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS monto
            FROM ejecucion.movimiento_dedup m
            JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
            JOIN catalogo.plan_presupuestario p ON p.id = m.plan_id
            JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
            WHERE p.codigo = 'PRESUPBUSO'
              AND YEAR(m.fecha_movimiento) = 2026
            GROUP BY c.descripcion
            HAVING SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END) > 0
            ORDER BY monto DESC
        """,
    },
    "cuadro-16-ejecucion-2025": {
        "titulo": "Cuadro 16 — Estado de Ejecución del Presupuesto 2025",
        "descripcion": "Rubro × Aprobado / Ejecutado / Disponible / % Ejecución (incluye Presupuesto de Capital).",
        "fuente": "ejecucion",
        "columnas": ["rubro", "aprobado", "ejecutado", "disponible", "pct_ejecucion"],
        "sql": """
            WITH base AS (
              SELECT
                CASE
                  WHEN (c.path = 'n5.n5' OR c.path LIKE 'n5.n5.%') THEN 'Reuniones Gobernanza'
                  WHEN (c.path = 'n5.n4' OR c.path LIKE 'n5.n4.%') THEN 'Misiones de Servicio'
                  WHEN (c.path = 'n5.n3' OR c.path LIKE 'n5.n3.%') THEN 'Servicios Profesionales a Término'
                  WHEN (c.path = 'n5.n2' OR c.path LIKE 'n5.n2.%') THEN 'Gastos en Personal'
                  WHEN (c.path = 'n5.n6' OR c.path LIKE 'n5.n6.%') THEN 'Gastos Operativos'
                  WHEN (c.path = 'n1.n7' OR c.path LIKE 'n1.n7.%') THEN 'Presupuesto de Capital (TI)'
                  ELSE 'Otros'
                END AS rubro,
                CASE
                  WHEN (c.path = 'n5.n5' OR c.path LIKE 'n5.n5.%') THEN 1
                  WHEN (c.path = 'n5.n4' OR c.path LIKE 'n5.n4.%') THEN 2
                  WHEN (c.path = 'n5.n3' OR c.path LIKE 'n5.n3.%') THEN 3
                  WHEN (c.path = 'n5.n2' OR c.path LIKE 'n5.n2.%') THEN 4
                  WHEN (c.path = 'n5.n6' OR c.path LIKE 'n5.n6.%') THEN 5
                  WHEN (c.path = 'n1.n7' OR c.path LIKE 'n1.n7.%') THEN 6
                  ELSE 7
                END AS orden,
                tm.categoria AS cat,
                m.monto_vigente AS vig,
                m.monto_ejecutado AS eje
              FROM ejecucion.movimiento_dedup m
              JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
              JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
              JOIN catalogo.plan_presupuestario p ON p.id = m.plan_id
              WHERE YEAR(m.fecha_movimiento) = 2025
                AND p.codigo = 'PRESUPDEGASTOS'
            )
            SELECT
              rubro,
              ROUND(SUM(CASE WHEN cat='inicial' THEN vig ELSE 0 END), 0) AS aprobado,
              ROUND(SUM(CASE WHEN cat NOT IN ('inicial','modificacion') THEN eje ELSE 0 END), 0) AS ejecutado,
              ROUND((SUM(CASE WHEN cat='inicial' THEN vig ELSE 0 END)
                   - SUM(CASE WHEN cat NOT IN ('inicial','modificacion') THEN eje ELSE 0 END)), 0) AS disponible,
              ROUND((100.0 * SUM(CASE WHEN cat NOT IN ('inicial','modificacion') THEN eje ELSE 0 END) /
                NULLIF(SUM(CASE WHEN cat='inicial' THEN vig ELSE 0 END), 0)), 1) AS pct_ejecucion
            FROM base
            GROUP BY rubro, orden
            HAVING SUM(CASE WHEN cat='inicial' THEN vig ELSE 0 END) > 0
            ORDER BY orden
        """,
    },
}


@router.get("/cuadros")
async def list_cuadros() -> list[dict[str, Any]]:
    return [
        {
            "codigo": k,
            "titulo": v["titulo"],
            "descripcion": v["descripcion"],
            "fuente": v["fuente"],
            "columnas": v["columnas"],
        }
        for k, v in CUADROS.items()
    ]


@router.get("/cuadros/{codigo}")
async def get_cuadro(
    codigo: str,
    ciclo_anio: int | None = Query(None, description="Año del ciclo. Hoy los cuadros DPP están fijados a 2025/2026; "
                                                     "valores distintos devuelven 422 hasta que se parametricen."),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    cuadro = CUADROS.get(codigo)
    if not cuadro:
        raise HTTPException(404, f"Cuadro '{codigo}' no encontrado. Disponibles: {list(CUADROS.keys())}")
    if ciclo_anio is not None and ciclo_anio not in (2025, 2026):
        raise HTTPException(422, f"Los cuadros DPP están parametrizados a 2025/2026; ciclo_anio={ciclo_anio} no soportado aún.")

    try:
        rows = (await db.execute(text(cuadro["sql"]))).mappings().all()
        return {
            "codigo": codigo,
            "titulo": cuadro["titulo"],
            "descripcion": cuadro["descripcion"],
            "fuente": cuadro["fuente"],
            "columnas": cuadro["columnas"],
            "filas": [dict(r) for r in rows],
        }
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        # Si la query del cuadro falla (típicamente porque falta data o por un
        # cambio de schema), devolvemos un objeto vacío con el error legible
        # en `mensaje_error` en vez de propagar un 500. Esto evita que un
        # cuadro roto rompa todo el dashboard del cliente.
        return {
            "codigo": codigo,
            "titulo": cuadro["titulo"],
            "descripcion": cuadro["descripcion"],
            "fuente": cuadro["fuente"],
            "columnas": cuadro["columnas"],
            "filas": [],
            "warning": f"Datos no disponibles: {type(e).__name__}: {str(e)[:200]}",
        }


# ============================================================
# Dashboards (módulo Análisis · Dashboards)
#
# Categorías canónicas usadas en los 4 tableros:
#   • Salarios y Beneficios  (5.2.*)
#   • Consultores            (5.3.*)
#   • Misiones de Servicio   (5.4.*)
#   • Reuniones Gobernanza   (5.5.*)
#   • Gastos Operativos      (5.6.*)
#
# Las cuentas mencionadas no son imputables a su nivel raíz pero la regla CASE
# las agrupa por <@ ltree.
# ============================================================

CATEGORIAS_DASHBOARD_SQL = """
  CASE
    WHEN (c.path = 'n5.n2' OR c.path LIKE 'n5.n2.%') THEN 'Salarios y Beneficios'
    WHEN (c.path = 'n5.n3' OR c.path LIKE 'n5.n3.%') THEN 'Consultores'
    WHEN (c.path = 'n5.n4' OR c.path LIKE 'n5.n4.%') THEN 'Misiones de Servicio'
    WHEN (c.path = 'n5.n5' OR c.path LIKE 'n5.n5.%') THEN 'Reuniones Gobernanza'
    WHEN (c.path = 'n5.n6' OR c.path LIKE 'n5.n6.%') THEN 'Gastos Operativos'
    ELSE 'Otros'
  END
"""


@router.get("/dashboard/institucional")
async def dashboard_institucional(
    ciclo_anio: int | None = Query(None, description="Año del ciclo. Si null, usa el ciclo vigente."),
    snapshot: str | None = Query(None, description="Corte de ejecución. Si null, el más reciente."),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Tablero presidencial: comparativo entre VPs × categorías de cuenta.

    Solo deberían ver esto usuarios PRE/VPF/admin (control en el frontend).
    """
    # Resolver snapshot:
    # - Si el usuario lo indica, usar ese.
    # - Si filtró ciclo_anio, usar el snapshot más reciente que TENGA datos para ese año.
    # - Si no, el más reciente global.
    if not snapshot:
        if ciclo_anio:
            snapshot = (await db.execute(text("""
                SELECT TOP 1 snapshot_label FROM ejecucion.movimiento
                WHERE YEAR(fecha_movimiento) = :anio
                GROUP BY snapshot_label
                ORDER BY MAX(snapshot_label) DESC
            """), {"anio": ciclo_anio})).scalar()
        if not snapshot:
            snapshot = (await db.execute(text("""
                SELECT TOP 1 snapshot_label FROM ejecucion.movimiento
                GROUP BY snapshot_label
                ORDER BY MAX(snapshot_label) DESC
            """))).scalar() or "corte_2026_03"

    # Resolver ciclo target (dentro del snapshot)
    if ciclo_anio:
        ciclo_id = (await db.execute(
            text("SELECT id FROM core.ciclo_presupuestario WHERE anio=:a"),
            {"a": ciclo_anio},
        )).scalar()
    else:
        ciclo_id = (await db.execute(
            text("""SELECT TOP 1 cp.id FROM core.ciclo_presupuestario cp
                    WHERE EXISTS (SELECT 1 FROM ejecucion.movimiento_dedup m
                                  WHERE m.ciclo_id = cp.id AND m.snapshot_label = :s)
                    ORDER BY cp.anio DESC"""),
            {"s": snapshot},
        )).scalar()
        if not ciclo_id:
            ciclo_id = (await db.execute(
                text("SELECT TOP 1 id FROM core.ciclo_presupuestario ORDER BY anio DESC")
            )).scalar()
    if not ciclo_id:
        return {"error": "no hay ciclo", "totales": [], "matriz": []}

    params = {"ciclo": ciclo_id, "snap": snapshot}

    # Totales por VP × categoría (subquery para poder usar el alias en GROUP BY)
    sql_matriz = text(f"""
        SELECT vp, categoria,
               ROUND(SUM(aprobado), 0)  AS aprobado,
               ROUND(SUM(ejecutado), 0) AS ejecutado
        FROM (
          SELECT
            m.vp_codigo AS vp,
            {CATEGORIAS_DASHBOARD_SQL} AS categoria,
            CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END AS aprobado,
            CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END AS ejecutado
          FROM ejecucion.movimiento_dedup m
          JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
          JOIN catalogo.plan_presupuestario p ON p.id = m.plan_id
          LEFT JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
          WHERE m.ciclo_id = :ciclo AND m.snapshot_label = :snap AND m.vp_codigo IS NOT NULL
            AND p.codigo = 'PRESUPDEGASTOS'
        ) base
        GROUP BY vp, categoria
        ORDER BY vp, categoria
    """)
    matriz = [dict(r) for r in (await db.execute(sql_matriz, params)).mappings().all()]

    # KPIs institucionales (suma de TODO incluyendo movs sin VP — capital/fondo)
    sql_kpis = text("""
        SELECT
          ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS aprobado_total,
          ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS ejecutado_total,
          COUNT(DISTINCT m.vp_codigo) AS vps,
          COUNT(*) AS movimientos
        FROM ejecucion.movimiento_dedup m
        JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        JOIN catalogo.plan_presupuestario p ON p.id = m.plan_id
        WHERE m.ciclo_id = :ciclo AND m.snapshot_label = :snap
          AND p.codigo = 'PRESUPDEGASTOS'
    """)
    kpis = dict((await db.execute(sql_kpis, params)).mappings().first() or {})

    # Top 8 cuentas por monto aprobado
    sql_top = text("""
        SELECT TOP 8
          c.codigo AS cuenta_codigo,
          c.descripcion AS cuenta,
          ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS aprobado,
          ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS ejecutado
        FROM ejecucion.movimiento_dedup m
        JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
        JOIN catalogo.plan_presupuestario p ON p.id = m.plan_id
        WHERE m.ciclo_id = :ciclo AND m.snapshot_label = :snap AND c.imputable = 1
          AND p.codigo = 'PRESUPDEGASTOS'
        GROUP BY c.codigo, c.descripcion
        HAVING SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END) > 0
        ORDER BY aprobado DESC
    """)
    top_cuentas = [dict(r) for r in (await db.execute(sql_top, params)).mappings().all()]

    # Año real del ciclo
    anio_real = (await db.execute(
        text("SELECT anio FROM core.ciclo_presupuestario WHERE id=:c"), {"c": ciclo_id}
    )).scalar()

    return {
        "anio": anio_real,
        "kpis": kpis,
        "matriz_vp_categoria": matriz,
        "top_cuentas": top_cuentas,
    }


@router.get("/dashboard/por-vp/{vp_codigo}")
async def dashboard_por_vp(
    vp_codigo: str,
    ciclo_anio: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Tablero de una sola VP: cuentas relevantes, distribución por categoría, % ejecución."""
    if ciclo_anio:
        ciclo_id = (await db.execute(
            text("SELECT id FROM core.ciclo_presupuestario WHERE anio=:a"),
            {"a": ciclo_anio},
        )).scalar()
    else:
        ciclo_id = (await db.execute(
            text("""SELECT TOP 1 id FROM core.ciclo_presupuestario
                    WHERE estado IN ('vigente','planificacion')
                    ORDER BY anio DESC""")
        )).scalar()
    if not ciclo_id:
        return {"error": "no hay ciclo"}

    # Mapeo VP_codigo → nombre completo (igual al de planificacion.py)
    CODIGO_VP = {
        "GOB": "GOBERNANZA INSTITUCIONAL",
        "PRE": "PRESIDENCIA EJECUTIVA",
        "VPE": "VICEPRESIDENCIA EJECUTIVA",
        "VPD": "VICEPRESIDENCIA DE DESARROLLO ESTRATÉGICO",
        "VPO": "VICEPRESIDENCIA DE OPERACIONES Y PAÍSES",
        "VPF": "VICEPRESIDENCIA DE FINANZAS",
    }
    vp_nombre = CODIGO_VP.get(vp_codigo.upper(), vp_codigo)

    # Por categoría (subquery para alias en GROUP BY)
    sql_cat = text(f"""
        SELECT categoria,
               ROUND(SUM(aprobado), 0)  AS aprobado,
               ROUND(SUM(ejecutado), 0) AS ejecutado
        FROM (
          SELECT
            {CATEGORIAS_DASHBOARD_SQL} AS categoria,
            CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END AS aprobado,
            CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END AS ejecutado
          FROM ejecucion.movimiento_dedup m
          JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
          JOIN catalogo.plan_presupuestario p ON p.id = m.plan_id
          LEFT JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
          WHERE m.ciclo_id = :ciclo AND m.vp_codigo = :vp
            AND p.codigo = 'PRESUPDEGASTOS'
        ) base
        GROUP BY categoria
        ORDER BY aprobado DESC
    """)
    por_categoria = [dict(r) for r in
                     (await db.execute(sql_cat, {"ciclo": ciclo_id, "vp": vp_nombre})).mappings().all()]

    # KPIs
    sql_kpis = text("""
        SELECT
          ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS aprobado,
          ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS ejecutado,
          COUNT(*) AS movimientos
        FROM ejecucion.movimiento_dedup m
        JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        JOIN catalogo.plan_presupuestario p ON p.id = m.plan_id
        WHERE m.ciclo_id = :ciclo AND m.vp_codigo = :vp
          AND p.codigo = 'PRESUPDEGASTOS'
    """)
    kpis = dict((await db.execute(sql_kpis, {"ciclo": ciclo_id, "vp": vp_nombre})).mappings().first() or {})

    # Top 10 cuentas
    sql_top = text("""
        SELECT TOP 10
          c.codigo AS cuenta_codigo,
          c.descripcion AS cuenta,
          ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS aprobado,
          ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS ejecutado
        FROM ejecucion.movimiento_dedup m
        JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
        WHERE m.ciclo_id = :ciclo AND m.vp_codigo = :vp AND c.imputable = 1
        GROUP BY c.codigo, c.descripcion
        HAVING SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END) > 0
        ORDER BY aprobado DESC
    """)
    top_cuentas = [dict(r) for r in
                   (await db.execute(sql_top, {"ciclo": ciclo_id, "vp": vp_nombre})).mappings().all()]

    anio_real = (await db.execute(
        text("SELECT anio FROM core.ciclo_presupuestario WHERE id=:c"), {"c": ciclo_id}
    )).scalar()

    return {
        "anio": anio_real,
        "vp_codigo": vp_codigo.upper(),
        "vp_nombre": vp_nombre,
        "kpis": kpis,
        "por_categoria": por_categoria,
        "top_cuentas": top_cuentas,
    }


@router.get("/dashboard/historico")
async def dashboard_historico(
    vp_codigo: str | None = Query(None, description="Si se indica, filtra a una VP. Si null, devuelve todas."),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Comparativo interanual: cada año × VP × categoría.

    Si vp_codigo es None: el tablero institucional ve todas las VPs.
    Si vp_codigo se setea: el tablero por VP ve solo la suya.
    """
    CODIGO_VP = {
        "GOB": "GOBERNANZA INSTITUCIONAL",
        "PRE": "PRESIDENCIA EJECUTIVA",
        "VPE": "VICEPRESIDENCIA EJECUTIVA",
        "VPD": "VICEPRESIDENCIA DE DESARROLLO ESTRATÉGICO",
        "VPO": "VICEPRESIDENCIA DE OPERACIONES Y PAÍSES",
        "VPF": "VICEPRESIDENCIA DE FINANZAS",
    }
    where_vp = ""
    params: dict[str, Any] = {}
    if vp_codigo:
        where_vp = "AND m.vp_codigo = :vp"
        params["vp"] = CODIGO_VP.get(vp_codigo.upper(), vp_codigo)

    sql = text(f"""
        SELECT anio, vp, categoria,
               ROUND(SUM(aprobado), 0)  AS aprobado,
               ROUND(SUM(ejecutado), 0) AS ejecutado
        FROM (
          SELECT
            cp.anio AS anio,
            m.vp_codigo AS vp,
            {CATEGORIAS_DASHBOARD_SQL} AS categoria,
            CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END AS aprobado,
            CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END AS ejecutado
          FROM ejecucion.movimiento_dedup m
          JOIN catalogo.plan_presupuestario p ON p.id = m.plan_id
          JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
          JOIN core.ciclo_presupuestario cp ON cp.id = m.ciclo_id
          LEFT JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
          WHERE p.codigo = 'PRESUPDEGASTOS' AND m.vp_codigo IS NOT NULL {where_vp}
        ) base
        GROUP BY anio, vp, categoria
        ORDER BY anio, vp, categoria
    """)
    filas = [dict(r) for r in (await db.execute(sql, params)).mappings().all()]
    anios = sorted({f["anio"] for f in filas})
    return {"anios": anios, "filas": filas, "vp_codigo": vp_codigo.upper() if vp_codigo else None}


# ============================================================
# Drilldown universal — del agregado al movimiento K2B individual
# ============================================================
_CODIGO_VP_NOMBRE = {
    "GOB": "GOBERNANZA INSTITUCIONAL",
    "PRE": "PRESIDENCIA EJECUTIVA",
    "VPE": "VICEPRESIDENCIA EJECUTIVA",
    "VPD": "VICEPRESIDENCIA DE DESARROLLO ESTRATÉGICO",
    "VPO": "VICEPRESIDENCIA DE OPERACIONES Y PAÍSES",
    "VPF": "VICEPRESIDENCIA DE FINANZAS",
}

_CATEGORIA_TO_LTREE = {
    "Salarios y Beneficios": "n5.n2",
    "Consultores":            "n5.n3",
    "Misiones de Servicio":   "n5.n4",
    "Reuniones Gobernanza":   "n5.n5",
    "Gastos Operativos":      "n5.n6",
}


@router.get("/drilldown/movimientos")
async def drilldown_movimientos(
    ciclo_anio: int | None = Query(None),
    vp_codigo: str | None = Query(None, description="Filtra por VP corta: GOB/PRE/VPE/VPD/VPO/VPF"),
    categoria: str | None = Query(None, description="Salarios y Beneficios / Consultores / Misiones de Servicio / Reuniones Gobernanza / Gastos Operativos"),
    cuenta_codigo: str | None = Query(None, description="Filtra por código exacto de cuenta (ej. 5.4.1.01)"),
    cuenta_codigo_prefix: str | None = Query(None, description="Filtra por prefijo de cuenta (ej. '5.4.1' incluye 5.4.1.01, 5.4.1.02…)"),
    item_codigo: str | None = Query(None, description="Filtra por código exacto de item (ej. 02.05.02)"),
    plan_codigo: str | None = Query(None, description="Filtra por plan: PRESUPDEGASTOS, PRESUPBUSO, PREFONESP"),
    tipo: str | None = Query(None, description="aprobado | ejecutado"),
    snapshot: str | None = Query(None, description="Corte de ejecución. Si null, el más reciente."),
    limit: int = Query(200, le=2000),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Devuelve movimientos K2B individuales según filtros.

    Usado por el Drilldown universal del frontend: cualquier número agregado
    se puede expandir hasta acá para ver las transacciones que lo componen.
    """
    # Resolver snapshot:
    # - Si el usuario lo especificó, usar ese.
    # - Si no, y se filtró un ciclo_anio, usar el snapshot más reciente QUE TENGA datos para ese año
    #   (ej. 2025 solo está en corte_2026_03; 2026 está en ambos pero el más reciente es corte_2026_05).
    # - Si no hay ciclo_anio, usar el snapshot global más reciente.
    if not snapshot:
        if ciclo_anio:
            snapshot = (await db.execute(text("""
                SELECT TOP 1 snapshot_label FROM ejecucion.movimiento
                WHERE YEAR(fecha_movimiento) = :anio
                GROUP BY snapshot_label
                ORDER BY MAX(snapshot_label) DESC
            """), {"anio": ciclo_anio})).scalar()
        if not snapshot:
            snapshot = (await db.execute(text("""
                SELECT TOP 1 snapshot_label FROM ejecucion.movimiento
                GROUP BY snapshot_label
                ORDER BY MAX(snapshot_label) DESC
            """))).scalar() or "corte_2026_03"

    where = ["m.snapshot_label = :snap"]
    params: dict[str, Any] = {"snap": snapshot}

    if ciclo_anio:
        where.append("cp.anio = :anio")
        params["anio"] = ciclo_anio
    if vp_codigo:
        where.append("m.vp_codigo = :vp")
        params["vp"] = _CODIGO_VP_NOMBRE.get(vp_codigo.upper(), vp_codigo)
    if categoria:
        ltree_pref = _CATEGORIA_TO_LTREE.get(categoria)
        if ltree_pref:
            # Parametrizamos los valores aunque vengan de un dict del código:
            # mantener bindparams en todos los puntos hace más fácil auditar
            # que no hay inyección de SQL en este endpoint.
            where.append("(c.path = :ltp OR c.path LIKE :ltp_like)")
            params["ltp"] = ltree_pref
            params["ltp_like"] = f"{ltree_pref}.%"
        else:
            # categoría arbitraria: filtramos por descripción
            where.append("c.descripcion LIKE :cat")
            params["cat"] = f"%{categoria}%"
    if cuenta_codigo:
        where.append("c.codigo = :cc")
        params["cc"] = cuenta_codigo
    if cuenta_codigo_prefix:
        # Coincidencia por prefijo dot-aware: 5.4.1 ⇒ 5.4.1.* (no matchea 5.41.x).
        where.append("(c.codigo = :ccp OR c.codigo LIKE :ccp_like)")
        params["ccp"] = cuenta_codigo_prefix
        params["ccp_like"] = cuenta_codigo_prefix + ".%"
    if item_codigo:
        where.append("i.codigo = :ic")
        params["ic"] = item_codigo
    if plan_codigo:
        where.append("pl.codigo = :pc")
        params["pc"] = plan_codigo
    if tipo == "aprobado":
        where.append("tm.categoria = 'inicial'")
    elif tipo == "ejecutado":
        where.append("tm.categoria NOT IN ('inicial','modificacion')")

    where_sql = " AND ".join(where)

    sql_mov = text(f"""
        SELECT TOP (:limit)
          m.id, m.k2b_id, m.fecha_movimiento,
          cp.anio AS ciclo_anio,
          m.vp_codigo, m.area, m.centro_presupuestal, m.subcentro_presupuestal,
          c.codigo AS cuenta_codigo, c.descripcion AS cuenta_descripcion,
          i.codigo AS item_codigo, i.descripcion AS item_descripcion,
          pl.codigo AS plan_codigo,
          tm.k2b_codigo AS tipo_codigo, tm.nombre AS tipo_nombre, tm.categoria AS tipo_categoria,
          m.documento_tipo, m.documento_numero, m.concepto, m.persona,
          m.monto_vigente, m.monto_ejecutado, m.monto_comprometido, m.monto_pagado
        FROM ejecucion.movimiento_dedup m
        JOIN core.ciclo_presupuestario cp ON cp.id = m.ciclo_id
        JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
        JOIN catalogo.plan_presupuestario pl ON pl.id = m.plan_id
        LEFT JOIN catalogo.item_planificacion i ON i.id = m.item_id
        WHERE {where_sql}
        ORDER BY m.fecha_movimiento DESC, m.id DESC
    """)
    params["limit"] = limit
    rows = [dict(r) for r in (await db.execute(sql_mov, params)).mappings().all()]

    # Resumen agregado para mostrar contexto. Incluimos el LEFT JOIN a item
    # porque where_sql puede referenciar i.codigo cuando se filtra por item_codigo.
    sql_sum = text(f"""
        SELECT
          COUNT(*) AS total,
          ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS aprobado,
          ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS ejecutado
        FROM ejecucion.movimiento_dedup m
        JOIN core.ciclo_presupuestario cp ON cp.id = m.ciclo_id
        JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
        JOIN catalogo.plan_presupuestario pl ON pl.id = m.plan_id
        LEFT JOIN catalogo.item_planificacion i ON i.id = m.item_id
        WHERE {where_sql}
    """)
    resumen = dict((await db.execute(sql_sum, params)).mappings().first() or {})

    return {
        "filtros": {
            "ciclo_anio": ciclo_anio,
            "vp_codigo": vp_codigo,
            "categoria": categoria,
            "cuenta_codigo": cuenta_codigo,
            "cuenta_codigo_prefix": cuenta_codigo_prefix,
            "item_codigo": item_codigo,
            "plan_codigo": plan_codigo,
            "tipo": tipo,
        },
        "resumen": resumen,
        "movimientos": rows,
        "limit": limit,
        "trunc": resumen.get("total", 0) and int(resumen.get("total") or 0) > limit,
    }


# ============================================================
# Comparativo de ciclos: planificación (en curso) vs vigente vs ejecutado anterior
# ============================================================
@router.get("/dashboard/comparativo-ciclos")
async def comparativo_ciclos(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """3 columnas para cada VP × categoría:
      - planificacion: lo que se está cargando en el ciclo abierto (planificacion.linea_solicitud)
      - vigente:       el aprobado/vigente del último ciclo cerrado o en curso
      - ejecutado:     ejecutado del ciclo INMEDIATAMENTE anterior al vigente
    """
    # Resolver los 3 ciclos relevantes
    ciclos = (await db.execute(text("""
        SELECT id, anio, estado FROM core.ciclo_presupuestario ORDER BY anio
    """))).mappings().all()
    if not ciclos:
        return {"error": "sin ciclos"}

    anios = [c["anio"] for c in ciclos]
    # planificación = ciclo más nuevo en estado 'planificacion'
    ciclo_plan = next((c for c in reversed(ciclos) if c["estado"] == "planificacion"), None)
    # vigente = el más nuevo NO 'planificacion' (vigente o cerrado)
    ciclo_vig = next((c for c in reversed(ciclos) if c["estado"] != "planificacion"), None)
    # anterior = el inmediato anterior al vigente
    ciclo_ant = None
    if ciclo_vig:
        idx_vig = anios.index(ciclo_vig["anio"])
        if idx_vig > 0:
            ciclo_ant = next((c for c in ciclos if c["anio"] == anios[idx_vig - 1]), None)

    # Planificación: sumar líneas de solicitud por VP × categoría (cuenta)
    plan_filas: list[dict[str, Any]] = []
    if ciclo_plan:
        sql_plan = text(f"""
            SELECT vp, categoria, ROUND(SUM(monto), 0) AS monto
            FROM (
              SELECT
                s.vp_codigo AS vp,
                {CATEGORIAS_DASHBOARD_SQL} AS categoria,
                ls.monto_solicitado AS monto
              FROM planificacion.linea_solicitud ls
              JOIN planificacion.solicitud s ON s.id = ls.solicitud_id
              JOIN catalogo.cuenta_planificacion c ON c.id = ls.cuenta_id
              WHERE s.ciclo_id = :cid
            ) base
            GROUP BY vp, categoria
            HAVING SUM(monto) > 0
            ORDER BY vp, categoria
        """).bindparams(cid=ciclo_plan["id"])
        try:
            plan_filas = [dict(r) for r in (await db.execute(sql_plan)).mappings().all()]
        except SQLAlchemyError:
            # Si la query plan falla (típicamente: no hay datos de planificación
            # para ese ciclo), seguimos con lista vacía y el dashboard pinta
            # solo el lado de ejecución.
            plan_filas = []

    # Vigente y ejecutado anterior: misma estructura desde ejecucion.movimiento
    async def por_vp_cat(ciclo_id: int, mode: str) -> list[dict[str, Any]]:
        cond = "tm.categoria = 'inicial'" if mode == "vigente" else "tm.categoria NOT IN ('inicial','modificacion')"
        col  = "m.monto_vigente" if mode == "vigente" else "m.monto_ejecutado"
        # Mapeamos vp_codigo (nombre largo) a su código corto para uniformar
        sql = text(f"""
            SELECT vp, categoria, ROUND(SUM(monto), 0) AS monto
            FROM (
              SELECT
                CASE m.vp_codigo
                  WHEN 'GOBERNANZA INSTITUCIONAL' THEN 'GOB'
                  WHEN 'PRESIDENCIA EJECUTIVA' THEN 'PRE'
                  WHEN 'VICEPRESIDENCIA EJECUTIVA' THEN 'VPE'
                  WHEN 'VICEPRESIDENCIA DE DESARROLLO ESTRATÉGICO' THEN 'VPD'
                  WHEN 'VICEPRESIDENCIA DE OPERACIONES Y PAÍSES' THEN 'VPO'
                  WHEN 'VICEPRESIDENCIA DE FINANZAS' THEN 'VPF'
                  ELSE m.vp_codigo
                END AS vp,
                {CATEGORIAS_DASHBOARD_SQL} AS categoria,
                CASE WHEN {cond} THEN {col} ELSE 0 END AS monto
              FROM ejecucion.movimiento_dedup m
              JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
              LEFT JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
              WHERE m.ciclo_id = :cid AND m.vp_codigo IS NOT NULL
            ) base
            GROUP BY vp, categoria
            HAVING SUM(monto) > 0
            ORDER BY vp, categoria
        """).bindparams(cid=ciclo_id)
        return [dict(r) for r in (await db.execute(sql)).mappings().all()]

    vig_filas = await por_vp_cat(ciclo_vig["id"], "vigente") if ciclo_vig else []
    ant_filas = await por_vp_cat(ciclo_ant["id"], "ejecutado") if ciclo_ant else []

    return {
        "ciclos": {
            "planificacion": dict(ciclo_plan) if ciclo_plan else None,
            "vigente":       dict(ciclo_vig)  if ciclo_vig  else None,
            "anterior":      dict(ciclo_ant)  if ciclo_ant  else None,
        },
        "filas": {
            "planificacion": plan_filas,
            "vigente":       vig_filas,
            "ejecutado_anterior": ant_filas,
        },
    }


# ============================================================
# Calendario de ejecución (heatmap mes × categoría)
# ============================================================
@router.get("/dashboard/calendario-ejecucion")
async def calendario_ejecucion(
    ciclo_anio: int | None = Query(None),
    vp_codigo: str | None = Query(None, description="GOB/PRE/VPE/VPD/VPO/VPF — si null, institucional"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Heatmap mes × categoría con monto ejecutado.

    Útil para detectar estacionalidad (misiones se concentran en Q4, salarios planos, etc.).
    """
    if ciclo_anio:
        ciclo_id = (await db.execute(
            text("SELECT id FROM core.ciclo_presupuestario WHERE anio=:a"),
            {"a": ciclo_anio},
        )).scalar()
    else:
        ciclo_id = (await db.execute(
            text("""SELECT TOP 1 cp.id FROM core.ciclo_presupuestario cp
                    WHERE EXISTS (SELECT 1 FROM ejecucion.movimiento_dedup m WHERE m.ciclo_id = cp.id)
                    ORDER BY cp.anio DESC""")
        )).scalar()
    if not ciclo_id:
        return {"error": "sin ciclo"}

    where_vp = ""
    params: dict[str, Any] = {"cid": ciclo_id}
    if vp_codigo:
        where_vp = "AND m.vp_codigo = :vp"
        params["vp"] = _CODIGO_VP_NOMBRE.get(vp_codigo.upper(), vp_codigo)

    sql = text(f"""
        SELECT mes, categoria,
               ROUND(SUM(monto), 0) AS monto,
               SUM(cnt) AS movimientos
        FROM (
          SELECT
            MONTH(m.fecha_movimiento) AS mes,
            {CATEGORIAS_DASHBOARD_SQL} AS categoria,
            CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END AS monto,
            CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN 1 ELSE 0 END AS cnt
          FROM ejecucion.movimiento_dedup m
          JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
          LEFT JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
          WHERE m.ciclo_id = :cid {where_vp}
        ) base
        WHERE monto > 0
        GROUP BY mes, categoria
        ORDER BY mes, categoria
    """)
    filas = [dict(r) for r in (await db.execute(sql, params)).mappings().all()]

    anio_real = (await db.execute(
        text("SELECT anio FROM core.ciclo_presupuestario WHERE id=:c"), {"c": ciclo_id}
    )).scalar()

    return {
        "anio": anio_real,
        "vp_codigo": vp_codigo.upper() if vp_codigo else None,
        "filas": filas,
    }
