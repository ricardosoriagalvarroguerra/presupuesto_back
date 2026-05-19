"""ejecucion: schema con movimientos K2B + ciclo presupuestario 2026 seed

Revision ID: 005_ejecucion
Revises: 004_usuarios_seed
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "005_ejecucion"
down_revision: str | None = "004_usuarios_seed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "movimiento",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("k2b_id", sa.String(64), unique=True, nullable=False),
        sa.Column("ciclo_id", sa.Integer, sa.ForeignKey("core.ciclo_presupuestario.id"), nullable=False),
        sa.Column("plan_id", sa.Integer, sa.ForeignKey("catalogo.plan_presupuestario.id"), nullable=False),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("catalogo.item_planificacion.id")),
        sa.Column("cuenta_id", sa.Integer, sa.ForeignKey("catalogo.cuenta_planificacion.id")),
        sa.Column("vp_codigo", sa.String(255)),
        sa.Column("area", sa.String(255)),
        sa.Column("centro_presupuestal", sa.String(255)),
        sa.Column("subcentro_presupuestal", sa.String(255)),
        sa.Column("fecha_movimiento", sa.DateTime(timezone=True), nullable=False),
        sa.Column("estado", sa.String(32), nullable=False),
        sa.Column("tipo_movimiento_id", sa.Integer, sa.ForeignKey("catalogo.tipo_movimiento.id"), nullable=False),
        sa.Column("monto_inicial",      sa.Numeric(18, 2), server_default="0"),
        sa.Column("monto_ajustes",      sa.Numeric(18, 2), server_default="0"),
        sa.Column("monto_vigente",      sa.Numeric(18, 2), server_default="0"),
        sa.Column("monto_disponible",   sa.Numeric(18, 2), server_default="0"),
        sa.Column("monto_comprometido", sa.Numeric(18, 2), server_default="0"),
        sa.Column("monto_obligado",     sa.Numeric(18, 2), server_default="0"),
        sa.Column("monto_pagado",       sa.Numeric(18, 2), server_default="0"),
        sa.Column("monto_ejecutado",    sa.Numeric(18, 2), server_default="0"),
        sa.Column("documento_tipo", sa.String(128)),
        sa.Column("documento_numero", sa.String(128)),
        sa.Column("concepto", sa.Text),
        sa.Column("persona", sa.String(255)),
        sa.Column("importado_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        schema="ejecucion",
    )
    op.create_index("ix_mov_ciclo_plan", "movimiento", ["ciclo_id", "plan_id"], schema="ejecucion")
    op.create_index("ix_mov_item", "movimiento", ["item_id"], schema="ejecucion")
    op.create_index("ix_mov_cuenta", "movimiento", ["cuenta_id"], schema="ejecucion")
    op.create_index("ix_mov_tipo", "movimiento", ["tipo_movimiento_id"], schema="ejecucion")
    op.create_index("ix_mov_fecha", "movimiento", ["fecha_movimiento"], schema="ejecucion")
    op.create_index("ix_mov_vp", "movimiento", ["vp_codigo"], schema="ejecucion")

    # Seed ciclo 2026 (necesario para el ETL del histórico)
    op.execute(
        """
        INSERT INTO core.ciclo_presupuestario (anio, nombre, estado)
        VALUES (2026, 'Presupuesto Institucional 2026', 'vigente')
        ON CONFLICT (anio) DO NOTHING;
        INSERT INTO core.ciclo_presupuestario (anio, nombre, estado)
        VALUES (2027, 'Presupuesto Institucional 2027', 'planificacion')
        ON CONFLICT (anio) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute('DROP TABLE IF EXISTS ejecucion.movimiento CASCADE')
    op.execute("DELETE FROM core.ciclo_presupuestario WHERE anio IN (2026, 2027)")
