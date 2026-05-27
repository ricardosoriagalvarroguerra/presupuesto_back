"""Endpoints de solicitudes presupuestarias — el archivo más cargado del backend.

Convive el CRUD de solicitudes y líneas con el workflow de aprobación, los
snapshots, observaciones y adjuntos. No lo partí en módulos más chicos
porque muchas funciones comparten los mismos helpers de lock y eventos —
separarlos forzaría a importar mucho ida y vuelta para poca ganancia.

Workflow:

  Etapa 0  Elaboración              cargadores de la VP
  Etapa 1  Revisión Vicepresidente  VP titular (PRE/GOB saltan a etapa 2)
  Etapa 2  Revisión Presidencia     Presidente o Jefe de Gabinete
  Etapa 3  Aprobado
  Etapa 4  Cerrado (administrativo)

En cada etapa hay 3 acciones: aprobar (sube), observar (vuelve a 0 con
observaciones abiertas), devolver (vuelve a 0 sin observaciones específicas).
Devolver u observar reconsume todo el ciclo: la solicitud tiene que pasar
otra vez por VP y después Presidencia.

Concurrencia: las operaciones de transición y los POST/PATCH/DELETE de líneas
toman `WITH (UPDLOCK, ROWLOCK)` sobre la fila de la solicitud antes de actuar.
Sin esto dos clicks paralelos a "Enviar a revisión" pueden ejecutar ambos, y
un POST de línea concurrente puede colar una línea en una solicitud que se
acaba de enviar (TOCTOU clásico). 
PD: Cabe recalcar que este workflow no es el definido, fue planteado de tal manera 
para mostrar la funcionalidad, se espera las necesidades levantadas por el grupo de trabajo.
"""
from decimal import Decimal
from typing import Any

import json
import logging
import os
import uuid as _uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.domain.authz import (
    alcance_solicitudes_sql,
    puede_acceder_linea,
    puede_acceder_planilla,
    puede_acceder_solicitud,
    puede_aprobar_presidencia,
    puede_aprobar_vp,
    puede_enviar_a_revision,
    vp_salta_revision_vp,
)
from app.domain.calculo import CalculoError, calcular_monto_linea
from app.security import CurrentUser, get_current_user
from app.domain.enums import (
    AccionEvento,
    ESTADOS_EDITABLES,
    EstadoWorkflow,
    puede_editar,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/planificacion", tags=["solicitudes"])


# ============================================================
# Schemas Pydantic
# ============================================================

class SolicitudCrear(BaseModel):
    ciclo_anio: int
    vp_codigo: str
    nombre: str
    # usuario_id: deprecado — la identidad viene del JWT. Aceptamos el campo
    # como opcional para compat hacia atrás con clientes viejos, pero lo IGNORAMOS.
    usuario_id: int | None = None


class LineaCrear(BaseModel):
    model_config = {"extra": "forbid"}  # rechaza campos no declarados (anti mass-assignment)
    planilla_template_id: int
    item_id: int
    cuenta_id: int
    plan_codigo: str = "PRESUPDEGASTOS"
    gestor_id: int | None = None
    modalidad: str  # 'parametrizada' | 'directa'
    formula_codigo: str | None = None
    parametros: dict[str, Any] = Field(default_factory=dict)
    monto_solicitado: Decimal = Field(ge=0, decimal_places=2)
    justificacion: str | None = None
    usuario_id: int | None = None  # deprecado — JWT manda


class LineaGrupoCrear(BaseModel):
    """N líneas que forman una sola unidad visual (una 'misión' descompuesta en
    pasajes/viáticos/hospedaje/etc.). Se crean en UNA transacción."""
    model_config = {"extra": "forbid"}
    lineas: list[LineaCrear] = Field(min_length=1, max_length=50)


class LineaPatch(BaseModel):
    """Patch del solicitante — solo modifica datos de su lado del workflow."""
    model_config = {"extra": "forbid"}
    monto_solicitado: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    justificacion: str | None = None
    parametros: dict[str, Any] | None = None
    estado_linea: str | None = None
    observacion: str | None = None
    usuario_id: int | None = None  # deprecado — JWT manda


class TransicionIn(BaseModel):
    accion: str
    comentario: str | None = None
    usuario_id: int | None = None  # deprecado — JWT manda


class SolicitudPatch(BaseModel):
    nombre: str | None = None
    usuario_id: int | None = None  # deprecado — JWT manda


# ============================================================
# Helpers
# ============================================================

async def _registrar_evento(
    db: AsyncSession,
    solicitud_id: int,
    accion: str,
    usuario_id: int,
    *,
    linea_id: int | None = None,
    etapa_anterior: int | None = None,
    etapa_nueva: int | None = None,
    estado_anterior: str | None = None,
    estado_nuevo: str | None = None,
    payload: dict[str, Any] | None = None,
    comentario: str | None = None,
) -> None:
    """Append-only en `planificacion.evento_solicitud` (audit log inmutable).

    NO hace commit — el caller controla la transacción. Eso es a propósito:
    si la operación que generó el evento falla después del INSERT, el rollback
    también borra el evento, así no quedan eventos auditando acciones que no
    se materializaron.

    `payload` se serializa a JSON (NVARCHAR(MAX) en MSSQL) — sirve para grabar
    detalles del request sin tener que agregar columnas. `default=str` cubre
    Decimal y fechas que el cliente puede mandar.
    """
    await db.execute(
        text(
            """INSERT INTO planificacion.evento_solicitud
               (solicitud_id, linea_id, accion, etapa_anterior, etapa_nueva,
                estado_anterior, estado_nuevo, payload, usuario_id, comentario)
               VALUES (:sid, :lid, :acc, :ea, :en, :sa, :sn,
                       :pl, :uid, :com)"""
        ),
        {
            "sid": solicitud_id, "lid": linea_id, "acc": accion,
            "ea": etapa_anterior, "en": etapa_nueva,
            "sa": estado_anterior, "sn": estado_nuevo,
            "pl": __import__("json").dumps(payload or {}, default=str),
            "uid": usuario_id, "com": comentario,
        },
    )


async def _crear_snapshot(
    db: AsyncSession,
    solicitud_id: int,
    etapa: int,
    motivo: str,
    usuario_id: int | None,
) -> int:
    """Congela el estado actual de la solicitud + sus líneas (snapshot inmutable).

    Se llama desde `transicion` en momentos clave del workflow: envío a
    revisión, devolución con observaciones, reaprobado post-ajustes,
    aprobación final por Presidencia.

    El propósito es reportería: los dashboards muestran "Solicitado vs
    Aprobado vs Final" comparando los snapshots de cada hito. Si una solicitud
    se aprueba y después se modifica (vía devolución u override admin),
    sin los snapshots se perderían los montos históricos.

    Los snapshots no se editan. Para corregir un snapshot equivocado, se
    genera uno nuevo (con motivo='correccion_manual' o similar).
    """
    snap_id = (await db.execute(
        text(
            """INSERT INTO planificacion.snapshot_solicitud
                  (solicitud_id, etapa, motivo, monto_total, created_by)
               OUTPUT INSERTED.id
               SELECT s.id, :et, :mot,
                      COALESCE(s.monto_total, 0), :uid
               FROM planificacion.solicitud s WHERE s.id = :s"""
        ),
        {"s": solicitud_id, "et": etapa, "mot": motivo, "uid": usuario_id},
    )).scalar()
    await db.execute(
        text(
            """INSERT INTO planificacion.snapshot_linea
                  (snapshot_id, linea_id, item_codigo, cuenta_codigo, plan_codigo,
                   parametros, monto_solicitado, monto_objetivos, monto_presidencia,
                   monto_directorio, justificacion)
               SELECT :snap, l.id, i.codigo, c.codigo, p.codigo, l.parametros,
                      l.monto_solicitado, l.monto_objetivos, l.monto_presidencia,
                      l.monto_directorio, l.justificacion
               FROM planificacion.linea_solicitud l
               LEFT JOIN catalogo.item_planificacion i ON i.id = l.item_id
               LEFT JOIN catalogo.cuenta_planificacion c ON c.id = l.cuenta_id
               LEFT JOIN catalogo.plan_presupuestario p ON p.id = l.plan_id
               WHERE l.solicitud_id = :s"""
        ),
        {"snap": snap_id, "s": solicitud_id},
    )
    return int(snap_id)


async def _recalc_total(db: AsyncSession, solicitud_id: int) -> None:
    """Recalcula `monto_total` y `monto_aprobado` sumando todas las líneas.

    Se llama después de cada POST/PATCH/DELETE de línea. Toma `WITH (UPDLOCK,
    ROWLOCK)` para serializar contra otros recálculos concurrentes — dos
    cargadores agregando líneas a la misma solicitud en paralelo pueden
    pisarse el total (lost update clásico) si no se lockea.

    El lock es liviano (una fila por solicitud, pocas operaciones concurrentes)
    y las subqueries del UPDATE son atómicas dentro de la transacción.

    `monto_aprobado` usa COALESCE(directorio, presidencia, objetivos, 0): el
    monto "vigente" depende de hasta dónde haya llegado el workflow. Cuando
    Presidencia aprueba se rellena `monto_presidencia` y eso pisa el aprobado.
    """
    # Lock la fila de la solicitud antes de leer-y-escribir.
    await db.execute(
        text("SELECT id FROM planificacion.solicitud WITH (UPDLOCK, ROWLOCK) WHERE id=:s"),
        {"s": solicitud_id},
    )
    await db.execute(
        text(
            """UPDATE planificacion.solicitud SET
                 monto_total    = COALESCE((SELECT SUM(monto_solicitado) FROM planificacion.linea_solicitud WITH (UPDLOCK, ROWLOCK) WHERE solicitud_id=:s), 0),
                 monto_aprobado = COALESCE((SELECT SUM(COALESCE(monto_directorio, monto_presidencia, monto_objetivos, 0))
                                            FROM planificacion.linea_solicitud WHERE solicitud_id=:s), 0)
               WHERE id=:s"""
        ),
        {"s": solicitud_id},
    )


# ============================================================
# Endpoints
# ============================================================

@router.post("/solicitudes")
async def crear_solicitud(p: SolicitudCrear, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    # Authz: solo ver_todo o usuarios de la misma VP pueden crear la solicitud
    # de esa VP. Cross-VP por planilla NO crea solicitudes (aporta líneas).
    if not current.ver_todo and current.vp_codigo != p.vp_codigo:
        raise HTTPException(
            403,
            f"Tu rol no puede crear solicitudes de {p.vp_codigo} (sos {current.vp_codigo or 'sin VP'}).",
        )
    ciclo_id = (await db.execute(
        text("SELECT id FROM core.ciclo_presupuestario WHERE anio=:a"),
        {"a": p.ciclo_anio},
    )).scalar()
    if not ciclo_id:
        raise HTTPException(404, f"Ciclo {p.ciclo_anio} no existe")

    # Regla de negocio: una sola solicitud por (ciclo, VP). Si ya existe,
    # devolvemos 409 con el id de la existente para que el frontend redirija.
    existente = (await db.execute(
        text("SELECT TOP 1 id, nombre FROM planificacion.solicitud WHERE ciclo_id=:cid AND vp_codigo=:vp"),
        {"cid": ciclo_id, "vp": p.vp_codigo},
    )).mappings().first()
    if existente:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "solicitud_ya_existe",
                "mensaje": f"Ya existe una solicitud para {p.vp_codigo} en el ciclo {p.ciclo_anio}.",
                "solicitud_id": existente["id"],
                "nombre": existente["nombre"],
            },
        )

    res = (await db.execute(
        text(
            """INSERT INTO planificacion.solicitud
                 (ciclo_id, vp_codigo, nombre, etapa_actual, estado_workflow, created_by)
               OUTPUT INSERTED.id
               VALUES (:cid, :vp, :nom, 0, 'en_elaboracion', :uid)"""
        ),
        {"cid": ciclo_id, "vp": p.vp_codigo, "nom": p.nombre, "uid": current.id},
    )).scalar_one()

    await _registrar_evento(
        db, res, "crear_solicitud", current.id,
        etapa_nueva=0, estado_nuevo="en_elaboracion",
        payload={"nombre": p.nombre, "vp_codigo": p.vp_codigo, "ciclo_anio": p.ciclo_anio},
    )
    await db.commit()
    return {"id": res, "ciclo_anio": p.ciclo_anio, "vp_codigo": p.vp_codigo, "nombre": p.nombre}


