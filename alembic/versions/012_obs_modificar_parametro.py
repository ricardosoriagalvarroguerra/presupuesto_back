"""Agrega 'modificar_parametro' al enum observacion_accion

Revision ID: 012_obs_modificar_parametro
Revises: 011_observaciones_snapshots
Create Date: 2026-05-12

Permite observaciones específicas sobre parámetros de línea
(ej. cambiar cant_viajes, duracion_dias, horas_mes, meses) en vez de
modificar el monto directamente. Útil para planillas parametrizadas
donde el monto se deriva de los parámetros.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "012_obs_modificar_parametro"
down_revision: str | None = "011_observaciones_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE planificacion.observacion_accion ADD VALUE IF NOT EXISTS 'modificar_parametro'"
        )


def downgrade() -> None:
    # PostgreSQL no soporta DROP VALUE en enums; queda disponible pero inocuo.
    pass
