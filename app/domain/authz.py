"""Único lugar donde vive la regla de "qué puede tocar cada usuario".

La misma lógica está espejada en el frontend (`SolicitudEditor.tsx::tieneAcceso`)
para que la UI no muestre opciones que el back va a rechazar. Si cambia una,
hay que cambiar la otra — están sincronizadas a propósito.

Conceptos clave:
  - `vp_codigo` del usuario: a qué VP pertenece (VPF / VPD / VPO / VPE / PRE / GOB).
  - `ver_todo` (BIT): bypass total de chequeos. Lo tienen los usuarios admin /
    soporte. No es solo "lectura": también puede editar, así que se asigna con
    cuidado.
  - `usuario_planilla_extra`: mecanismo cross-VP por planilla. Le suma a un
    user acceso a UNA planilla en solicitudes de OTRAS VPs.
  - `planilla_template.solo_cross_vp`: marca de planilla "institucional" con
    dueño único. Cuando está en 1, la regla normal "mi VP → todas las planillas"
    NO aplica: solo accede el user que la tenga en `usuario_planilla_extra`.
    Ver `puede_acceder_planilla` abajo.
"""
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def planillas_extra_de(db: AsyncSession, usuario_id: int) -> list[str]:
    """Devuelve la lista de códigos de planilla cross-VP del usuario.

    Para la mayoría devuelve []. Solo los usuarios marcados como cargadores
    institucionales de alguna planilla tienen entradas acá.
    """
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
    """¿Puede este user ver/editar `planilla_codigo` en una solicitud de `vp_solicitud`?

    Orden de evaluación (importa el orden):
      1. ver_todo=true → puede todo. Sin matices.
      2. Si la planilla es `solo_cross_vp=1` (institucional con dueño único):
         la regla "mi VP propia" NO aplica. Solo accede quien la tenga en
         `usuario_planilla_extra`. Sin esto, cualquier cargador de la misma
         VP que el dueño podría cargarla.
      3. vp del user == vp de la solicitud → adelante (planilla normal en VP propia).
      4. Planilla está en sus extras → cross-VP autorizado.

    Espejo en frontend: `SolicitudEditor.tsx::tieneAcceso`.
    """
    user = (await db.execute(
        text("SELECT vp_codigo, ver_todo FROM core.usuario WHERE id=:u"),
        {"u": usuario_id},
    )).mappings().first()
    if not user:
        return False
    if user["ver_todo"]:
        return True

    # Necesitamos saber si la planilla está marcada como institucional.
    # Es una consulta extra por POST de línea, pero `planilla_template` tiene
    # pocas filas y MSSQL la cachea; no se nota en performance.
    pt = (await db.execute(
        text("SELECT solo_cross_vp FROM catalogo.planilla_template WHERE codigo=:c"),
        {"c": planilla_codigo},
    )).mappings().first()
    solo_cross_vp = bool(pt and pt["solo_cross_vp"])

    extras = await planillas_extra_de(db, usuario_id)
    if solo_cross_vp:
        # Institucional → SOLO via extras. Sin atajos.
        return planilla_codigo in extras

    # Planilla normal: VP propia o extras.
    if user["vp_codigo"] == vp_solicitud:
        return True
    return planilla_codigo in extras


async def puede_acceder_solicitud(
    db: AsyncSession,
    usuario_id: int,
    solicitud_id: int,
) -> tuple[bool, str | None]:
    """¿Puede abrir/modificar la solicitud `solicitud_id`?

    Es un chequeo más laxo que `puede_acceder_planilla` — acá vemos si entra
    al editor, no si puede cargar una línea puntual. Adentro del editor el
    filtro fino por planilla lo aplica la otra función.

    Regla:
      1. ver_todo=true → siempre.
      2. Su VP propia → siempre.
      3. presidente / jefe_gabinete / adm_sistema → siempre (necesitan abrir
         todas las solicitudes en etapa 2 para revisarlas).
      4. Tiene planillas_extra (cualquiera) → puede abrir cualquier solicitud
         para aportar su planilla cross-VP, aunque la solicitud todavía no
         tenga líneas de esa planilla. Si bloqueáramos por "no hay líneas
         tuyas todavía", el cargador cross-VP no podría meter su primera.

    Devuelve `(puede, vp_codigo)`. El vp_codigo se devuelve aunque falle el
    permiso, para usarlo en el mensajer de error.
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
    # Presidencia / Jefe Gabinete / admin → scope global por su rol.
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
    """¿Puede tocar la línea? Resuelve (vp_solicitud, planilla) y delega.

    La sutileza: un user cross-VP no puede tocar cualquier línea de la
    solicitud ajena — solo las de SU planilla. Por eso resolvemos la planilla
    específica de la línea y pasamos por `puede_acceder_planilla`.
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