@router.get("/solicitudes")
async def listar_solicitudes(
    ciclo_anio: int | None = None,
    vp_codigo: str | None = None,
    limit: int = Query(default=500, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    # Scope viene del JWT (no del query) — evita BOLA donde un cliente
    # ajeno a la VP omite el filtro y ve todo.
    scope_sql, scope_params = await alcance_solicitudes_sql(db, current.id)
    sql = """
        SELECT s.id, cp.anio AS ciclo_anio, s.vp_codigo, s.nombre,
               s.etapa_actual, s.estado_workflow,
               s.monto_total, s.monto_aprobado,
               s.created_at, s.updated_at,
               u.nombre + ' ' + u.apellido AS creado_por,
               (SELECT COUNT(*) FROM planificacion.linea_solicitud WHERE solicitud_id=s.id) AS lineas_count
        FROM planificacion.solicitud s
        JOIN core.ciclo_presupuestario cp ON cp.id = s.ciclo_id
        LEFT JOIN core.usuario u ON u.id = s.created_by
        WHERE 1=1
    """
    sql += scope_sql
    params: dict[str, Any] = dict(scope_params)
    if ciclo_anio:
        sql += " AND cp.anio = :ca"; params["ca"] = ciclo_anio
    # vp_codigo del query es un filtro adicional (UX), nunca puede ampliar el
    # scope: si el user ya está limitado a una VP por su token, el AND extra es
    # idempotente; si pide otra, no devuelve nada.
    if vp_codigo:
        sql += " AND s.vp_codigo = :vp"; params["vp"] = vp_codigo
    sql += " ORDER BY s.updated_at DESC OFFSET :off ROWS FETCH NEXT :lim ROWS ONLY"
    params["lim"] = limit
    params["off"] = offset
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


@router.patch("/solicitudes/{sid}")
async def modificar_solicitud(sid: int, p: SolicitudPatch, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Cambia metadatos editables de la solicitud (hoy: nombre)."""
    ok, vp_sol = await puede_acceder_solicitud(db, current.id, sid)
    if vp_sol is None:
        raise HTTPException(404, "Solicitud no encontrada")
    if not ok:
        raise HTTPException(403, f"Tu rol no puede modificar solicitudes de {vp_sol}.")
    s = (await db.execute(
        text("SELECT id, estado_workflow, nombre FROM planificacion.solicitud WHERE id=:s"),
        {"s": sid},
    )).mappings().first()
    if s["estado_workflow"] not in ESTADOS_EDITABLES:
        raise HTTPException(409, f"No se puede editar en estado '{s['estado_workflow']}'")

    if p.nombre is not None and p.nombre.strip() and p.nombre != s["nombre"]:
        await db.execute(
            text("UPDATE planificacion.solicitud SET nombre=:n WHERE id=:s"),
            {"n": p.nombre.strip(), "s": sid},
        )
        await _registrar_evento(
            db, sid, "modificar_linea", current.id,
            payload={"campo": "nombre", "anterior": s["nombre"], "nuevo": p.nombre.strip()},
        )
    await db.commit()
    return {"id": sid, "nombre": p.nombre or s["nombre"]}


@router.get("/solicitudes/{sid}")
async def detalle_solicitud(sid: int, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    ok, vp_sol = await puede_acceder_solicitud(db, current.id, sid)
    if vp_sol is None:
        raise HTTPException(404, "Solicitud no encontrada")
    if not ok:
        raise HTTPException(403, f"Tu rol no puede ver solicitudes de {vp_sol}.")
    # Reducido de 4 queries secuenciales a 2: (1) solicitud + agregados via
    # subqueries; (2) líneas con joins. Eventos pasaron a su propio paginado.
    s = (await db.execute(
        text("""
            SELECT s.*,
                   cp.anio AS ciclo_anio,
                   u.nombre + ' ' + u.apellido AS creado_por,
                   COALESCE((SELECT COUNT(*) FROM planificacion.snapshot_solicitud
                               WHERE solicitud_id = s.id
                                 AND motivo = 'devuelto_con_observaciones'), 0) AS devoluciones,
                   COALESCE((SELECT COUNT(*) FROM planificacion.observacion
                               WHERE solicitud_id = s.id AND estado <> 'abierta'), 0) AS observaciones_resueltas
            FROM planificacion.solicitud s
            JOIN core.ciclo_presupuestario cp ON cp.id = s.ciclo_id
            LEFT JOIN core.usuario u ON u.id = s.created_by
            WHERE s.id = :s
        """),
        {"s": sid},
    )).mappings().first()
    if not s:
        raise HTTPException(404, "Solicitud no encontrada")

    # Filtro de líneas para usuarios cross-VP por planilla (caso Angel/Salarios):
    # si entran a una solicitud de otra VP, solo ven las líneas de SU planilla.
    # Para todos los demás (cargador propio, VP titular, Presidencia, ver_todo,
    # admin) no se filtra: ven todas las líneas.
    filtro_planilla = ""
    params_lineas: dict[str, Any] = {"s": sid}
    es_de_su_vp = (current.vp_codigo or "").upper() == (s["vp_codigo"] or "").upper()
    es_revisor_global = current.ver_todo or any(
        r in (current.roles or []) for r in ("presidente", "jefe_gabinete", "adm_sistema")
    )
    if current.planillas_extra and not es_de_su_vp and not es_revisor_global:
        filtro_planilla = " AND pt.codigo IN :pextra"
        params_lineas["pextra"] = current.planillas_extra

    stmt_lineas = text(f"""SELECT l.id, l.planilla_template_id, pt.codigo AS planilla_codigo, pt.nombre AS planilla_nombre,
                       l.item_id, i.codigo AS item_codigo, i.descripcion AS item_descripcion,
                       l.cuenta_id, c.codigo AS cuenta_codigo, c.descripcion AS cuenta_descripcion,
                       l.gestor_id, g.nombre AS gestor_nombre,
                       l.plan_id, p.codigo AS plan_codigo,
                       l.modalidad, l.formula_codigo, l.parametros,
                       l.monto_solicitado, l.monto_objetivos, l.monto_presidencia, l.monto_directorio,
                       l.justificacion, l.estado_linea, l.observacion,
                       l.created_at, l.updated_at,
                       uc.nombre + ' ' + uc.apellido AS created_by_nombre,
                       COALESCE(uu.nombre + ' ' + uu.apellido, uc.nombre + ' ' + uc.apellido) AS updated_by_nombre
                FROM planificacion.linea_solicitud l
                JOIN catalogo.planilla_template pt ON pt.id = l.planilla_template_id
                JOIN catalogo.item_planificacion i ON i.id = l.item_id
                JOIN catalogo.cuenta_planificacion c ON c.id = l.cuenta_id
                LEFT JOIN catalogo.gestor g ON g.id = l.gestor_id
                JOIN catalogo.plan_presupuestario p ON p.id = l.plan_id
                LEFT JOIN core.usuario uc ON uc.id = l.created_by
                LEFT JOIN core.usuario uu ON uu.id = l.updated_by
                WHERE l.solicitud_id=:s{filtro_planilla}
                ORDER BY l.id""")
    if "pextra" in params_lineas:
        # `IN :pextra` requiere bindparam(expanding=True) para que SQLAlchemy
        # materialice la lista como IN (?, ?, ?) ejecutable por pyodbc/aioodbc.
        stmt_lineas = stmt_lineas.bindparams(bindparam("pextra", expanding=True))
    lineas = (await db.execute(stmt_lineas, params_lineas)).mappings().all()

    eventos = (await db.execute(
        text("""SELECT TOP 100 e.*, u.nombre + ' ' + u.apellido AS usuario_nombre
                FROM planificacion.evento_solicitud e
                LEFT JOIN core.usuario u ON u.id = e.usuario_id
                WHERE e.solicitud_id=:s
                ORDER BY e.created_at DESC"""),
        {"s": sid},
    )).mappings().all()

    sol_dict = dict(s)
    sol_dict["ciclo_revision"] = int(sol_dict.pop("devoluciones", 0)) + 1
    sol_dict["observaciones_resueltas"] = int(sol_dict.get("observaciones_resueltas", 0))

    return {
        "solicitud": sol_dict,
        "lineas": [dict(r) for r in lineas],
        "eventos": [dict(r) for r in eventos],
    }


@router.get("/actividad-reciente")
async def actividad_reciente(
    limit: int = 20,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Eventos recientes de planificación para el feed del dashboard.

    Scope viene del JWT (no del query): si ver_todo o tiene planillas_extra,
    ve todos; si tiene VP propia, solo eventos de solicitudes de su VP.
    """
    params: dict[str, Any] = {"lim": min(max(limit, 1), 100)}
    scope_sql, scope_params = await alcance_solicitudes_sql(db, current.id)
    params.update(scope_params)
    # scope_sql viene con prefijo " AND ...". En este endpoint armamos WHERE
    # nosotros mismos, así que normalizamos.
    where_sql = ""
    if scope_sql.strip():
        where_sql = "WHERE " + scope_sql.strip().removeprefix("AND ").strip()
    rows = (await db.execute(
        text(
            f"""SELECT TOP (:lim) e.id, e.solicitud_id, e.linea_id, e.accion, e.payload,
                       e.estado_anterior, e.estado_nuevo,
                       e.etapa_anterior, e.etapa_nueva,
                       e.created_at, e.comentario,
                       u.nombre AS usuario_nombre, u.apellido AS usuario_apellido,
                       s.vp_codigo, cp.anio AS ciclo_anio, s.nombre AS solicitud_nombre,
                       l.planilla_template_id,
                       pt.nombre AS planilla_nombre,
                       c.codigo AS cuenta_codigo, c.descripcion AS cuenta_descripcion
                  FROM planificacion.evento_solicitud e
                  JOIN planificacion.solicitud s ON s.id = e.solicitud_id
                  JOIN core.ciclo_presupuestario cp ON cp.id = s.ciclo_id
                  LEFT JOIN core.usuario u ON u.id = e.usuario_id
                  LEFT JOIN planificacion.linea_solicitud l ON l.id = e.linea_id
                  LEFT JOIN catalogo.planilla_template pt ON pt.id = l.planilla_template_id
                  LEFT JOIN catalogo.cuenta_planificacion c ON c.id = l.cuenta_id
                  {where_sql}
                  ORDER BY e.created_at DESC"""
        ),
        params,
    )).mappings().all()
    return [dict(r) for r in rows]


async def _insertar_linea(
    db: AsyncSession,
    sid: int,
    vp_codigo: str,
    p: LineaCrear,
    current_id: int,
) -> int:
    """Inserta UNA línea y registra el evento — SIN commit ni `_recalc_total`.

    Reúne authz por planilla, validación de la matriz item↔cuenta, resolución de
    gestor y cálculo autoritativo del monto. El llamador debe ejecutar
    `_recalc_total` + `db.commit()`. Diferir el commit permite que el endpoint de
    grupo cree N líneas en UNA transacción: si una falla, ninguna persiste.
    """
    # Authz unificado (helper único — drift impossible)
    pt_codigo = (await db.execute(
        text("SELECT codigo FROM catalogo.planilla_template WHERE id=:t"),
        {"t": p.planilla_template_id},
    )).scalar()
    if not pt_codigo:
        raise HTTPException(400, f"Planilla {p.planilla_template_id} no existe")
    if not await puede_acceder_planilla(db, current_id, vp_codigo, pt_codigo):
        raise HTTPException(
            status_code=403,
            detail=f"Tu rol no puede agregar líneas a la planilla '{pt_codigo}' "
                   f"en una solicitud de {vp_codigo}.",
        )

    # Validar matriz item↔cuenta — RECHAZA combinaciones no listadas en catalogo.relacion_item_cuenta
    # (346 pares oficiales DPP). El frontend ya filtra opciones; este es el guard duro de servidor.
    # MSSQL no permite `EXISTS(...)` como expresión escalar — usamos CASE.
    chk = (await db.execute(text("""
        SELECT i.codigo AS item_codigo, c.codigo AS cuenta_codigo,
               CASE WHEN EXISTS(SELECT 1 FROM catalogo.relacion_item_cuenta r
                                WHERE r.item_id=:i AND r.cuenta_id=:c)
                    THEN 1 ELSE 0 END AS valida
        FROM catalogo.item_planificacion i, catalogo.cuenta_planificacion c
        WHERE i.id=:i AND c.id=:c
    """), {"i": p.item_id, "c": p.cuenta_id})).mappings().first()
    if not chk:
        raise HTTPException(400, f"Item {p.item_id} o cuenta {p.cuenta_id} no existen.")
    if not chk["valida"]:
        raise HTTPException(
            400,
            f"Combinación inválida: item '{chk['item_codigo']}' no puede imputar a cuenta "
            f"'{chk['cuenta_codigo']}' según la matriz oficial item↔cuenta.",
        )

    plan_id = (await db.execute(
        text("SELECT id FROM catalogo.plan_presupuestario WHERE codigo=:c"),
        {"c": p.plan_codigo},
    )).scalar()
    if not plan_id:
        raise HTTPException(400, f"Plan {p.plan_codigo} no existe")

    # Si el frontend no nos pasó gestor_id, resolvemos automáticamente desde gestor_item.
    # Para items hijos (02.03.05), heredamos del padre directo si no hay entry propia.
    gestor_id_final = p.gestor_id
    if gestor_id_final is None:
        gestor_id_final = (await db.execute(
            text("""
                SELECT COALESCE(
                  (SELECT TOP 1 gi.gestor_id FROM catalogo.gestor_item gi WHERE gi.item_id = :i),
                  (SELECT gi.gestor_id FROM catalogo.gestor_item gi
                     WHERE gi.item_id = (SELECT TOP 1 parent_id FROM catalogo.item_planificacion WHERE id = :i))
                )
            """),
            {"i": p.item_id},
        )).scalar()

    # AUTORIDAD DEL BACKEND: recalculamos `monto_solicitado` desde los parámetros,
    # la cuenta destino y las tarifas oficiales (catalogo.tarifa_*). El monto del
    # payload del cliente es solo un hint para captura directa; si la línea es
    # parametrizada y la tarifa no existe, `calcular_monto_linea` lanza
    # CalculoError → 422 (no se acepta el hint del cliente).
    try:
        monto_calculado = await calcular_monto_linea(
            db,
            planilla_codigo=pt_codigo,
            cuenta_codigo=chk["cuenta_codigo"],
            parametros=p.parametros,
            monto_hint=p.monto_solicitado,
        )
    except CalculoError as e:
        raise HTTPException(422, str(e))

    nueva_id = (await db.execute(
        text("""INSERT INTO planificacion.linea_solicitud
                  (solicitud_id, planilla_template_id, item_id, cuenta_id, gestor_id, plan_id,
                   modalidad, formula_codigo, parametros,
                   monto_solicitado, justificacion, created_by)
                OUTPUT INSERTED.id
                VALUES (:s, :pt, :i, :c, :g, :pl,
                        :mod, :fc, :pr,
                        :ms, :j, :u)"""),
        {"s": sid, "pt": p.planilla_template_id, "i": p.item_id, "c": p.cuenta_id,
         "g": gestor_id_final, "pl": plan_id, "mod": p.modalidad, "fc": p.formula_codigo,
         "pr": json.dumps(p.parametros), "ms": monto_calculado, "j": p.justificacion,
         "u": current_id},
    )).scalar_one()

    await _registrar_evento(
        db, sid, "agregar_linea", current_id, linea_id=nueva_id,
        payload={"monto_solicitado": str(monto_calculado), "item_id": p.item_id, "cuenta_id": p.cuenta_id,
                 "hint_cliente": str(p.monto_solicitado)},
    )
    return int(nueva_id)


@router.post("/solicitudes/{sid}/lineas")
async def agregar_linea(sid: int, p: LineaCrear, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    #: bloquea la fila de la solicitud durante todo este POST. Si en
    # paralelo alguien ejecutó una transición (p.ej. "Enviar al VP"), nuestra
    # lectura espera al COMMIT, vemos el estado actualizado y el check siguiente
    # rechaza con 409 — sin esto un cargador podía colar líneas en una solicitud
    # ya enviada a revisión.
    s = (await db.execute(
        text("SELECT id, estado_workflow, vp_codigo FROM planificacion.solicitud WITH (UPDLOCK, ROWLOCK) WHERE id=:s"),
        {"s": sid},
    )).mappings().first()
    if not s:
        raise HTTPException(404, "Solicitud no encontrada")
    if s["estado_workflow"] not in ESTADOS_EDITABLES:
        raise HTTPException(409, f"No se puede agregar líneas en estado '{s['estado_workflow']}'")

    nueva_id = await _insertar_linea(db, sid, s["vp_codigo"], p, current.id)
    await _recalc_total(db, sid)
    await db.commit()
    return {"id": nueva_id, "solicitud_id": sid}


@router.post("/solicitudes/{sid}/lineas-grupo")
async def agregar_lineas_grupo(sid: int, p: LineaGrupoCrear, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Crea N líneas (una 'misión' descompuesta por cuenta) en UNA transacción.

    Atomicidad: si la creación de cualquier línea falla (combinación inválida,
    tarifa faltante, authz), se aborta TODO el grupo — ninguna línea persiste.
    Evita las 'misiones partidas' que dejaba el frontend al crear las líneas
    una por una con un loop sin rollback.
    """
    s = (await db.execute(
        text("SELECT id, estado_workflow, vp_codigo FROM planificacion.solicitud WITH (UPDLOCK, ROWLOCK) WHERE id=:s"),
        {"s": sid},
    )).mappings().first()
    if not s:
        raise HTTPException(404, "Solicitud no encontrada")
    if s["estado_workflow"] not in ESTADOS_EDITABLES:
        raise HTTPException(409, f"No se puede agregar líneas en estado '{s['estado_workflow']}'")

    ids: list[int] = []
    for linea in p.lineas:
        ids.append(await _insertar_linea(db, sid, s["vp_codigo"], linea, current.id))
    await _recalc_total(db, sid)
    await db.commit()
    return {"solicitud_id": sid, "ids": ids}


@router.patch("/lineas/{lid}")
async def modificar_linea(lid: int, p: LineaPatch, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    ok, vp_sol = await puede_acceder_linea(db, current.id, lid)
    if vp_sol is None:
        raise HTTPException(404, "Línea no encontrada")
    if not ok:
        raise HTTPException(403, f"Tu rol no puede modificar líneas de {vp_sol}.")
    actual = (await db.execute(
        text("SELECT * FROM planificacion.linea_solicitud WITH (UPDLOCK, ROWLOCK) WHERE id=:l"),
        {"l": lid},
    )).mappings().first()

    # Guard: solo permitir modificar líneas en estados editables del workflow.
    # serializa contra transiciones concurrentes (mismo motivo que
    # agregar_linea — evita lost update / TOCTOU si alguien envió a revisión
    # entre que el cliente leyó y mandó el PATCH).
    estado = (await db.execute(
        text("SELECT estado_workflow FROM planificacion.solicitud WITH (UPDLOCK, ROWLOCK) WHERE id=:s"),
        {"s": actual["solicitud_id"]},
    )).scalar()
    if estado not in ESTADOS_EDITABLES:
        raise HTTPException(409, f"No se puede modificar líneas en estado '{estado}'. "
                                  "El presupuesto aprobado queda congelado.")

    # Solo se modifican los campos del schema LineaPatch (solicitante).
    # Los montos por etapa (vp/presidencia) los mueve el workflow de transiciones,
    # no este PATCH.
    cambios: dict[str, Any] = {}
    for k in ("monto_solicitado", "justificacion"):
        v = getattr(p, k, None)
        if v is not None and v != actual.get(k):
            cambios[k] = v
    if p.parametros is not None:
        cambios["parametros"] = p.parametros

    # AUTORIDAD DEL BACKEND: si la línea es parametrizada y cambian los parámetros
    # (o si vino un monto del cliente), recalculamos desde tarifas oficiales en
    # lugar de confiar en el hint. Mismo principio que en POST /lineas — sin esto,
    # un request manipulado podría inflar el monto de una línea de Misiones.
    if actual.get("modalidad") == "parametrizada" and ("parametros" in cambios or "monto_solicitado" in cambios):
        pt_codigo = (await db.execute(
            text("SELECT codigo FROM catalogo.planilla_template WHERE id=:t"),
            {"t": actual["planilla_template_id"]},
        )).scalar()
        cuenta_codigo = (await db.execute(
            text("SELECT codigo FROM catalogo.cuenta_planificacion WHERE id=:c"),
            {"c": actual["cuenta_id"]},
        )).scalar()
        # Param efectivo = los actuales + override del PATCH si vinieron.
        parametros_efectivos = dict(actual.get("parametros") or {})
        if "parametros" in cambios:
            parametros_efectivos.update(cambios["parametros"])
        try:
            monto_calc = await calcular_monto_linea(
                db,
                planilla_codigo=pt_codigo or "",
                cuenta_codigo=cuenta_codigo or "",
                parametros=parametros_efectivos,
                monto_hint=cambios.get("monto_solicitado", actual.get("monto_solicitado")),
            )
        except CalculoError as e:
            raise HTTPException(422, str(e))
        cambios["monto_solicitado"] = monto_calc

    if not cambios:
        return {"id": lid, "cambios": 0}

    sets = []
    params: dict[str, Any] = {"l": lid, "u": current.id}
    for k, v in cambios.items():
        if k == "parametros":
            # `parametros` es NVARCHAR(MAX) en MSSQL — basta con el JSON serializado.
            sets.append(f"{k} = :p_{k}")
            import json
            params[f"p_{k}"] = json.dumps(v)
        else:
            sets.append(f"{k} = :p_{k}")
            params[f"p_{k}"] = v
    sets.append("updated_by = :u")

    await db.execute(
        text(f"UPDATE planificacion.linea_solicitud SET {', '.join(sets)} WHERE id=:l"),
        params,
    )

    await _registrar_evento(
        db, actual["solicitud_id"], "modificar_linea", current.id,
        linea_id=lid,
        payload={"cambios": {k: str(v) for k, v in cambios.items()}},
        comentario=p.observacion,
    )
    await _recalc_total(db, actual["solicitud_id"])
    await db.commit()
    return {"id": lid, "cambios": len(cambios)}


@router.delete("/lineas/{lid}")
async def eliminar_linea(lid: int, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    ok, vp_sol = await puede_acceder_linea(db, current.id, lid)
    if vp_sol is None:
        raise HTTPException(404, "Línea no encontrada")
    if not ok:
        raise HTTPException(403, f"Tu rol no puede eliminar líneas de {vp_sol}.")
    actual = (await db.execute(
        text("SELECT * FROM planificacion.linea_solicitud WITH (UPDLOCK, ROWLOCK) WHERE id=:l"),
        {"l": lid},
    )).mappings().first()

    # Guard: no permitir eliminar líneas de solicitudes ya aprobadas/cerradas
    # serializa contra transiciones concurrentes.
    estado = (await db.execute(
        text("SELECT estado_workflow FROM planificacion.solicitud WITH (UPDLOCK, ROWLOCK) WHERE id=:s"),
        {"s": actual["solicitud_id"]},
    )).scalar()
    if estado not in ESTADOS_EDITABLES:
        raise HTTPException(409, f"No se puede eliminar líneas en estado '{estado}'.")

    await db.execute(text("DELETE FROM planificacion.linea_solicitud WITH (UPDLOCK, ROWLOCK) WHERE id=:l"), {"l": lid})
    await _registrar_evento(
        db, actual["solicitud_id"], "eliminar_linea", current.id,
        payload={"item_id": actual["item_id"], "cuenta_id": actual["cuenta_id"], "monto": str(actual["monto_solicitado"])},
    )
    await _recalc_total(db, actual["solicitud_id"])
    await db.commit()
    return {"id": lid, "deleted": True}


@router.delete("/solicitudes/{sid}/lineas-grupo/{grupo_id}")
async def eliminar_lineas_grupo(sid: int, grupo_id: str, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Borra todas las líneas de un grupo (una 'misión') en UNA transacción.

    Atomicidad: el DELETE es una sola sentencia — o se borran todas las líneas
    del grupo o ninguna. Evita las 'misiones partidas' que dejaba el frontend al
    borrar las líneas una por una con un loop sin rollback.
    """
    s = (await db.execute(
        text("SELECT estado_workflow, vp_codigo FROM planificacion.solicitud WHERE id=:s"),
        {"s": sid},
    )).mappings().first()
    if not s:
        raise HTTPException(404, "Solicitud no encontrada")
    if s["estado_workflow"] not in ESTADOS_EDITABLES:
        raise HTTPException(409, f"No se puede eliminar líneas en estado '{s['estado_workflow']}'.")

    # `grupo_id` vive dentro de parametros (JSONB). Resolvemos las líneas del grupo.
    lineas = (await db.execute(
        text("""SELECT l.id, l.item_id, l.cuenta_id, l.monto_solicitado, pt.codigo AS planilla
                FROM planificacion.linea_solicitud l
                JOIN catalogo.planilla_template pt ON pt.id = l.planilla_template_id
                WHERE l.solicitud_id = :s AND JSON_VALUE(l.parametros, '$.grupo_id') = :g"""),
        {"s": sid, "g": grupo_id},
    )).mappings().all()
    if not lineas:
        raise HTTPException(404, "Grupo de líneas no encontrado")

    # Authz: el usuario debe poder acceder a cada planilla involucrada en el grupo.
    for planilla in {l["planilla"] for l in lineas}:
        if not await puede_acceder_planilla(db, current.id, s["vp_codigo"], planilla):
            raise HTTPException(
                status_code=403,
                detail=f"Tu rol no puede eliminar líneas de la planilla '{planilla}' "
                       f"en una solicitud de {s['vp_codigo']}.",
            )

    await db.execute(
        text("DELETE FROM planificacion.linea_solicitud "
             "WHERE solicitud_id = :s AND JSON_VALUE(parametros, '$.grupo_id') = :g"),
        {"s": sid, "g": grupo_id},
    )
    for l in lineas:
        await _registrar_evento(
            db, sid, "eliminar_linea", current.id,
            payload={"item_id": l["item_id"], "cuenta_id": l["cuenta_id"],
                     "monto": str(l["monto_solicitado"]), "grupo_id": grupo_id},
        )
    await _recalc_total(db, sid)
    await db.commit()
    return {"solicitud_id": sid, "grupo_id": grupo_id, "deleted": len(lineas)}


# ────────────────────────────────────────────────────────────────────────────
# Mapa de transiciones permitidas — el cerebro del workflow.
# ────────────────────────────────────────────────────────────────────────────
#
# Una entrada = una "acción" que el frontend puede mandar. La acción define:
#   - desde qué etapa puede dispararse (`de_etapa`)
#   - desde qué estados (`estados_validos`)
#   - a qué estado/etapa va (`estado_destino`, `etapa_destino`)
#   - qué columna de timestamp setear (`timestamp_col`)
#   - qué rol valida la acción (`rbac`: 'enviar' | 'vp' | 'presidencia')
#   - si la acción "congela" monto (copiar de un campo a otro)
#
# Etapas:
#   0 Elaboración        cargadores trabajan
#   1 Revisión VP        Vicepresidente titular acepta/observa/devuelve
#                        (PRE y GOB saltan esta etapa — ver `solo_vp_sin_vicepresidente`)
#   2 Revisión Presid.   Presidenta o Jefe de Gabinete acepta/observa/devuelve
#   3 Aprobado           ya está firme; va al pre-cierre administrativo
#   4 Cerrado            inmutable, solo entra al histórico
#
# Estados que permiten editar líneas (ESTADOS_EDITABLES en domain/enums.py):
#   en_elaboracion, observado_vp, devuelto_vp,
#   observado_presidencia, devuelto_presidencia.
# Cualquier otro estado bloquea POST/PATCH/DELETE de líneas (lo chequea el
# endpoint de cada operación antes de tocar nada).
#
# Congelado de montos:
#   Cuando una etapa aprueba, el monto vigente se copia a un campo separado
#   (`monto_vp`, `monto_presidencia`) y queda inmutable. Si después se devuelve
#   la solicitud por observaciones y el cargador edita `monto_solicitado`,
#   el monto aprobado original sigue intacto en su campo.
#
# Estados legacy: el enum acepta `enviado_revision`, `en_revision`, `validado`,
# `observado`, `devuelto` por compatibilidad con datos viejos. El código nuevo
# no los emite — si ves una solicitud con esos estados es histórica.
TRANSICIONES: dict[str, dict[str, Any]] = {
    # --- ETAPA 0 → 1: cargadores envían al VP ----------------------------
    "enviar_a_revision_vp": {
        "de_etapa": [0],
        "estados_validos": [
            "en_elaboracion",
            "observado_vp", "devuelto_vp",
            "observado_presidencia", "devuelto_presidencia",
        ],
        "estado_destino": "en_revision_vp",
        "etapa_destino": 1,
        "timestamp_col": "enviado_a_revision_at",
        "rbac": "enviar",
    },
    # --- ETAPA 0 → 2: cargadores de PRE/GOB saltan VP --------------------
    "enviar_a_revision_presidencia": {
        "de_etapa": [0],
        "estados_validos": [
            "en_elaboracion",
            "observado_presidencia", "devuelto_presidencia",
        ],
        "estado_destino": "en_revision_presidencia",
        "etapa_destino": 2,
        "timestamp_col": "enviado_a_revision_at",
        "rbac": "enviar",
        "solo_vp_sin_vicepresidente": True,
    },
    # --- ETAPA 1: el VP titular responde ----------------------------------
    "aprobar_vp": {
        "de_etapa": [1],
        "estados_validos": ["en_revision_vp"],
        "estado_destino": "en_revision_presidencia",
        "etapa_destino": 2,
        "timestamp_col": "aprobado_vp_at",
        "congelar_origen": "monto_solicitado",
        "congelar_destino": "monto_vp",
        "rbac": "vp",
    },
    "observar_vp": {
        "de_etapa": [1],
        "estados_validos": ["en_revision_vp"],
        "estado_destino": "observado_vp",
        "etapa_destino": 0,
        "rbac": "vp",
    },
    "devolver_vp": {
        "de_etapa": [1],
        "estados_validos": ["en_revision_vp"],
        "estado_destino": "devuelto_vp",
        "etapa_destino": 0,
        "rbac": "vp",
    },
    # --- ETAPA 2: Presidencia / Jefe Gabinete responden -------------------
    "aprobar_presidencia": {
        "de_etapa": [2],
        "estados_validos": ["en_revision_presidencia"],
        "estado_destino": "aprobado",
        "etapa_destino": 3,
        "timestamp_col": "aprobado_presidencia_at",
        # Si la solicitud pasó por VP, congelamos desde monto_vp.
        # Si saltó (PRE/GOB), congelamos desde monto_solicitado.
        "congelar_origen": "COALESCE(monto_vp, monto_solicitado)",
        "congelar_destino": "monto_presidencia",
        "rbac": "presidencia",
    },
    "observar_presidencia": {
        "de_etapa": [2],
        "estados_validos": ["en_revision_presidencia"],
        "estado_destino": "observado_presidencia",
        "etapa_destino": 0,
        "rbac": "presidencia",
    },
    "devolver_presidencia": {
        "de_etapa": [2],
        "estados_validos": ["en_revision_presidencia"],
        "estado_destino": "devuelto_presidencia",
        "etapa_destino": 0,
        "rbac": "presidencia",
    },
    # --- ETAPA 3 → 4: cierre administrativo -------------------------------
    "cerrar": {
        "de_etapa": [3],
        "estados_validos": ["aprobado"],
        "estado_destino": "cerrado",
        "etapa_destino": 4,
        "rbac": "presidencia",
    },
}


def _verifica_rbac(
    accion_rbac: str,
    current: CurrentUser,
    vp_solicitud: str,
) -> None:
    """Valida que el usuario pueda ejecutar el tipo de transición indicado.

    Tipos:
      - 'enviar'      → cargador de la VP (vp_codigo coincide).
      - 'vp'          → VP titular de esa VP.
      - 'presidencia' → Presidente o Jefe de Gabinete.
    """
    roles = current.roles or []
    if accion_rbac == "enviar":
        if not puede_enviar_a_revision(roles, current.vp_codigo, vp_solicitud):
            raise HTTPException(
                403,
                f"Tu rol no puede enviar a revisión solicitudes de {vp_solicitud}.",
            )
    elif accion_rbac == "vp":
        if not puede_aprobar_vp(roles, current.vp_codigo, vp_solicitud):
            raise HTTPException(
                403,
                f"Solo el Vicepresidente de {vp_solicitud} puede aprobar/observar/devolver en esta etapa.",
            )
    elif accion_rbac == "presidencia":
        if not puede_aprobar_presidencia(roles):
            raise HTTPException(
                403,
                "Solo Presidencia o Jefe de Gabinete pueden actuar en esta etapa.",
            )


@router.post("/solicitudes/{sid}/transicion")
async def transicion(sid: int, p: TransicionIn, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Dispara una transición del workflow.

    Esta función concentra MUCHO: lee la solicitud con lock, valida etapa/estado/
    rol, ejecuta la transición, congela montos si corresponde, registra el evento,
    y genera snapshot. Si algo falla todo queda dentro de una transacción y se
    revierte al final.

    Orden de los chequeos (importa):
      1) Authz general: ¿puede tocar esta solicitud? (404/403)
      2) Lock + lectura del estado actual
      3) Acción válida en el mapa
      4) Etapa actual habilita la acción
      5) Estado actual habilita la acción
      6) RBAC: el rol del usuario puede ejecutar este TIPO de acción
      7) Reglas especiales (PRE/GOB no usa enviar_a_revision_vp)
      8) Observaciones abiertas: bloquean reenvíos, exigen 1+ para observar
      9) UPDATE solicitud (estado, etapa, timestamp)
     10) Congelar monto si corresponde (copia monto_X → campo destino en líneas)
     11) Registrar evento
     12) Snapshot automático si la transición es un hito
    """
    ok, vp_sol = await puede_acceder_solicitud(db, current.id, sid)
    if vp_sol is None:
        raise HTTPException(404, "Solicitud no encontrada")
    if not ok:
        raise HTTPException(403, f"Tu rol no puede ejecutar transiciones sobre solicitudes de {vp_sol}.")
    # Lock pesimista: serializa esta transición contra otras transiciones y
    # contra POST/PATCH/DELETE de líneas concurrentes. Sin esto, dos clicks
    # en "Enviar a revisión" pueden ejecutarse ambos (evento duplicado), y un
    # POST de línea entrando justo cuando otra request envió la solicitud
    # puede dejar la línea en una solicitud ya "en revisión" (estado no
    # editable). `WITH (UPDLOCK, ROWLOCK)` es el equivalente MSSQL del
    # `FOR UPDATE` de PG.
    s = (await db.execute(
        text("SELECT * FROM planificacion.solicitud WITH (UPDLOCK, ROWLOCK) WHERE id=:s"),
        {"s": sid},
    )).mappings().first()

    cfg = TRANSICIONES.get(p.accion)
    if not cfg:
        raise HTTPException(400, f"Acción '{p.accion}' no soportada")

    etapa_actual = s["etapa_actual"]
    estado_actual = s["estado_workflow"]
    if etapa_actual not in cfg["de_etapa"]:
        raise HTTPException(409, f"No se puede ejecutar '{p.accion}' desde la etapa {etapa_actual}")
    estados_validos = cfg.get("estados_validos")
    if estados_validos and estado_actual not in estados_validos:
        raise HTTPException(
            409,
            f"No se puede ejecutar '{p.accion}' desde el estado '{estado_actual}'. "
            f"Estados permitidos: {', '.join(estados_validos)}",
        )

    # RBAC: la acción puede ser legítima por estado pero ilegal por rol.
    _verifica_rbac(cfg["rbac"], current, vp_sol)

    # Solo PRE/GOB pueden usar enviar_a_revision_presidencia (saltan al VP).
    if cfg.get("solo_vp_sin_vicepresidente") and not vp_salta_revision_vp(vp_sol):
        raise HTTPException(
            409,
            f"La solicitud de {vp_sol} debe pasar por la revisión del Vicepresidente "
            "antes de Presidencia. Usá 'enviar_a_revision_vp'.",
        )

    # Validaciones específicas del flujo de revisión con observaciones:
    #   - cualquier "observar_*" requiere al menos una observación abierta.
    #   - cualquier "enviar_*" desde estado observado/devuelto requiere que
    #     TODAS las observaciones del último ciclo estén resueltas.
    acciones_observar = {"observar_vp", "observar_presidencia"}
    acciones_enviar = {"enviar_a_revision_vp", "enviar_a_revision_presidencia"}
    estados_post_devolucion = {
        "observado_vp", "devuelto_vp",
        "observado_presidencia", "devuelto_presidencia",
    }

    if p.accion in acciones_observar:
        n_abiertas = (await db.execute(
            text("""SELECT COUNT(*) FROM planificacion.observacion
                    WHERE solicitud_id=:s AND estado='abierta'"""),
            {"s": sid},
        )).scalar() or 0
        if n_abiertas == 0:
            raise HTTPException(
                400,
                "Para devolver con observaciones tenés que crear al menos una observación abierta.",
            )
    if p.accion in acciones_enviar and estado_actual in estados_post_devolucion:
        n_abiertas = (await db.execute(
            text("""SELECT COUNT(*) FROM planificacion.observacion
                    WHERE solicitud_id=:s AND estado='abierta'"""),
            {"s": sid},
        )).scalar() or 0
        if n_abiertas > 0:
            raise HTTPException(
                400,
                f"Hay {n_abiertas} observación(es) sin resolver. Aplicalas o rechazalas antes de reenviar.",
            )

    sets = ["estado_workflow = :est"]
    params: dict[str, Any] = {"s": sid, "est": cfg["estado_destino"]}
    if cfg["etapa_destino"] is not None:
        sets.append("etapa_actual = :etapa")
        params["etapa"] = cfg["etapa_destino"]
    if cfg.get("timestamp_col"):
        sets.append(f"{cfg['timestamp_col']} = SYSDATETIMEOFFSET()")
    if p.comentario:
        sets.append("comentario_actual = :com")
        params["com"] = p.comentario

    await db.execute(
        text(f"UPDATE planificacion.solicitud SET {', '.join(sets)} WHERE id=:s"),
        params,
    )

    # Congelar montos al aprobar una etapa. Copiamos el monto "vigente" a un
    # campo específico de la etapa (`monto_vp`, `monto_presidencia`) para que
    # quede inmutable. Si después el flujo se devuelve por observaciones y el
    # cargador edita `monto_solicitado`, el monto que se firmó en cada etapa
    # sigue accesible en su campo correspondiente.
    #
    # `congelar_origen` puede ser:
    #   - nombre de columna ("monto_solicitado") → lo envolvemos en COALESCE.
    #   - expresión SQL ("COALESCE(monto_vp, monto_solicitado)") → para los
    #     casos donde la VP saltó etapa 1 y `monto_vp` está NULL: el COALESCE
    #     cae a `monto_solicitado`.
    origen = cfg.get("congelar_origen")
    destino = cfg.get("congelar_destino")
    if origen and destino:
        # Heurística para distinguir "nombre de columna" de "expresión":
        # si tiene paréntesis o espacios es expresión. En cualquier caso
        # envolvemos en COALESCE(_, 0) — los NULLs en estos campos rompen
        # los SUM() de la reportería.
        if "(" in origen or " " in origen:
            origen_expr = f"COALESCE({origen}, 0)"
        else:
            origen_expr = f"COALESCE({origen}, 0)"
        await db.execute(
            text(f"""UPDATE planificacion.linea_solicitud
                     SET {destino} = {origen_expr}
                     WHERE solicitud_id = :s"""),
            {"s": sid},
        )
        # Y recalculamos el monto_aprobado total con los valores recién congelados.
        await _recalc_total(db, sid)

    await _registrar_evento(
        db, sid, p.accion, current.id,
        etapa_anterior=etapa_actual, etapa_nueva=cfg.get("etapa_destino"),
        estado_anterior=s["estado_workflow"], estado_nuevo=cfg["estado_destino"],
        comentario=p.comentario,
    )

    # Snapshots automáticos en hitos clave (para comparativos en reportería).
    motivo_snap: str | None = None
    if p.accion in ("enviar_a_revision_vp", "enviar_a_revision_presidencia"):
        # Primer envío vs reenvío tras observaciones (de cualquier nivel).
        motivo_snap = (
            "reaprobado_post_ajustes"
            if estado_actual in (
                "observado_vp", "devuelto_vp",
                "observado_presidencia", "devuelto_presidencia",
            )
            else "enviado_revision"
        )
    elif p.accion in ("observar_vp", "observar_presidencia"):
        motivo_snap = "devuelto_con_observaciones"
    elif p.accion == "aprobar_presidencia":
        # Reusa el motivo legacy "aprobado_directorio" del enum snapshot_motivo
        # (semánticamente: "aprobado final por la autoridad superior").
        motivo_snap = "aprobado_directorio"
    # aprobar_vp no genera snapshot automático — es un paso intermedio. Si en
    # el futuro hace falta auditarlo, agregar valor "aprobado_vp" al enum
    # planificacion.snapshot_motivo en una migración nueva.
    if motivo_snap:
        etapa_snap = cfg.get("etapa_destino") if cfg.get("etapa_destino") is not None else etapa_actual
        snap_id = await _crear_snapshot(db, sid, etapa_snap, motivo_snap, current.id)
        await _registrar_evento(
            db, sid, "snapshot", current.id,
            payload={"snapshot_id": snap_id, "motivo": motivo_snap, "etapa": etapa_snap},
        )

    await db.commit()
    return {
        "id": sid,
        "etapa_anterior": etapa_actual,
        "etapa_nueva": cfg.get("etapa_destino"),
        "estado_nuevo": cfg["estado_destino"],
    }


# ============================================================
# Adjuntos por línea (PDF, Word, Excel — justificativos)
# ============================================================

_MIME_PERMITIDOS = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}
_TAMANO_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


def _storage_root() -> Path:
    p = Path(get_settings().attachment_local_path).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


@router.post("/lineas/{lid}/adjuntos")
async def subir_adjunto_linea(
    lid: int,
    archivo: UploadFile = File(...),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Sube un archivo (PDF, Word, Excel) y lo asocia a la línea presupuestaria."""
    ok, vp_sol = await puede_acceder_linea(db, current.id, lid)
    if vp_sol is None:
        raise HTTPException(404, f"línea {lid} no existe")
    if not ok:
        raise HTTPException(403, f"Tu rol no puede subir adjuntos a líneas de {vp_sol}.")
    # Validar línea + obtener solicitud_id para registrar evento
    row = (await db.execute(
        text("SELECT id, solicitud_id FROM planificacion.linea_solicitud WHERE id=:l"),
        {"l": lid},
    )).mappings().first()
    sid = row["solicitud_id"]

    mime = archivo.content_type or ""
    if mime not in _MIME_PERMITIDOS:
        raise HTTPException(
            415,
            f"Tipo de archivo no soportado ({mime}). Aceptamos: PDF, Word (.doc/.docx), Excel (.xls/.xlsx).",
        )

    # Leer al buffer para chequear tamaño (UploadFile usa SpooledTemporaryFile).
    data = await archivo.read()
    if len(data) > _TAMANO_MAX_BYTES:
        raise HTTPException(413, f"Archivo demasiado grande ({len(data)} bytes). Máximo: 10 MB.")
    if len(data) == 0:
        raise HTTPException(400, "Archivo vacío.")

    # Path en disco: {storage}/{sid}/{lid}/{uuid}_{nombre_sanitizado}
    nombre_seguro = "".join(
        c if c.isalnum() or c in "._- " else "_" for c in (archivo.filename or "archivo")
    )[:120]
    rel_dir = Path(str(sid)) / str(lid)
    abs_dir = _storage_root() / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    uid = _uuid.uuid4().hex[:12]
    filename_final = f"{uid}_{nombre_seguro}"
    abs_path = abs_dir / filename_final
    with open(abs_path, "wb") as f:
        f.write(data)

    rel_path = str(rel_dir / filename_final)
    aid = (await db.execute(
        text(
            """INSERT INTO planificacion.adjunto_linea
               (linea_id, nombre_original, tipo_mime, tamano_bytes, path_relativo, subido_por)
               OUTPUT INSERTED.id
               VALUES (:l, :n, :m, :t, :p, :u)"""
        ),
        {"l": lid, "n": archivo.filename or "archivo", "m": mime,
         "t": len(data), "p": rel_path, "u": current.id},
    )).scalar()

    await _registrar_evento(
        db, sid, "subir_adjunto", current.id, linea_id=lid,
        payload={"adjunto_id": aid, "nombre": archivo.filename, "bytes": len(data)},
    )
    await db.commit()

    return {
        "id": aid,
        "linea_id": lid,
        "nombre_original": archivo.filename,
        "tipo_mime": mime,
        "tamano_bytes": len(data),
        "extension": _MIME_PERMITIDOS[mime],
    }


