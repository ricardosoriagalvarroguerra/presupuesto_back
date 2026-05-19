"""Elimina la etapa intermedia "Aprobado objetivos" del workflow.

Revision ID: 013_remove_etapa_objetivos
Revises: 012_obs_modificar_parametro
Create Date: 2026-05-13

El flujo pasa de 5 a 4 etapas:
  ANTES                          AHORA
  0 Borrador                     0 Borrador
  1 Aprobado objetivos     →     (eliminado)
  2 Aprobado Presidencia   →     1 Aprobado Presidencia
  3 Aprobado Directorio    →     2 Aprobado Directorio
  4 Cerrado                →     3 Cerrado

Para solicitudes existentes:
  - Las que estaban en etapa 2 con estado 'validado' (= "aprobado objetivos,
    esperando Presidencia") vuelven a estado 'enviado_revision' en etapa 1.
    Es la interpretación más fiel: ya no existe esa fase intermedia, así que
    quedan a la espera de la revisión de Presidencia.
  - Las que estaban en etapa 3 o 4 se rebajan 1 punto.
  - Snapshots con etapa 2/3/4 se rebajan 1 (preserva el orden histórico).
  - `evento_solicitud` NO se renumera para conservar el histórico fiel del
    momento en que ocurrió cada cambio (el frontend formatea el evento por
    `accion`, no por el número de etapa).

Las columnas `monto_objetivos` y `aprobado_objetivos_at` se mantienen en la
tabla por compatibilidad — quedan en NULL para nuevas solicitudes.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "013_remove_etapa_objetivos"
down_revision: str | None = "012_obs_modificar_parametro"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Paso 1: marcar temporalmente las etapas afectadas con valores negativos
    # para evitar choques al renumerar (si bajamos etapa 3 a 2, no queremos
    # que la siguiente UPDATE machaque las que YA estaban en 2).
    op.execute(
        """UPDATE planificacion.solicitud
              SET etapa_actual = -etapa_actual
            WHERE etapa_actual >= 2"""
    )
    op.execute(
        """UPDATE planificacion.snapshot_solicitud
              SET etapa = -etapa
            WHERE etapa >= 2"""
    )

    # Paso 2: solicitudes en etapa -2 con estado 'validado' (eran "aprobado objetivos")
    # → vuelven a 'enviado_revision' en etapa 1 (esperando Presidencia).
    op.execute(
        """UPDATE planificacion.solicitud
              SET etapa_actual = 1, estado_workflow = 'enviado_revision'
            WHERE etapa_actual = -2 AND estado_workflow = 'validado'"""
    )
    # Solicitudes en etapa -2 con otro estado (observado/devuelto en fase objetivos)
    # → también van a etapa 1 manteniendo el estado.
    op.execute(
        """UPDATE planificacion.solicitud
              SET etapa_actual = 1
            WHERE etapa_actual = -2"""
    )
    # Etapa -3 → 2; etapa -4 → 3
    op.execute(
        """UPDATE planificacion.solicitud
              SET etapa_actual = 2
            WHERE etapa_actual = -3"""
    )
    op.execute(
        """UPDATE planificacion.solicitud
              SET etapa_actual = 3
            WHERE etapa_actual = -4"""
    )

    # Snapshots: misma rebaja sin reinterpretación de estado.
    op.execute(
        """UPDATE planificacion.snapshot_solicitud SET etapa = 1 WHERE etapa = -2"""
    )
    op.execute(
        """UPDATE planificacion.snapshot_solicitud SET etapa = 2 WHERE etapa = -3"""
    )
    op.execute(
        """UPDATE planificacion.snapshot_solicitud SET etapa = 3 WHERE etapa = -4"""
    )


def downgrade() -> None:
    # No es 100% reversible — el estado 'validado' tras objetivos se "perdió"
    # al hacer downgrade ya no podemos saber qué solicitudes habían pasado por
    # esa fase. Hacemos el inverso para etapas conocidas; las que eran
    # 'aprobado objetivos' no se pueden recuperar.
    op.execute(
        """UPDATE planificacion.solicitud
              SET etapa_actual = -etapa_actual
            WHERE etapa_actual >= 1"""
    )
    op.execute(
        """UPDATE planificacion.solicitud SET etapa_actual = 2 WHERE etapa_actual = -1"""
    )
    op.execute(
        """UPDATE planificacion.solicitud SET etapa_actual = 3 WHERE etapa_actual = -2"""
    )
    op.execute(
        """UPDATE planificacion.solicitud SET etapa_actual = 4 WHERE etapa_actual = -3"""
    )
    op.execute(
        """UPDATE planificacion.snapshot_solicitud SET etapa = -etapa WHERE etapa >= 1"""
    )
    op.execute(
        """UPDATE planificacion.snapshot_solicitud SET etapa = 2 WHERE etapa = -1"""
    )
    op.execute(
        """UPDATE planificacion.snapshot_solicitud SET etapa = 3 WHERE etapa = -2"""
    )
    op.execute(
        """UPDATE planificacion.snapshot_solicitud SET etapa = 4 WHERE etapa = -3"""
    )
