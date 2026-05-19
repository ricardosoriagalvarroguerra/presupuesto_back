"""ejecucion.movimiento.snapshot_label — soporte para múltiples cortes de ejecución

Revision ID: 009_snapshots
Revises: 008_planillas_extra
Create Date: 2026-05-11

Permite mantener múltiples snapshots del histórico de ejecución K2B (uno por mes/import)
sin perder los anteriores. La UI elige qué snapshot mostrar (default: el más reciente).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "009_snapshots"
down_revision: str | None = "008_planillas_extra"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) columna snapshot_label (no nullable, con default temporal)
    op.add_column(
        "movimiento",
        sa.Column("snapshot_label", sa.String(32), nullable=False, server_default="corte_2026_03"),
        schema="ejecucion",
    )
    # 2) índice para filtrar rápido por snapshot
    op.create_index(
        "ix_mov_snapshot",
        "movimiento",
        ["snapshot_label"],
        schema="ejecucion",
    )
    # 3) k2b_id ya NO puede ser globalmente unique (mismo movimiento puede aparecer en varios snapshots).
    #    Lo cambiamos a unique (k2b_id, snapshot_label).
    # Buscar el nombre real del constraint UNIQUE existente
    bind = op.get_bind()
    cons = bind.exec_driver_sql("""
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'ejecucion.movimiento'::regclass AND contype='u'
    """).fetchall()
    for (name,) in cons:
        if "k2b" in name.lower():
            op.execute(f'ALTER TABLE ejecucion.movimiento DROP CONSTRAINT IF EXISTS "{name}"')
    op.create_unique_constraint(
        "uq_mov_k2b_snapshot",
        "movimiento",
        ["k2b_id", "snapshot_label"],
        schema="ejecucion",
    )


def downgrade() -> None:
    op.drop_constraint("uq_mov_k2b_snapshot", "movimiento", schema="ejecucion")
    op.drop_index("ix_mov_snapshot", table_name="movimiento", schema="ejecucion")
    op.drop_column("movimiento", "snapshot_label", schema="ejecucion")
    op.create_unique_constraint(
        "movimiento_k2b_id_key",
        "movimiento",
        ["k2b_id"],
        schema="ejecucion",
    )
