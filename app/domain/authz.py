"""Helpers únicos de autorización por VP / planilla.

Antes la regla estaba partida en 3 lugares: frontend (tieneAccesoPlanillaEnVp),
backend agregar_linea, y filtro de /solicitudes. Un cambio en una sin las otras
abre brechas. Esta es la fuente única backend.
"""
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def planillas_extra_de(db: AsyncSession, usuario_id: int) -> list[str]:
    """Códigos de planilla a los que el usuario puede acceder cross-VP."""
    rows = (await db.execute(
        text("SELECT planilla_codigo FROM core.usuario_planilla_extra WHERE usuario_id=:u"),
        {"u": usuario_id},
    )).scalars().all()
    return list(rows)


async def puede_acceder_planilla(
    db: AsyncSession,
    usuario_id: int,
    vp_solicitud: str,
    planilla_codigo: str,
) -> bool:
    """¿El usuario puede ver/editar `planilla_codigo` en una solicitud de
    `vp_solicitud`?

    Reglas (mismas que el frontend `tieneAccesoPlanillaEnVp`):
      1. ver_todo=true   → siempre.
      2. vp_codigo == vp_solicitud → siempre (es su VP propia).
      3. planilla_codigo ∈ usuario_planilla_extra → solo esa planilla cross-VP.
    """
    user = (await db.execute(
        text("SELECT vp_codigo, ver_todo FROM core.usuario WHERE id=:u"),
        {"u": usuario_id},
    )).mappings().first()
    if not user:
        return False
    if user["ver_todo"]:
        return True
    if user["vp_codigo"] == vp_solicitud:
        return True
    extras = await planillas_extra_de(db, usuario_id)
    return planilla_codigo in extras


async def puede_acceder_solicitud(
    db: AsyncSession,
    usuario_id: int,
    solicitud_id: int,
) -> tuple[bool, str | None]:
    """¿El usuario puede ver/modificar la solicitud `solicitud_id`?

    Espejo de `alcance_solicitudes_sql` para chequeo puntual por id:
      1. ver_todo=true              → siempre.
      2. vp_codigo == solicitud.vp  → siempre (es su VP).
      3. tiene `planillas_extra`    → puede acceder a cualquier solicitud
         (caso Angel/Salarios cross-VP: aporta líneas de SU planilla a
         solicitudes de cualquier VP, así que necesita poder abrir el editor
         incluso si la solicitud aún no tiene líneas de esa planilla).

    Devuelve (puede, vp_codigo_de_la_solicitud). `vp_codigo` se devuelve aunque
    sea para logging/mensajes de error.
    """
    s = (await db.execute(
        text("SELECT vp_codigo FROM planificacion.solicitud WHERE id=:s"),
        {"s": solicitud_id},
    )).mappings().first()
    if not s:
        return False, None
    vp_sol = s["vp_codigo"]

    user = (await db.execute(
        text("SELECT vp_codigo, ver_todo FROM core.usuario WHERE id=:u"),
        {"u": usuario_id},
    )).mappings().first()
    if not user:
        return False, vp_sol
    if user["ver_todo"]:
        return True, vp_sol
    if user["vp_codigo"] == vp_sol:
        return True, vp_sol
    # Presidente / Jefe Gabinete / adm_sistema pueden abrir cualquier solicitud
    # (necesitan acceso para revisarla en etapa 2).
    roles = await _roles_de(db, usuario_id)
    if {"presidente", "jefe_gabinete", "adm_sistema"}.intersection(roles):
        return True, vp_sol
    extras = await planillas_extra_de(db, usuario_id)
    if extras:
        return True, vp_sol
    return False, vp_sol


async def puede_acceder_linea(
    db: AsyncSession,
    usuario_id: int,
    linea_id: int,
) -> tuple[bool, str | None]:
    """¿El usuario puede ver/modificar la línea `linea_id`?

    Resuelve (vp_solicitud, planilla_codigo) de la línea y aplica
    `puede_acceder_planilla` — los usuarios cross-VP por planilla solo pueden
    tocar líneas de SU planilla, no cualquier línea de la solicitud.
    """
    row = (await db.execute(
        text("""
            SELECT s.vp_codigo AS vp, pt.codigo AS planilla
            FROM planificacion.linea_solicitud l
            JOIN planificacion.solicitud s ON s.id = l.solicitud_id
            JOIN catalogo.planilla_template pt ON pt.id = l.planilla_template_id
            WHERE l.id = :l
        """),
        {"l": linea_id},
    )).mappings().first()
    if not row:
        return False, None
    ok = await puede_acceder_planilla(db, usuario_id, row["vp"], row["planilla"])
    return ok, row["vp"]


