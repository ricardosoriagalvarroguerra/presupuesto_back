"""Workflow nuevo: revisión del Vicepresidente como paso intermedio.

Revision ID: 030_workflow_vp
Revises: 029_auditoria_login
Create Date: 2026-05-18

Cambia el flujo de aprobación:

    Antes:  Cargador → [enviar] → Presidencia → [aprobar] → Directorio
    Ahora:  Cargador → [enviar] → Vicepresidente → [aprobar] → Presidencia → [aprobar]

Cambios DDL (todos aditivos para no romper datos existentes):
  1. Nuevos valores del enum `planificacion.solicitud_estado_wf`:
       en_revision_vp, observado_vp, devuelto_vp,
       en_revision_presidencia, observado_presidencia, devuelto_presidencia.
     Los valores viejos (enviado_revision, en_revision, validado, observado,
     devuelto) quedan para no invalidar filas históricas; el código nuevo
     deja de usarlos.
  2. `linea_solicitud.monto_vp` (numeric 18,2 nullable): congela el monto
     aprobado por el VP. Los campos viejos (monto_objetivos, monto_directorio)
     quedan deprecated pero no se borran (no romper backwards-compat).
  3. `solicitud.aprobado_vp_at` (timestamptz): marca de tiempo del OK del VP.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "030_workflow_vp"
down_revision: str | None = "029_auditoria_login"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NEW_ENUM_VALUES = [
    "en_revision_vp",
    "observado_vp",
    "devuelto_vp",
    "en_revision_presidencia",
    "observado_presidencia",
    "devuelto_presidencia",
]


def upgrade() -> None:
    # 1. Agregar valores nuevos al enum (idempotente con IF NOT EXISTS).
    # ALTER TYPE ADD VALUE requiere commit fuera de transacción del bloque
    # actual en versiones viejas de PG; usamos COMMIT explícito como salvaguarda.
    bind = op.get_bind()
    for v in _NEW_ENUM_VALUES:
        bind.exec_driver_sql(
            f"ALTER TYPE planificacion.solicitud_estado_wf ADD VALUE IF NOT EXISTS '{v}'"
        )

    # 2. Columna monto_vp en linea_solicitud (congela el monto al aprobar VP).
    op.add_column(
        "linea_solicitud",
        sa.Column("monto_vp", sa.Numeric(18, 2), nullable=True),
        schema="planificacion",
    )

    # 3. Timestamp del OK del VP en la solicitud.
    op.add_column(
        "solicitud",
        sa.Column("aprobado_vp_at", sa.DateTime(timezone=True), nullable=True),
        schema="planificacion",
    )


def downgrade() -> None:
    # NO se puede DROP VALUE de un enum en Postgres (es una limitación del motor).
    # Si hace falta rollback total, se hace recreando el tipo desde cero, lo que
    # requiere mover datos. Acá solo revertimos las columnas — los enum values
    # nuevos quedan sin uso pero presentes.
    op.drop_column("solicitud", "aprobado_vp_at", schema="planificacion")
    op.drop_column("linea_solicitud", "monto_vp", schema="planificacion")
