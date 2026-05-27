"""Endpoints del módulo Planificación — workflows agregados y desgloses por VP.

Este router contesta tres preguntas:
  1) ¿Qué workflows hay en la institución? (uno por (ciclo, VP))
     GET /workflows  → lista para el dashboard de planificación
     GET /workflows-institucionales → versión consolidada para análisis
  2) ¿Cómo está la VP X en el ciclo Y? (resumen agregado por categoría)
     GET /workflows/{anio}/{vp}/resumen
  3) ¿Qué pidieron las líneas de planificación, desglosado por item × cuenta?
     GET /avance
     GET /avance/lineas

"workflow" acá es un concepto agregado (un par ciclo × VP), no una máquina
de estados ni la solicitud propiamente dicha — un workflow puede contener
0, 1 o varias solicitudes en distintos estados.
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.security import CurrentUser, get_current_user

router = APIRouter(prefix="/planificacion", tags=["planificacion"])


# Lista canónica de VPs que generan workflows (Transversal excluida — standby)
VPS_WORKFLOW = [
    "GOBERNANZA INSTITUCIONAL",
    "PRESIDENCIA EJECUTIVA",
    "VICEPRESIDENCIA EJECUTIVA",
    "VICEPRESIDENCIA DE DESARROLLO ESTRATÉGICO",
    "VICEPRESIDENCIA DE OPERACIONES Y PAÍSES",
    "VICEPRESIDENCIA DE FINANZAS",
]

# Mapeo VP → código corto (usado en URLs y permisos)
VP_CODIGO = {
    "GOBERNANZA INSTITUCIONAL": "GOB",
    "PRESIDENCIA EJECUTIVA": "PRE",
    "VICEPRESIDENCIA EJECUTIVA": "VPE",
    "VICEPRESIDENCIA DE DESARROLLO ESTRATÉGICO": "VPD",
    "VICEPRESIDENCIA DE OPERACIONES Y PAÍSES": "VPO",
    "VICEPRESIDENCIA DE FINANZAS": "VPF",
}
CODIGO_VP = {v: k for k, v in VP_CODIGO.items()}


@router.get("/workflows")
async def listar_workflows(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Devuelve un workflow por (ciclo, VP) — uno por VP por año.

    El frontend filtra por permisos del usuario (vp_codigo + ver_todo).
    """
    sql = text("""
        WITH agg AS (
          SELECT
            cp.id AS ciclo_id,
            cp.anio,
            cp.estado AS ciclo_estado,
            m.vp_codigo AS vp,
            ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS aprobado,
            ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS ejecutado,
            COUNT(*) AS movimientos,
            MAX(m.fecha_movimiento) AS ultima_actualizacion
          FROM ejecucion.movimiento_dedup m
          JOIN core.ciclo_presupuestario cp ON cp.id = m.ciclo_id
          JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
          JOIN (
            SELECT YEAR(fecha_movimiento) AS anio,
                   MAX(snapshot_label) AS snap
            FROM ejecucion.movimiento GROUP BY YEAR(fecha_movimiento)
          ) lsy ON lsy.anio = YEAR(m.fecha_movimiento)
               AND lsy.snap = m.snapshot_label
          WHERE m.vp_codigo IS NOT NULL
          GROUP BY cp.id, cp.anio, cp.estado, m.vp_codigo
        )
        SELECT * FROM agg
        -- SQL Server pone NULLs primero en DESC; el CASE lo invierte → NULLs al final.
        ORDER BY anio DESC, CASE WHEN aprobado IS NULL THEN 1 ELSE 0 END, aprobado DESC
    """)
    rows = (await db.execute(sql)).mappings().all()
    out = []
    for r in rows:
        vp = r["vp"]
        if vp == "TRANSVERSAL":
            continue  # standby
        ciclo_estado = r["ciclo_estado"]
        # Para workflows históricos (cerrado/vigente), todos están aprobados por Directorio
        state = 4 if ciclo_estado in ("cerrado", "vigente") else (3 if ciclo_estado == "planificacion" else 0)
        vp_code = VP_CODIGO.get(vp, vp[:3])
        out.append({
            "ciclo_id": r["ciclo_id"],
            "anio": r["anio"],
            "vp": vp,
            "vp_codigo": vp_code,
            "code": f"WF-{r['anio']}-{vp_code}",
            "name": f"Presupuesto {vp.title()} {r['anio']}",
            "owner": vp_code,
            "state": state,
            "ciclo_estado": ciclo_estado,
            "aprobado_total": float(r["aprobado"] or 0),
            "ejecutado_total": float(r["ejecutado"] or 0),
            "movimientos": r["movimientos"],
            "updated": r["ultima_actualizacion"].isoformat() if r["ultima_actualizacion"] else None,
        })
    return out