# ============================================================
# RBAC del workflow de aprobación
# ============================================================
#
# Roles relevantes en core.rol:
#   - vicepresidente          → VP titular (uno por VPF/VPD/VPO/VPE).
#   - presidente              → Luciana Botafogo (Presidenta).
#   - jefe_gabinete           → Maria Calvino. Equivalente al Presidente en
#                               revisión de Presidencia (delegación plena).
#   - jefe_unidad / jefe_division / analista
#                             → cargadores de la VP. Pueden trabajar la
#                               solicitud y enviarla a revisión, pero NO aprobarla.
#   - adm_sistema             → administrador, puede ejecutar cualquier
#                               transición (override técnico para soporte).
#
# VPs especiales:
#   - PRE (Presidencia Ejecutiva) y GOB (Gobernanza) NO tienen "Vicepresidente"
#     propio, así que sus solicitudes saltan la etapa 1 (revisión VP) y van
#     directo a etapa 2 (revisión Presidencia).

ROLES_PRESIDENCIA = {"presidente", "jefe_gabinete"}
ROLES_ADMIN_OVERRIDE = {"adm_sistema"}

# VPs que NO tienen Vicepresidente propio → saltan la etapa de revisión VP.
VPS_SIN_VICEPRESIDENTE = {"PRE", "GOB"}


def vp_salta_revision_vp(vp_codigo: str | None) -> bool:
    """Verdadero si la VP no tiene VP propio (PRE/GOB) y la solicitud debe
    saltar la etapa 1 e ir directo a revisión de Presidencia."""
    return (vp_codigo or "").upper() in VPS_SIN_VICEPRESIDENTE


def puede_aprobar_vp(
    roles: list[str], usuario_vp_codigo: str | None, solicitud_vp_codigo: str
) -> bool:
    """¿El usuario actual puede aprobar/observar/devolver una solicitud en la
    etapa 1 (revisión VP)?

    Regla: rol 'vicepresidente' Y vp_codigo del usuario == vp_codigo de la
    solicitud. También aceptamos adm_sistema como override.
    """
    if not roles:
        return False
    if ROLES_ADMIN_OVERRIDE.intersection(roles):
        return True
    if "vicepresidente" not in roles:
        return False
    return (usuario_vp_codigo or "").upper() == (solicitud_vp_codigo or "").upper()


def puede_aprobar_presidencia(roles: list[str]) -> bool:
    """¿El usuario actual puede aprobar/observar/devolver en la etapa 2
    (revisión Presidencia)? Presidente y Jefe de Gabinete tienen poderes
    equivalentes. adm_sistema queda como override."""
    if not roles:
        return False
    if ROLES_ADMIN_OVERRIDE.intersection(roles):
        return True
    return bool(ROLES_PRESIDENCIA.intersection(roles))


def puede_enviar_a_revision(
    roles: list[str], usuario_vp_codigo: str | None, solicitud_vp_codigo: str
) -> bool:
    """¿El usuario puede empujar la solicitud al siguiente paso desde etapa 0?

    Cualquier usuario con vp_codigo coincidente (cargador) puede hacerlo. El
    VP también puede (es habitual que él mismo cierre el paquete y lo envíe).
    adm_sistema override."""
    if not roles:
        return False
    if ROLES_ADMIN_OVERRIDE.intersection(roles):
        return True
    return (usuario_vp_codigo or "").upper() == (solicitud_vp_codigo or "").upper()


async def _roles_de(db: AsyncSession, usuario_id: int) -> list[str]:
    rows = (await db.execute(
        text("""
            SELECT r.codigo FROM core.usuario_rol ur
            JOIN core.rol r ON r.id = ur.rol_id
            WHERE ur.usuario_id = :u
        """),
        {"u": usuario_id},
    )).scalars().all()
    return list(rows)


async def alcance_solicitudes_sql(
    db: AsyncSession,
    usuario_id: int,
) -> tuple[str, dict[str, Any]]:
    """Devuelve un fragmento SQL `AND ...` (puede ser vacío) + params para
    aplicar el scope de visibilidad de solicitudes según el rol del usuario.

    Llamado desde GET /solicitudes, GET /actividad-reciente, GET /avance.
    """
    user = (await db.execute(
        text("SELECT vp_codigo, ver_todo FROM core.usuario WHERE id=:u"),
        {"u": usuario_id},
    )).mappings().first()
    if not user:
        # Sin usuario → no ve nada (defensa en profundidad)
        return " AND 1=0", {}
    if user["ver_todo"]:
        return "", {}

    roles = await _roles_de(db, usuario_id)
    # Presidente y Jefe de Gabinete tienen scope global: necesitan ver todas las
    # solicitudes para revisarlas en etapa 2. adm_sistema lo mismo por soporte.
    if {"presidente", "jefe_gabinete", "adm_sistema"}.intersection(roles):
        return "", {}

    extras = await planillas_extra_de(db, usuario_id)
    if extras:
        # Tiene planillas cross-VP → ve todas las solicitudes (puede aportar a cada una).
        # El filtro fino por planilla se aplica en el editor (tabs visibles).
        return "", {}
    if user["vp_codigo"]:
        return " AND s.vp_codigo = :scope_vp", {"scope_vp": user["vp_codigo"]}
    # Sin VP y sin planillas_extra → no ve nada.
    return " AND 1=0", {}
