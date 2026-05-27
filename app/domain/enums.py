"""Enums compartidos del dominio de planificación.

  El frontend tiene un mirror en
`frontend/src/domain/enums.ts` que debe mantenerse en sincronía.

Workflow vigente (migración 030):

    Etapa 0  Elaboración              cargadores de la VP
    Etapa 1  Revisión Vicepresidente  VP titular (solo VPF/VPD/VPO/VPE)
    Etapa 2  Revisión Presidencia     Presidenta / Jefe Gabinete
    Etapa 3  Aprobado
    Etapa 4  Cerrado

Los estados legacy (enviado_revision, en_revision, validado, observado,
devuelto) quedan presentes en el enum DB para no invalidar filas históricas,
pero el código nuevo no los emite. EstadoWorkflow.LEGACY_* los marca.
"""
from enum import Enum


class EstadoWorkflow(str, Enum):
    """Estado actual de una solicitud en el workflow de aprobación."""
    EN_ELABORACION = "en_elaboracion"

    # --- Workflow nuevo ---
    EN_REVISION_VP = "en_revision_vp"
    OBSERVADO_VP = "observado_vp"
    DEVUELTO_VP = "devuelto_vp"
    EN_REVISION_PRESIDENCIA = "en_revision_presidencia"
    OBSERVADO_PRESIDENCIA = "observado_presidencia"
    DEVUELTO_PRESIDENCIA = "devuelto_presidencia"

    APROBADO = "aprobado"
    CERRADO = "cerrado"

    # --- Legacy (no se emiten desde el código nuevo, sí se aceptan en lectura) ---
    LEGACY_ENVIADO_REVISION = "enviado_revision"
    LEGACY_EN_REVISION = "en_revision"
    LEGACY_OBSERVADO = "observado"
    LEGACY_DEVUELTO = "devuelto"
    LEGACY_VALIDADO = "validado"


# Estados en los que el solicitante puede modificar líneas (etapa 0).
ESTADOS_EDITABLES: frozenset[str] = frozenset({
    EstadoWorkflow.EN_ELABORACION.value,
    EstadoWorkflow.OBSERVADO_VP.value,
    EstadoWorkflow.DEVUELTO_VP.value,
    EstadoWorkflow.OBSERVADO_PRESIDENCIA.value,
    EstadoWorkflow.DEVUELTO_PRESIDENCIA.value,
    # Legacy: si quedan solicitudes viejas en esos estados, siguen editables.
    EstadoWorkflow.LEGACY_OBSERVADO.value,
    EstadoWorkflow.LEGACY_DEVUELTO.value,
})

# Estados terminales — no admiten más transiciones de aprobación.
ESTADOS_TERMINALES: frozenset[str] = frozenset({
    EstadoWorkflow.APROBADO.value,
    EstadoWorkflow.CERRADO.value,
})

# Estados "en curso" (vivos en algún paso del workflow).
ESTADOS_EN_CURSO: frozenset[str] = frozenset({
    EstadoWorkflow.EN_ELABORACION.value,
    EstadoWorkflow.EN_REVISION_VP.value,
    EstadoWorkflow.OBSERVADO_VP.value,
    EstadoWorkflow.DEVUELTO_VP.value,
    EstadoWorkflow.EN_REVISION_PRESIDENCIA.value,
    EstadoWorkflow.OBSERVADO_PRESIDENCIA.value,
    EstadoWorkflow.DEVUELTO_PRESIDENCIA.value,
})


class EstadoLinea(str, Enum):
    """Estado de una línea individual de la solicitud."""
    BORRADOR = "borrador"
    VALIDADA = "validada"
    OBSERVADA = "observada"
    APROBADA = "aprobada"
    RECHAZADA = "rechazada"


class AccionEvento(str, Enum):
    """Acciones auditables registradas en evento_solicitud."""
    CREAR_SOLICITUD = "crear_solicitud"
    AGREGAR_LINEA = "agregar_linea"
    MODIFICAR_LINEA = "modificar_linea"
    ELIMINAR_LINEA = "eliminar_linea"
    # Workflow nuevo
    ENVIAR_A_REVISION_VP = "enviar_a_revision_vp"
    ENVIAR_A_REVISION_PRESIDENCIA = "enviar_a_revision_presidencia"
    APROBAR_VP = "aprobar_vp"
    OBSERVAR_VP = "observar_vp"
    DEVOLVER_VP = "devolver_vp"
    APROBAR_PRESIDENCIA = "aprobar_presidencia"
    OBSERVAR_PRESIDENCIA = "observar_presidencia"
    DEVOLVER_PRESIDENCIA = "devolver_presidencia"
    CERRAR = "cerrar"
    # Legacy
    LEGACY_ENVIAR_A_REVISION = "enviar_a_revision"
    LEGACY_APROBAR_OBJETIVOS = "aprobar_objetivos"
    LEGACY_APROBAR_DIRECTORIO = "aprobar_directorio"
    LEGACY_OBSERVAR = "observar"
    LEGACY_DEVOLVER = "devolver"
    # Misc
    SUBIR_ADJUNTO = "subir_adjunto"
    ELIMINAR_ADJUNTO = "eliminar_adjunto"
    CREAR_OBSERVACION = "crear_observacion"
    APLICAR_OBSERVACION = "aplicar_observacion"
    RECHAZAR_OBSERVACION = "rechazar_observacion"
    SNAPSHOT = "snapshot"


def puede_editar(estado_workflow: str) -> bool:
    """Boolean helper: la solicitud está en estado editable por el solicitante."""
    return estado_workflow in ESTADOS_EDITABLES
