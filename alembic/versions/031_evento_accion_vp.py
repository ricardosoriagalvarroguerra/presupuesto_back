"""Agrega los nuevos valores de acción del workflow al enum evento_accion.

Revision ID: 031_evento_accion_vp
Revises: 030_workflow_vp
Create Date: 2026-05-18

Sin esto, el INSERT en planificacion.evento_solicitud falla con
"invalid input value for enum planificacion.evento_accion" cuando el código
nuevo registra cualquier transición del workflow nuevo.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "031_evento_accion_vp"
down_revision: str | None = "030_workflow_vp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NUEVOS = [
    "enviar_a_revision_vp",
    "enviar_a_revision_presidencia",
    "aprobar_vp",
    "observar_vp",
    "devolver_vp",
    "observar_presidencia",
    "devolver_presidencia",
]


def upgrade() -> None:
    bind = op.get_bind()
    for v in _NUEVOS:
        bind.exec_driver_sql(
            f"ALTER TYPE planificacion.evento_accion ADD VALUE IF NOT EXISTS '{v}'"
        )


def downgrade() -> None:
    # PG no permite DROP VALUE en un enum — quedan presentes (inocuos si no se usan).
    pass