@router.get("/workflows/{anio}/{vp_codigo}/resumen")
async def resumen_workflow_vp(
    anio: int,
    vp_codigo: str,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Resumen aglomerado del workflow de una VP × año.

    Devuelve:
      - cabecera con totales aprobado/ejecutado
      - tabla de categorías de cuenta agrupadas (Salarios, Consultores, Misiones, etc.)
        cada una con sus cuentas hoja desglosadas
    """
    vp = CODIGO_VP.get(vp_codigo.upper())
    if not vp:
        raise HTTPException(404, f"VP {vp_codigo} no encontrada")

    ciclo = (await db.execute(
        text("SELECT id, nombre, estado FROM core.ciclo_presupuestario WHERE anio=:a"),
        {"a": anio},
    )).mappings().first()
    if not ciclo:
        raise HTTPException(404, f"Ciclo {anio} no encontrado")

    # Cabecera con totales para esta VP × ciclo
    head_sql = text("""
        SELECT
          ROUND(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END), 0) AS aprobado,
          ROUND(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END), 0) AS ejecutado,
          COUNT(*) AS movimientos
        FROM ejecucion.movimiento_dedup m
        JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        JOIN (
          SELECT YEAR(fecha_movimiento) AS anio,
                 MAX(snapshot_label) AS snap
          FROM ejecucion.movimiento GROUP BY YEAR(fecha_movimiento)
        ) lsy ON lsy.anio = YEAR(m.fecha_movimiento)
             AND lsy.snap = m.snapshot_label
        WHERE m.ciclo_id = :ciclo_id AND m.vp_codigo = :vp
    """)
    head = (await db.execute(head_sql, {"ciclo_id": ciclo["id"], "vp": vp})).mappings().first()

    # Detalle agrupado por categoría (nivel 3 de la cuenta) y cuentas hoja
    detalle_sql = text("""
        WITH base AS (
          SELECT
            -- categoría: descripción del nivel 3 ('Salarios y Beneficios', 'Consultores', etc.)
            COALESCE(c3.descripcion, c.descripcion) AS categoria,
            COALESCE(c3.codigo, c.codigo) AS categoria_codigo,
            c.codigo AS cuenta_codigo,
            c.descripcion AS cuenta,
            CASE WHEN tm.categoria='inicial' THEN m.monto_vigente ELSE 0 END AS aprobado,
            CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado ELSE 0 END AS ejecutado
          FROM ejecucion.movimiento_dedup m
          JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
          JOIN (
            SELECT YEAR(fecha_movimiento) AS anio,
                   MAX(snapshot_label) AS snap
            FROM ejecucion.movimiento GROUP BY YEAR(fecha_movimiento)
          ) lsy ON lsy.anio = YEAR(m.fecha_movimiento)
               AND lsy.snap = m.snapshot_label
          JOIN catalogo.cuenta_planificacion c ON c.id = m.cuenta_id
          -- ltree `<@` (descendiente o igual) → comparación con LIKE prefijo.
          LEFT JOIN catalogo.cuenta_planificacion c3
                 ON c3.nivel = 3
                AND (c.path = c3.path OR c.path LIKE c3.path + '.%')
          WHERE m.ciclo_id = :ciclo_id AND m.vp_codigo = :vp
        )
        SELECT
          categoria,
          categoria_codigo,
          cuenta_codigo,
          cuenta,
          ROUND(SUM(aprobado), 0) AS aprobado,
          ROUND(SUM(ejecutado), 0) AS ejecutado
        FROM base
        GROUP BY categoria, categoria_codigo, cuenta_codigo, cuenta
        HAVING SUM(aprobado) > 0
        ORDER BY categoria_codigo, cuenta_codigo
    """)
    rows = (await db.execute(detalle_sql, {"ciclo_id": ciclo["id"], "vp": vp})).mappings().all()

    # Agrupar en estructura jerárquica { categoria: { total_aprobado, total_ejecutado, cuentas: [...] } }
    categorias: dict[str, dict[str, Any]] = {}
    for r in rows:
        cat = r["categoria"]
        if cat not in categorias:
            categorias[cat] = {
                "categoria": cat,
                "categoria_codigo": r["categoria_codigo"],
                "aprobado": 0.0,
                "ejecutado": 0.0,
                "cuentas": [],
            }
        ap = float(r["aprobado"] or 0)
        ej = float(r["ejecutado"] or 0)
        categorias[cat]["aprobado"] += ap
        categorias[cat]["ejecutado"] += ej
        categorias[cat]["cuentas"].append({
            "codigo": r["cuenta_codigo"],
            "descripcion": r["cuenta"],
            "aprobado": ap,
            "ejecutado": ej,
        })

    # Ordenar por aprobado descendente
    cats_ordenadas = sorted(categorias.values(), key=lambda c: c["aprobado"], reverse=True)

    return {
        "anio": anio,
        "vp": vp,
        "vp_codigo": vp_codigo.upper(),
        "ciclo_estado": ciclo["estado"],
        "aprobado_total": float(head["aprobado"] or 0) if head else 0,
        "ejecutado_total": float(head["ejecutado"] or 0) if head else 0,
        "movimientos": head["movimientos"] if head else 0,
        "categorias": cats_ordenadas,
    }


# Endpoint legacy (institucional consolidado) — Análisis lo usa
@router.get("/workflows-institucionales")
async def workflows_institucionales(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    sql = text("""
        SELECT
          cp.id, cp.anio, cp.nombre, cp.estado AS ciclo_estado,
          COALESCE(SUM(CASE WHEN tm.categoria='inicial' THEN m.monto_vigente END), 0) AS aprobado_total,
          COALESCE(SUM(CASE WHEN tm.categoria NOT IN ('inicial','modificacion') THEN m.monto_ejecutado END), 0) AS ejecutado_total,
          COUNT(m.id) AS movimientos,
          MAX(m.fecha_movimiento) AS ultima_actualizacion
        FROM core.ciclo_presupuestario cp
        LEFT JOIN ejecucion.movimiento_dedup m ON m.ciclo_id = cp.id
          AND m.snapshot_label = (SELECT MAX(snapshot_label)
                                  FROM ejecucion.movimiento m2
                                  WHERE YEAR(m2.fecha_movimiento) = cp.anio)
        LEFT JOIN catalogo.tipo_movimiento tm ON tm.id = m.tipo_movimiento_id
        GROUP BY cp.id, cp.anio, cp.nombre, cp.estado
        ORDER BY anio DESC
    """)
    rows = (await db.execute(sql)).mappings().all()
    return [
        {
            "ciclo_id": r["id"],
            "anio": r["anio"],
            "nombre": r["nombre"],
            "estado": r["ciclo_estado"],
            "aprobado": float(r["aprobado_total"] or 0),
            "ejecutado": float(r["ejecutado_total"] or 0),
            "movimientos": r["movimientos"],
        }
        for r in rows
    ]


def _alcance_avance_sql(vp_codigo: str | None, planillas_extra: list[str] | None) -> tuple[str, dict[str, Any]]:
    """Construye la cláusula `AND ...` que filtra por vp + planillas_extra.

    Helper único compartido entre `/avance` y `/avance/lineas` para evitar
    drift entre los dos endpoints (regla cross-VP en un solo lugar).

    Si hay `planillas_extra`, devuelve la lista en `params["pextra"]` para que
    el caller la pase con `bindparam("pextra", expanding=True)`.
    """
    params: dict[str, Any] = {}
    if not vp_codigo:
        return "", params
    if planillas_extra:
        params["vp"] = vp_codigo
        params["pextra"] = planillas_extra
        return (
            " AND (s.vp_codigo = :vp OR EXISTS ("
            "  SELECT 1 FROM planificacion.linea_solicitud l2"
            "  JOIN catalogo.planilla_template pt ON pt.id = l2.planilla_template_id"
            "  WHERE l2.solicitud_id = s.id AND pt.codigo IN :pextra"
            "))",
            params,
        )
    params["vp"] = vp_codigo
    return " AND s.vp_codigo = :vp", params


def _bind_expanding_pextra(stmt, params: dict[str, Any]):
    """Si la query consume :pextra, lo declaramos `expanding` para que
    SQLAlchemy lo materialice como un IN (?, ?, ?) compatible con SQL Server."""
    if "pextra" in params:
        return stmt.bindparams(bindparam("pextra", expanding=True))
    return stmt


@router.get("/avance")
async def avance(
    ciclo_anio: int,
    vp_codigo: str | None = None,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Vista jerárquica de avance: agrupa líneas de la solicitud por
    item × cuenta nivel 2 × cuenta nivel 3 × cuenta imputable y suma montos.

    Optimización: deriva los códigos de nivel 2 y 3 vía operadores `ltree`
    (`subpath(c.path, 0, N)`) en vez de string-split con `array_to_string`.
    `ltree` está indexado con GIST, lo que permite JOINs eficientes.

    Scope (siempre desde el JWT, nunca del query):
      - ver_todo=true  → ve todas las VPs (puede usar `vp_codigo` para filtrar UI).
      - vp_codigo X    → solo su VP X (vp_codigo del query se ignora si difiere).
      - planillas_extra → amplía cross-VP por planilla (Angel Flores / Salarios).
    """
    # Scope efectivo: el query NO puede ampliar lo que el JWT permite.
    effective_vp = vp_codigo if current.ver_todo else current.vp_codigo
    planillas_extra = current.planillas_extra or None
    scope_sql, scope_params = _alcance_avance_sql(effective_vp, planillas_extra)
    params: dict[str, Any] = {"anio": ciclo_anio, **scope_params}

    # `subpath(path, 0, N)` (ltree) → ancestro de nivel N. Como cn2/cn3 ya tienen
    # `nivel=N`, alcanza con la relación de ancestro: c.path = cnN.path o
    # c.path empieza con cnN.path + '.' (lo mismo que `c.path <@ cnN.path` en PG).
    sql = f"""
        SELECT
          i.codigo  AS item_codigo,
          i.descripcion AS item_desc,
          cn2.codigo AS cn2_codigo, cn2.descripcion AS cn2_desc,
          cn3.codigo AS cn3_codigo, cn3.descripcion AS cn3_desc,
          c.codigo  AS cuenta_codigo,
          c.descripcion AS cuenta_desc,
          CAST(SUM(l.monto_solicitado) AS FLOAT) AS total
        FROM planificacion.linea_solicitud l
        JOIN planificacion.solicitud s ON s.id = l.solicitud_id
        JOIN core.ciclo_presupuestario cp ON cp.id = s.ciclo_id
        JOIN catalogo.item_planificacion i ON i.id = l.item_id
        JOIN catalogo.cuenta_planificacion c ON c.id = l.cuenta_id
        LEFT JOIN catalogo.cuenta_planificacion cn2
               ON cn2.nivel = 2
              AND (c.path = cn2.path OR c.path LIKE cn2.path + '.%')
        LEFT JOIN catalogo.cuenta_planificacion cn3
               ON cn3.nivel = 3
              AND (c.path = cn3.path OR c.path LIKE cn3.path + '.%')
        WHERE cp.anio = :anio{scope_sql}
        GROUP BY i.codigo, i.descripcion, cn2.codigo, cn2.descripcion,
                 cn3.codigo, cn3.descripcion, c.codigo, c.descripcion
        ORDER BY i.codigo, cn2.codigo, cn3.codigo, c.codigo
    """
    stmt = _bind_expanding_pextra(text(sql), params)
    rows = (await db.execute(stmt, params)).mappings().all()
    return {
        "ciclo_anio": ciclo_anio,
        "vp_codigo": vp_codigo,
        "rows": [dict(r) for r in rows],
    }


