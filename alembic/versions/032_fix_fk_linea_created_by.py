"""Repara la FK perdida planificacion.linea_solicitud.created_by → core.usuario.

Revision ID: 032_fix_fk_linea_created_by
Revises: 031_evento_accion_vp
Create Date: 2026-05-18

Bug en migración 026: el helper `_drop_fk_if_exists(schema, table, "core", "usuario")`
dropea TODAS las FKs de esa tabla hacia core.usuario. El loop sobre `nulificar`
procesaba (linea_solicitud, created_by) primero y la creaba; en la iteración
siguiente para (linea_solicitud, updated_by) ese drop volvía a eliminar la
recién creada, dejando solo updated_by con FK.

Esta migración es idempotente: si la FK existe ya no hace nada.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "032_fix_fk_linea_created_by"
down_revision: str | None = "031_evento_accion_vp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # ¿Ya existe alguna FK desde linea_solicitud(created_by) → core.usuario?
    existe = bind.exec_driver_sql("""
        SELECT 1
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attnum = ANY(c.conkey) AND a.attrelid = c.conrelid
        WHERE c.contype = 'f'
          AND c.conrelid = 'planificacion.linea_solicitud'::regclass
          AND c.confrelid = 'core.usuario'::regclass
          AND a.attname = 'created_by'
        LIMIT 1
    """).scalar()
    if existe:
        return
    bind.exec_driver_sql("""
        ALTER TABLE planificacion.linea_solicitud
          ADD CONSTRAINT fk_linea_solicitud_created_by
          FOREIGN KEY (created_by)
          REFERENCES core.usuario(id)
          ON DELETE SET NULL
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE planificacion.linea_solicitud
          DROP CONSTRAINT IF EXISTS fk_linea_solicitud_created_by
    """)