# ────────────────────────────────────────────────────────────────────────────
# RBAC del workflow de aprobación
# ────────────────────────────────────────────────────────────────────────────
#
# Roles que figuran en core.rol:
#   - vicepresidente   → VP titular. Uno por VPF/VPD/VPO/VPE. Aprueba/observa
#                        SU propia VP en etapa 1.
#   - presidente       → cargo de Presidencia.
#   - jefe_gabinete    → Jefe de Gabinete. Tiene los mismos poderes que el
#                        Presidente en etapa 2 (delegación plena, para que el
#                        flujo no se trabe cuando uno de los dos no está).
#   - jefe_unidad / jefe_division / analista → cargadores. Trabajan la solicitud
#                        en etapa 0 y la envían a revisión, pero NO la aprueban.
#   - adm_sistema      → soporte técnico. Override total (cualquier transición).
#                        Es escape hatch, no se usa en el día a día.
#
# VPs sin VP propio: PRE (Presidencia Ejecutiva) y GOB (Gobernanza). Sus
# solicitudes saltan etapa 1 y van directo a etapa 2 (revisión de Presidencia)
# porque institucionalmente no tienen Vicepresidente que las firme. (Esta lógica tambien es un placeholder, esperando necesidades reales)
# PD: Estos roles son de prueba y no definen los verdaderos roles ni workflow que se
# va a implementar, esperando necesidades relevadas por el grupo de trabajo.

ROLES_PRESIDENCIA = {"presidente", "jefe_gabinete"}
ROLES_ADMIN_OVERRIDE = {"adm_sistema"}

# VPs que saltan la etapa de revisión VP.
VPS_SIN_VICEPRESIDENTE = {"PRE", "GOB"}


def vp_salta_revision_vp(vp_codigo: str | None) -> bool:
    """True si la VP no tiene VP propio → su solicitud va directo a Presidencia."""
    return (vp_codigo or "").upper() in VPS_SIN_VICEPRESIDENTE


def puede_aprobar_vp(
    roles: list[str], usuario_vp_codigo: str | None, solicitud_vp_codigo: str
) -> bool:
    """¿Puede actuar en etapa 1 (aprobar/observar/devolver como VP)?

    Necesita las dos condiciones:
      a) tener el rol `vicepresidente`
      b) que SU vp_codigo coincida con el de la solicitud
    Un VP de otra VP queda afuera por defecto. adm_sistema pasa por override.
    """
    if not roles:
        return False
    if ROLES_ADMIN_OVERRIDE.intersection(roles):
        return True
    if "vicepresidente" not in roles:
        return False
    return (usuario_vp_codigo or "").upper() == (solicitud_vp_codigo or "").upper()


def puede_aprobar_presidencia(roles: list[str]) -> bool:
    """¿Puede actuar en etapa 2 (Presidencia)? Presidente o Jefe de Gabinete.

    No chequeo VP porque Presidencia opera sobre cualquiera. adm_sistema override.
    """
    if not roles:
        return False
    if ROLES_ADMIN_OVERRIDE.intersection(roles):
        return True
    return bool(ROLES_PRESIDENCIA.intersection(roles))


def puede_enviar_a_revision(
    roles: list[str], usuario_vp_codigo: str | None, solicitud_vp_codigo: str
) -> bool:
    """¿Puede empujar la solicitud desde etapa 0 hacia la siguiente?

    Cualquier user de la VP de la solicitud puede (incluido el VP titular,
    que a veces cierra el paquete él mismo antes de mandarlo a revisión).
    Cross-VP de otras VPs NO pueden enviar — solo aportar líneas de su
    planilla; el envío lo dispara el dueño de la solicitud.
    """
    if not roles:
        return False
    if ROLES_ADMIN_OVERRIDE.intersection(roles):
        return True
    return (usuario_vp_codigo or "").upper() == (solicitud_vp_codigo or "").upper()


async def _roles_de(db: AsyncSession, usuario_id: int) -> list[str]:
    """Devuelve los códigos de rol del usuario. Helper interno."""
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
    """Fragmento de WHERE `AND ...` para filtrar listados de solicitudes por scope.

    Lo usan GET /solicitudes, GET /actividad-reciente, GET /avance. Devuelve
    string vacío cuando el user tiene scope global (ver_todo o presidencia).

    Conceptualmente: ¿qué solicitudes ve este user en el listado?
      - ver_todo / presidente / jefe_gabinete / adm_sistema → todas.
      - tiene cualquier planillas_extra → todas también (necesita poder entrar
        a cualquiera para aportar su planilla cross-VP; el filtro fino por
        planilla lo aplica el editor adentro).
      - tiene vp_codigo y NADA MÁS → solo las de su VP.
      - no tiene ni VP ni extras → nada (caso raro pero defensivo).

    Cuidado al cambiar: si sacás el caso "tiene extras → ve todo", los users
    cross-VP no podrían entrar a solicitudes de otras VPs donde todavía no
    cargaron ninguna línea.
    """
    user = (await db.execute(
        text("SELECT vp_codigo, ver_todo FROM core.usuario WHERE id=:u"),
        {"u": usuario_id},
    )).mappings().first()
    if not user:
        # Sin usuario en la BDR (token huérfano) → no ve nada. Defensa en profundidad.
        return " AND 1=0", {}
    if user["ver_todo"]:
        return "", {}

    roles = await _roles_de(db, usuario_id)
    if {"presidente", "jefe_gabinete", "adm_sistema"}.intersection(roles):
        return "", {}

    extras = await planillas_extra_de(db, usuario_id)
    if extras:
        return "", {}
    if user["vp_codigo"]:
        return " AND s.vp_codigo = :scope_vp", {"scope_vp": user["vp_codigo"]}
    # Sin VP y sin extras → no ve nada. Caso raro, pero mejor cerrado que abierto.
    return " AND 1=0", {}