@router.get("/avance/lineas")
async def avance_lineas(
    ciclo_anio: int,
    item_codigo: str,
    vp_codigo: str | None = None,
    # Filtros opcionales por nivel de cuenta. Si se pasa cuenta_imputable_codigo,
    # devuelve solo las líneas de esa cuenta hoja. Si solo se pasa cn2 o cn3,
    # devuelve todas las líneas cuya cuenta empieza con ese prefijo.
    cn2_codigo: str | None = None,
    cn3_codigo: str | None = None,
    cuenta_imputable_codigo: str | None = None,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Drill-down: devuelve las líneas individuales que componen un monto
    agregado de la vista de Avance. Scope siempre desde el JWT (ver `/avance`)."""
    effective_vp = vp_codigo if current.ver_todo else current.vp_codigo
    planillas_extra = current.planillas_extra or None
    scope_sql, scope_params = _alcance_avance_sql(effective_vp, planillas_extra)
    where = ["cp.anio = :anio", "i.codigo = :item"]
    params: dict[str, Any] = {"anio": ciclo_anio, "item": item_codigo, **scope_params}
    # Inyectamos scope_sql sin el AND (ya viene incluido en _alcance_avance_sql,
    # pero acá usamos lista de wheres, así que tomamos el contenido).
    if scope_sql:
        where.append(scope_sql.lstrip().removeprefix("AND ").strip())
    # Filtro por nivel de cuenta — ltree `<@` (descendiente o igual) emulado
    # con LIKE prefijo sobre la columna NVARCHAR `path`.
    if cuenta_imputable_codigo:
        where.append("c.codigo = :ckey")
        params["ckey"] = cuenta_imputable_codigo
    elif cn3_codigo or cn2_codigo:
        ancestro = cn3_codigo or cn2_codigo
        where.append(
            "EXISTS (SELECT 1 FROM catalogo.cuenta_planificacion cp_anc "
            "WHERE cp_anc.codigo = :ckey "
            "AND (c.path = cp_anc.path OR c.path LIKE cp_anc.path + '.%'))"
        )
        params["ckey"] = ancestro
    where_sql = " AND ".join(where)

    sql = f"""
        SELECT
          l.id, l.solicitud_id, s.nombre AS solicitud_nombre, s.vp_codigo,
          pt.codigo AS planilla_codigo, pt.nombre AS planilla_nombre,
          c.codigo AS cuenta_codigo, c.descripcion AS cuenta_desc,
          l.modalidad, l.parametros, l.justificacion,
          CAST(l.monto_solicitado AS FLOAT) AS monto,
          l.created_at, l.updated_at,
          uc.nombre + ' ' + uc.apellido AS creado_por
        FROM planificacion.linea_solicitud l
        JOIN planificacion.solicitud s ON s.id = l.solicitud_id
        JOIN core.ciclo_presupuestario cp ON cp.id = s.ciclo_id
        JOIN catalogo.item_planificacion i ON i.id = l.item_id
        JOIN catalogo.cuenta_planificacion c ON c.id = l.cuenta_id
        JOIN catalogo.planilla_template pt ON pt.id = l.planilla_template_id
        LEFT JOIN core.usuario uc ON uc.id = l.created_by
        WHERE {where_sql}
        ORDER BY l.created_at DESC
    """
    stmt = _bind_expanding_pextra(text(sql), params)
    rows = (await db.execute(stmt, params)).mappings().all()
    return {
        "ciclo_anio": ciclo_anio,
        "item_codigo": item_codigo,
        "scope": {"cn2": cn2_codigo, "cn3": cn3_codigo, "imputable": cuenta_imputable_codigo},
        "lineas": [dict(r) for r in rows],
        "total": sum(float(r["monto"] or 0) for r in rows),
    }