@router.get("/lineas/{lid}/adjuntos")
async def listar_adjuntos_linea(lid: int, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    ok, vp_sol = await puede_acceder_linea(db, current.id, lid)
    if vp_sol is None:
        raise HTTPException(404, f"línea {lid} no existe")
    if not ok:
        raise HTTPException(403, f"Tu rol no puede ver adjuntos de líneas de {vp_sol}.")
    rows = (await db.execute(
        text(
            """SELECT a.id, a.linea_id, a.nombre_original, a.tipo_mime,
                      a.tamano_bytes, a.created_at,
                      u.nombre AS subido_por_nombre, u.apellido AS subido_por_apellido
               FROM planificacion.adjunto_linea a
               LEFT JOIN core.usuario u ON u.id = a.subido_por
               WHERE a.linea_id = :l
               ORDER BY a.created_at DESC"""
        ),
        {"l": lid},
    )).mappings().all()
    return [dict(r) for r in rows]


@router.get("/adjuntos/{aid}")
async def descargar_adjunto(
    aid: int,
    inline: bool = False,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Devuelve el archivo binario. inline=true → preview (Content-Disposition inline)."""
    row = (await db.execute(
        text(
            """SELECT id, linea_id, nombre_original, tipo_mime, path_relativo
               FROM planificacion.adjunto_linea WHERE id=:a"""
        ),
        {"a": aid},
    )).mappings().first()
    if not row:
        raise HTTPException(404, "adjunto no existe")
    ok, vp_sol = await puede_acceder_linea(db, current.id, row["linea_id"])
    if not ok:
        raise HTTPException(403, f"Tu rol no puede descargar adjuntos de líneas de {vp_sol}.")
    abs_path = _storage_root() / row["path_relativo"]
    if not abs_path.exists():
        raise HTTPException(410, "archivo no disponible en el storage")
    disposition = "inline" if inline else "attachment"
    return FileResponse(
        path=str(abs_path),
        media_type=row["tipo_mime"],
        filename=row["nombre_original"],
        headers={
            "Content-Disposition": f'{disposition}; filename="{row["nombre_original"]}"'
        },
    )


# ============================================================
# Observaciones y snapshots (revisión Presidencia)
# ============================================================

_ACCIONES_VALIDAS = {
    "eliminar_linea",
    "modificar_monto",
    "modificar_parametro",
    "reducir_total_planilla",
    "solo_comentario",
}
_ALCANCES_VALIDOS = {"general", "planilla", "linea"}

# Tolerancia para comparar montos (USD redondeados a 2 decimales).
_MONTO_TOL = 0.01


async def _verificar_cumplimiento(
    db: AsyncSession, obs: dict[str, Any]
) -> tuple[bool, str | None]:
    """Verifica que el cambio sugerido por una observación se haya aplicado.

    El VP debe modificar manualmente la línea/planilla usando el editor normal.
    Esta función se invoca al recibir el `aplicar`: si el estado actual no
    refleja el cambio pedido, devolvemos (False, "mensaje claro") y la
    observación NO se marca como aplicada.

    Devuelve (True, None) si todo OK; (False, mensaje) en caso contrario.
    """
    accion = obs["accion_sugerida"]
    v = dict(obs["valor_sugerido"] or {})
    sid = obs["solicitud_id"]
    lid = obs["linea_id"]
    pid = obs["planilla_template_id"]

    # solo_comentario y observaciones sin sugerencia → siempre pasa
    if not accion or accion == "solo_comentario":
        return True, None

    if accion == "eliminar_linea":
        # Si linea_id ya está en NULL, la línea fue borrada (FK SET NULL).
        # En este caso el cumplimiento es justamente que no exista → success.
        if not lid:
            return True, None
        existe = (await db.execute(
            text("SELECT 1 FROM planificacion.linea_solicitud WHERE id=:l"),
            {"l": lid},
        )).scalar()
        if existe:
            return False, "La línea sugerida todavía existe. Eliminala antes de marcar como cumplida."
        return True, None

    if accion == "modificar_monto":
        if not lid:
            return False, "Observación sin línea asociada"
        nuevo_monto = v.get("nuevo_monto")
        if nuevo_monto is None:
            return False, "Observación sin nuevo_monto sugerido"
        linea = (await db.execute(
            text("SELECT monto_solicitado FROM planificacion.linea_solicitud WHERE id=:l"),
            {"l": lid},
        )).mappings().first()
        if not linea:
            return False, "La línea ya no existe."
        actual = float(linea["monto_solicitado"] or 0)
        if abs(actual - float(nuevo_monto)) > _MONTO_TOL:
            return False, (
                f"El monto de la línea sigue siendo USD {actual:,.0f} y debe ser USD {float(nuevo_monto):,.0f}. "
                f"Ajustá la línea antes de marcar como cumplida."
            )
        return True, None

    if accion == "modificar_parametro":
        if not lid:
            return False, "Observación sin línea asociada"
        param = v.get("parametro")
        nuevo_valor = v.get("nuevo_valor")
        if not param or nuevo_valor is None:
            return False, "Observación sin parámetro/nuevo_valor sugerido"
        linea = (await db.execute(
            text("SELECT parametros FROM planificacion.linea_solicitud WHERE id=:l"),
            {"l": lid},
        )).mappings().first()
        if not linea:
            return False, "La línea ya no existe."
        params = dict(linea["parametros"] or {})
        actual = params.get(param)
        # Comparación tolerante a tipos (param numérico puede venir como str/int/float).
        try:
            mismo = float(actual) == float(nuevo_valor)
        except (TypeError, ValueError):
            mismo = str(actual) == str(nuevo_valor)
        if not mismo:
            return False, (
                f"El parámetro «{param}» sigue en '{actual}' y debe ser '{nuevo_valor}'. "
                f"Ajustá la línea antes de marcar como cumplida."
            )
        return True, None

    if accion == "reducir_total_planilla":
        if not pid:
            return False, "Observación sin planilla asociada"
        reducir = float(v.get("reducir_en") or 0)
        inicial = float(v.get("monto_planilla_inicial") or 0)
        if reducir <= 0:
            return False, "Observación sin monto a reducir"
        # Total actual de esa planilla
        actual = (await db.execute(
            text(
                """SELECT COALESCE(SUM(monto_solicitado), 0)
                     FROM planificacion.linea_solicitud
                    WHERE solicitud_id=:s AND planilla_template_id=:p"""
            ),
            {"s": sid, "p": pid},
        )).scalar() or 0
        objetivo = inicial - reducir
        # Aceptamos cualquier reducción >= reducir_en (incluyendo reducir más
        # de lo solicitado — el objetivo se cumple igual o de sobra). Solo
        # rechazamos cuando el total actual es MAYOR al objetivo (sub-reducción).
        if float(actual) > objetivo + _MONTO_TOL:
            faltante = float(actual) - objetivo
            return False, (
                f"El total de la planilla es USD {float(actual):,.0f}. "
                f"Falta recortar USD {faltante:,.0f} para llegar a USD {objetivo:,.0f} "
                f"(había USD {inicial:,.0f}, Presidencia pidió recortar USD {reducir:,.0f}). "
                f"Podés recortar exactamente eso o más; ajustá las líneas y volvé a marcar como cumplida."
            )
        return True, None

    return False, f"Acción no soportada: {accion}"


class ObservacionCrear(BaseModel):
    alcance: str  # general | planilla | linea
    texto: str
    accion_sugerida: str | None = None
    valor_sugerido: dict[str, Any] = Field(default_factory=dict)
    linea_id: int | None = None
    planilla_template_id: int | None = None
    etapa_origen: int = 3
    usuario_id: int | None = None  # deprecado — JWT manda


class ObservacionResolver(BaseModel):
    accion: str  # 'aplicar' | 'rechazar'
    resolucion_comentario: str | None = None
    usuario_id: int | None = None  # deprecado — JWT manda


@router.post("/solicitudes/{sid}/observaciones")
async def crear_observacion(
    sid: int,
    p: ObservacionCrear,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if p.alcance not in _ALCANCES_VALIDOS:
        raise HTTPException(400, f"alcance inválido: {p.alcance}")
    if p.accion_sugerida and p.accion_sugerida not in _ACCIONES_VALIDAS:
        raise HTTPException(400, f"accion_sugerida inválida: {p.accion_sugerida}")
    if p.alcance == "linea" and not p.linea_id:
        raise HTTPException(400, "alcance=linea requiere linea_id")
    if p.alcance == "planilla" and not p.planilla_template_id:
        raise HTTPException(400, "alcance=planilla requiere planilla_template_id")

    ok, vp_sol = await puede_acceder_solicitud(db, current.id, sid)
    if vp_sol is None:
        raise HTTPException(404, "solicitud no existe")
    if not ok:
        raise HTTPException(403, f"Tu rol no puede crear observaciones sobre solicitudes de {vp_sol}.")

    # RBAC adicional por rol + estado: las observaciones solo las crea EL REVISOR
    # de la etapa actual, no cualquier usuario con acceso lateral (planillas_extra)
    # ni el propio cargador (no se observa a sí mismo).
    estado_sol = (await db.execute(
        text("SELECT estado_workflow FROM planificacion.solicitud WHERE id=:s"),
        {"s": sid},
    )).scalar()
    roles = current.roles or []
    autorizado = False
    if estado_sol == "en_revision_vp":
        autorizado = puede_aprobar_vp(roles, current.vp_codigo, vp_sol)
    elif estado_sol == "en_revision_presidencia":
        autorizado = puede_aprobar_presidencia(roles)
    elif estado_sol in ("enviado_revision", "en_revision", "validado"):
        # Workflow legacy: Presidencia revisaba directo.
        autorizado = puede_aprobar_presidencia(roles)
    if not autorizado:
        raise HTTPException(
            403,
            f"En estado '{estado_sol}' solo el revisor correspondiente puede crear observaciones.",
        )

    # Snapshot del estado actual en `valor_sugerido` para validar después al aplicar.
    # Sin esto no sabríamos cuál era el monto/parámetro original cuando se observó.
    valor = dict(p.valor_sugerido or {})
    if p.accion_sugerida == "modificar_monto" and p.linea_id:
        m = (await db.execute(
            text("SELECT monto_solicitado FROM planificacion.linea_solicitud WHERE id=:l"),
            {"l": p.linea_id},
        )).scalar()
        if m is not None:
            valor.setdefault("monto_original", float(m))
    elif p.accion_sugerida == "modificar_parametro" and p.linea_id:
        param = valor.get("parametro")
        if param:
            pr = (await db.execute(
                text("SELECT parametros FROM planificacion.linea_solicitud WHERE id=:l"),
                {"l": p.linea_id},
            )).mappings().first()
            if pr and pr["parametros"]:
                valor.setdefault("valor_original", (pr["parametros"] or {}).get(param))
    elif p.accion_sugerida == "reducir_total_planilla" and p.planilla_template_id:
        total = (await db.execute(
            text(
                """SELECT COALESCE(SUM(monto_solicitado), 0)
                     FROM planificacion.linea_solicitud
                    WHERE solicitud_id=:s AND planilla_template_id=:p"""
            ),
            {"s": sid, "p": p.planilla_template_id},
        )).scalar() or 0
        valor.setdefault("monto_planilla_inicial", float(total))

    oid = (await db.execute(
        text(
            """INSERT INTO planificacion.observacion
                  (solicitud_id, linea_id, planilla_template_id, alcance, texto,
                   accion_sugerida, valor_sugerido, etapa_origen, created_by)
               OUTPUT INSERTED.id
               VALUES (:s, :l, :p, :al, :t,
                       :ac,
                       :v, :eo, :uid)"""
        ),
        {"s": sid, "l": p.linea_id, "p": p.planilla_template_id,
         "al": p.alcance, "t": p.texto, "ac": p.accion_sugerida,
         "v": __import__("json").dumps(valor),
         "eo": p.etapa_origen, "uid": current.id},
    )).scalar()

    await _registrar_evento(
        db, sid, "crear_observacion", current.id, linea_id=p.linea_id,
        payload={"observacion_id": oid, "alcance": p.alcance,
                 "accion_sugerida": p.accion_sugerida, "valor_sugerido": p.valor_sugerido},
        comentario=p.texto[:500],
    )
    await db.commit()
    return {"id": oid}


@router.get("/solicitudes/{sid}/observaciones")
async def listar_observaciones(
    sid: int,
    estado: str | None = None,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    ok, vp_sol = await puede_acceder_solicitud(db, current.id, sid)
    if vp_sol is None:
        raise HTTPException(404, "solicitud no existe")
    if not ok:
        raise HTTPException(403, f"Tu rol no puede ver observaciones de solicitudes de {vp_sol}.")
    where = ["o.solicitud_id = :s"]
    params: dict[str, Any] = {"s": sid}
    if estado:
        where.append("o.estado = :est")
        params["est"] = estado
    rows = (await db.execute(
        text(f"""
            SELECT o.*,
                   uc.nombre AS created_by_nombre, uc.apellido AS created_by_apellido,
                   ur.nombre AS resuelta_por_nombre, ur.apellido AS resuelta_por_apellido,
                   l.item_id, l.cuenta_id,
                   c.codigo AS cuenta_codigo, c.descripcion AS cuenta_descripcion,
                   i.codigo AS item_codigo, i.descripcion AS item_descripcion,
                   pt.nombre AS planilla_nombre, pt.codigo AS planilla_codigo
              FROM planificacion.observacion o
              LEFT JOIN core.usuario uc ON uc.id = o.created_by
              LEFT JOIN core.usuario ur ON ur.id = o.resuelta_por
              LEFT JOIN planificacion.linea_solicitud l ON l.id = o.linea_id
              LEFT JOIN catalogo.cuenta_planificacion c ON c.id = l.cuenta_id
              LEFT JOIN catalogo.item_planificacion i ON i.id = l.item_id
              LEFT JOIN catalogo.planilla_template pt ON pt.id = o.planilla_template_id
             WHERE {' AND '.join(where)}
             ORDER BY o.created_at DESC
        """),
        params,
    )).mappings().all()
    return [dict(r) for r in rows]


@router.patch("/observaciones/{oid}")
async def resolver_observacion(
    oid: int,
    p: ObservacionResolver,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if p.accion not in ("aplicar", "rechazar"):
        raise HTTPException(400, "accion debe ser 'aplicar' o 'rechazar'")
    obs = (await db.execute(
        text("SELECT * FROM planificacion.observacion WHERE id=:o"), {"o": oid}
    )).mappings().first()
    if not obs:
        raise HTTPException(404, "observación no existe")
    if obs["estado"] != "abierta":
        raise HTTPException(409, f"observación ya {obs['estado']}")
    ok, vp_sol = await puede_acceder_solicitud(db, current.id, obs["solicitud_id"])
    if not ok:
        raise HTTPException(403, f"Tu rol no puede resolver observaciones de solicitudes de {vp_sol}.")

    # RBAC: resolver (aplicar/rechazar) lo hace el CARGADOR de la VP (quien
    # ajusta), no cualquier usuario con acceso lateral por planilla. Override:
    # adm_sistema y ver_todo.
    roles = current.roles or []
    es_cargador = (
        "adm_sistema" in roles
        or current.ver_todo
        or (current.vp_codigo or "").upper() == (vp_sol or "").upper()
    )
    if not es_cargador:
        raise HTTPException(
            403,
            "Solo un cargador de la VP de la solicitud (o un administrador) puede "
            "marcar observaciones como aplicadas/rechazadas.",
        )

    nuevo_estado = "aplicada" if p.accion == "aplicar" else "rechazada"

    # NUEVA LÓGICA: el VP debe haber hecho el cambio MANUALMENTE en el editor.
    # Aquí solo verificamos que el estado actual cumpla con lo observado. Si no
    # cumple, devolvemos 400 con un mensaje claro y la observación queda abierta.
    if p.accion == "aplicar":
        ok, msg = await _verificar_cumplimiento(db, dict(obs))
        if not ok:
            raise HTTPException(400, msg or "El ajuste sugerido todavía no se aplicó.")
    # rechazar no requiere verificación — el VP puede rechazar siempre con motivo.

    await db.execute(
        text(
            """UPDATE planificacion.observacion
                  SET estado = :est,
                      resuelta_por = :uid,
                      resuelta_at = SYSDATETIMEOFFSET(),
                      resolucion_comentario = :rc
                WHERE id = :o"""
        ),
        {"est": nuevo_estado, "uid": current.id, "rc": p.resolucion_comentario, "o": oid},
    )

    accion_evento = "aplicar_observacion" if p.accion == "aplicar" else "rechazar_observacion"
    await _registrar_evento(
        db, obs["solicitud_id"], accion_evento, current.id, linea_id=obs["linea_id"],
        payload={"observacion_id": oid, "accion_sugerida": obs["accion_sugerida"],
                 "valor_sugerido": dict(obs["valor_sugerido"] or {})},
        comentario=p.resolucion_comentario,
    )
    await db.commit()
    return {"id": oid, "estado": nuevo_estado}


@router.get("/solicitudes/{sid}/snapshots")
async def listar_snapshots(sid: int, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    ok, vp_sol = await puede_acceder_solicitud(db, current.id, sid)
    if vp_sol is None:
        raise HTTPException(404, "solicitud no existe")
    if not ok:
        raise HTTPException(403, f"Tu rol no puede ver snapshots de solicitudes de {vp_sol}.")
    rows = (await db.execute(
        text(
            """SELECT s.id, s.etapa, s.motivo, s.monto_total, s.created_at,
                      u.nombre AS created_by_nombre, u.apellido AS created_by_apellido,
                      (SELECT COUNT(*) FROM planificacion.snapshot_linea sl WHERE sl.snapshot_id = s.id) AS n_lineas
                 FROM planificacion.snapshot_solicitud s
                 LEFT JOIN core.usuario u ON u.id = s.created_by
                WHERE s.solicitud_id = :s
                ORDER BY s.created_at DESC"""
        ),
        {"s": sid},
    )).mappings().all()
    return [dict(r) for r in rows]


@router.get("/snapshots/{snap_id}/lineas")
async def detalle_snapshot(snap_id: int, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    snap = (await db.execute(
        text("SELECT * FROM planificacion.snapshot_solicitud WHERE id=:s"),
        {"s": snap_id},
    )).mappings().first()
    if not snap:
        raise HTTPException(404, "snapshot no existe")
    ok, vp_sol = await puede_acceder_solicitud(db, current.id, snap["solicitud_id"])
    if not ok:
        raise HTTPException(403, f"Tu rol no puede ver snapshots de solicitudes de {vp_sol}.")
    lineas = (await db.execute(
        text("SELECT * FROM planificacion.snapshot_linea WHERE snapshot_id=:s ORDER BY id"),
        {"s": snap_id},
    )).mappings().all()
    return {"snapshot": dict(snap), "lineas": [dict(r) for r in lineas]}


@router.delete("/adjuntos/{aid}")
async def eliminar_adjunto(
    aid: int,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = (await db.execute(
        text(
            """SELECT a.id, a.linea_id, a.path_relativo, a.nombre_original,
                      l.solicitud_id
               FROM planificacion.adjunto_linea a
               JOIN planificacion.linea_solicitud l ON l.id = a.linea_id
               WHERE a.id=:a"""
        ),
        {"a": aid},
    )).mappings().first()
    if not row:
        raise HTTPException(404, "adjunto no existe")
    ok, vp_sol = await puede_acceder_linea(db, current.id, row["linea_id"])
    if not ok:
        raise HTTPException(403, f"Tu rol no puede eliminar adjuntos de líneas de {vp_sol}.")
    # Borrar registro primero — si el unlink del archivo falla, no quedamos huérfanos en BDR.
    await db.execute(
        text("DELETE FROM planificacion.adjunto_linea WHERE id=:a"),
        {"a": aid},
    )
    try:
        # Defensa contra path traversal: el path final debe vivir dentro del root.
        root = _storage_root().resolve()
        abs_path = (root / row["path_relativo"]).resolve()
        if not str(abs_path).startswith(str(root) + os.sep) and abs_path != root:
            logger.warning("adjunto fuera de storage root, ignoro: id=%s path=%s", aid, row["path_relativo"])
        elif abs_path.exists():
            os.remove(abs_path)
    except OSError as e:
        # No tirar — la fila ya fue borrada y el archivo queda huérfano.
        # Pero loguear para que TI vea si hay un patrón de fallas (permisos, FS lleno).
        logger.warning("No se pudo borrar archivo adjunto id=%s path=%s: %s", aid, row.get("path_relativo"), e)

    await _registrar_evento(
        db, row["solicitud_id"], "eliminar_adjunto", current.id, linea_id=row["linea_id"],
        payload={"adjunto_id": aid, "nombre": row["nombre_original"]},
    )
    await db.commit()
    return {"id": aid, "deleted": True}

