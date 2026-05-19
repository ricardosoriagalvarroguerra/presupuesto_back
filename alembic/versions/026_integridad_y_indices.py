"""Endurece integridad: UNIQUE estricto, FK ON DELETE, FK planilla, índices.

Revision ID: 026_integridad_y_indices
Revises: 025_angel_vpe_con_extra
Create Date: 2026-05-18

Bloqueantes B4, B6 e importante I11 de la auditoría TI:

1. UNIQUE estricto en planificacion.solicitud(ciclo_id, vp_codigo) — la regla
   "una solicitud por ciclo/VP" pasa de check de aplicación a constraint BD.
2. FK policies normalizadas:
   - hijos de solicitud (lineas, eventos, observaciones, snapshots, adjuntos)
     → ON DELETE CASCADE (consistencia transitiva)
   - referencias a usuario (created_by, updated_by, usuario_id de eventos)
     → ON DELETE SET NULL (preserva auditoría aunque se borre el usuario)
3. FK real en core.usuario_planilla_extra(planilla_codigo)
   → catalogo.planilla_template(codigo) ON DELETE CASCADE.
4. Índices faltantes para queries frecuentes:
   - planificacion.linea_solicitud(planilla_template_id)
   - planificacion.evento_solicitud(solicitud_id)
   - core.usuario_planilla_extra(planilla_codigo)
   - planificacion.solicitud(ciclo_id, vp_codigo) — ya cubierto por UNIQUE
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "026_integridad_y_indices"
down_revision: str | None = "025_angel_vpe_con_extra"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_fk_if_exists(bind, schema: str, table: str, ref_schema: str, ref_table: str) -> None:
    """Busca y dropea cualquier FK que apunte de schema.table → ref_schema.ref_table."""
    rows = bind.execute(sa.text("""
        SELECT conname
        FROM pg_constraint c
        JOIN pg_class t  ON t.oid = c.conrelid  AND t.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname=:s)
        JOIN pg_class rt ON rt.oid = c.confrelid AND rt.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname=:rs)
        WHERE c.contype='f' AND t.relname=:t AND rt.relname=:rt
    """), {"s": schema, "t": table, "rs": ref_schema, "rt": ref_table}).scalars().all()
    for name in rows:
        bind.execute(sa.text(f'ALTER TABLE {schema}.{table} DROP CONSTRAINT IF EXISTS "{name}"'))


def upgrade() -> None:
    bind = op.get_bind()

    # ----- 1. UNIQUE estricto en solicitud -----------------------------------
    # Drop el UNIQUE compuesto viejo si existe (incluía nombre, demasiado laxo)
    old_uq = bind.execute(sa.text("""
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'planificacion.solicitud'::regclass AND contype = 'u'
    """)).scalars().all()
    for name in old_uq:
        bind.execute(sa.text(f'ALTER TABLE planificacion.solicitud DROP CONSTRAINT IF EXISTS "{name}"'))
    # Index único nuevo (= constraint, pero más portable y permite naming claro)
    bind.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_solicitud_ciclo_vp
        ON planificacion.solicitud (ciclo_id, vp_codigo)
    """))

    # ----- 2. FK policies normalizadas ---------------------------------------
    # 2a. Hijos de solicitud → ON DELETE CASCADE
    cascadear = [
        ("planificacion", "linea_solicitud",     "solicitud_id",  "planificacion", "solicitud", "id"),
        ("planificacion", "evento_solicitud",    "solicitud_id",  "planificacion", "solicitud", "id"),
        ("planificacion", "observacion",         "solicitud_id",  "planificacion", "solicitud", "id"),
        ("planificacion", "snapshot_solicitud",  "solicitud_id",  "planificacion", "solicitud", "id"),
        ("planificacion", "snapshot_linea",      "snapshot_id",   "planificacion", "snapshot_solicitud", "id"),
        ("planificacion", "adjunto_linea",       "linea_id",      "planificacion", "linea_solicitud", "id"),
    ]
    for schema, table, col, rs, rt, rcol in cascadear:
        _drop_fk_if_exists(bind, schema, table, rs, rt)
        bind.execute(sa.text(
            f'ALTER TABLE {schema}.{table} '
            f'ADD CONSTRAINT fk_{table}_{col} FOREIGN KEY ({col}) '
            f'REFERENCES {rs}.{rt}({rcol}) ON DELETE CASCADE'
        ))

    # 2b. Referencias a usuario → ON DELETE SET NULL (preserva auditoría)
    nulificar = [
        ("planificacion", "solicitud",        "created_by"),
        ("planificacion", "linea_solicitud",  "created_by"),
        ("planificacion", "linea_solicitud",  "updated_by"),
        ("planificacion", "evento_solicitud", "usuario_id"),
        ("planificacion", "observacion",      "creado_por"),
        ("planificacion", "observacion",      "resuelto_por"),
        ("planificacion", "adjunto_linea",    "creado_por"),
    ]
    for schema, table, col in nulificar:
        # solo si la columna existe (algunos pueden no haberse creado)
        existe = bind.execute(sa.text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_schema=:s AND table_name=:t AND column_name=:c
        """), {"s": schema, "t": table, "c": col}).scalar()
        if not existe:
            continue
        _drop_fk_if_exists(bind, schema, table, "core", "usuario")
        bind.execute(sa.text(
            f'ALTER TABLE {schema}.{table} '
            f'ADD CONSTRAINT fk_{table}_{col} FOREIGN KEY ({col}) '
            f'REFERENCES core.usuario(id) ON DELETE SET NULL'
        ))

    # ----- 3. FK real en usuario_planilla_extra ------------------------------
    _drop_fk_if_exists(bind, "core", "usuario_planilla_extra", "catalogo", "planilla_template")
    bind.execute(sa.text("""
        ALTER TABLE core.usuario_planilla_extra
        ADD CONSTRAINT fk_uplanex_planilla
        FOREIGN KEY (planilla_codigo)
        REFERENCES catalogo.planilla_template(codigo)
        ON DELETE CASCADE ON UPDATE CASCADE
    """))

    # ----- 4. Índices faltantes ----------------------------------------------
    bind.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_linea_solicitud_planilla_tpl "
        "ON planificacion.linea_solicitud (planilla_template_id)"
    ))
    bind.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_linea_solicitud_solicitud "
        "ON planificacion.linea_solicitud (solicitud_id)"
    ))
    bind.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_evento_solicitud_solicitud "
        "ON planificacion.evento_solicitud (solicitud_id)"
    ))
    bind.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_observacion_solicitud "
        "ON planificacion.observacion (solicitud_id)"
    ))
    bind.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_uplanex_planilla "
        "ON core.usuario_planilla_extra (planilla_codigo)"
    ))
    bind.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_linea_solicitud_item_cuenta "
        "ON planificacion.linea_solicitud (item_id, cuenta_id)"
    ))


def downgrade() -> None:
    bind = op.get_bind()

    # 4. Drop índices
    for ix in [
        "ix_linea_solicitud_planilla_tpl",
        "ix_linea_solicitud_solicitud",
        "ix_evento_solicitud_solicitud",
        "ix_observacion_solicitud",
        "ix_uplanex_planilla",
        "ix_linea_solicitud_item_cuenta",
    ]:
        if ix.startswith("ix_uplanex"):
            bind.execute(sa.text(f'DROP INDEX IF EXISTS core.{ix}'))
        else:
            bind.execute(sa.text(f'DROP INDEX IF EXISTS planificacion.{ix}'))

    # 3. Drop FK planilla
    bind.execute(sa.text(
        'ALTER TABLE core.usuario_planilla_extra '
        'DROP CONSTRAINT IF EXISTS fk_uplanex_planilla'
    ))

    # 2. Drop FK normalizadas (sin restituir las viejas — eran inconsistentes)
    for table in [
        "linea_solicitud", "evento_solicitud", "observacion",
        "snapshot_solicitud", "snapshot_linea", "adjunto_linea", "solicitud",
    ]:
        for col in [
            "solicitud_id", "snapshot_id", "linea_id",
            "created_by", "updated_by", "usuario_id", "creado_por", "resuelto_por",
        ]:
            bind.execute(sa.text(
                f'ALTER TABLE planificacion.{table} '
                f'DROP CONSTRAINT IF EXISTS fk_{table}_{col}'
            ))

    # 1. UNIQUE
    bind.execute(sa.text('DROP INDEX IF EXISTS planificacion.uq_solicitud_ciclo_vp'))
