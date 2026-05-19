"""planificacion.observacion + snapshot_solicitud/linea — flujo de revisión Presidencia

Revision ID: 011_observaciones_snapshots
Revises: 010_adjuntos_linea
Create Date: 2026-05-12

Agrega el ciclo de revisión con ajustes:
- `observacion` guarda comentarios/sugerencias (eliminar_linea, modificar_monto,
  reducir_total_planilla, solo_comentario) que Presidencia deja sobre una
  solicitud; el VP las aplica o rechaza.
- `snapshot_solicitud` + `snapshot_linea` capturan la solicitud completa en
  cada hito (envío a revisión, devolución con observaciones, reaprobación tras
  ajustes, aprobación final) para poder comparar "Solicitado vs Aprobado vs
  Final" en los dashboards de reportería.

Extiende también el enum `evento_accion` con los nuevos eventos auditables.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "011_observaciones_snapshots"
down_revision: str | None = "010_adjuntos_linea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) Enums propios de observación
    op.execute(
        "CREATE TYPE planificacion.observacion_alcance AS ENUM ('general','planilla','linea')"
    )
    op.execute(
        "CREATE TYPE planificacion.observacion_accion AS ENUM "
        "('eliminar_linea','modificar_monto','reducir_total_planilla','solo_comentario')"
    )
    op.execute(
        "CREATE TYPE planificacion.observacion_estado AS ENUM ('abierta','aplicada','rechazada')"
    )
    op.execute(
        "CREATE TYPE planificacion.snapshot_motivo AS ENUM "
        "('enviado_revision','devuelto_con_observaciones','reaprobado_post_ajustes','aprobado_directorio')"
    )

    # 2) Extender enum evento_accion con los nuevos eventos auditables
    with op.get_context().autocommit_block():
        for v in (
            "crear_observacion",
            "aplicar_observacion",
            "rechazar_observacion",
            "snapshot",
        ):
            op.execute(
                f"ALTER TYPE planificacion.evento_accion ADD VALUE IF NOT EXISTS '{v}'"
            )

    # 3) Tabla de observaciones
    op.create_table(
        "observacion",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "solicitud_id",
            sa.BigInteger,
            sa.ForeignKey("planificacion.solicitud.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "linea_id",
            sa.BigInteger,
            sa.ForeignKey("planificacion.linea_solicitud.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "planilla_template_id",
            sa.BigInteger,
            sa.ForeignKey("catalogo.planilla_template.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "alcance",
            postgresql.ENUM(name="observacion_alcance", schema="planificacion", create_type=False),
            nullable=False,
        ),
        sa.Column("texto", sa.Text, nullable=False),
        sa.Column(
            "accion_sugerida",
            postgresql.ENUM(name="observacion_accion", schema="planificacion", create_type=False),
            nullable=True,
        ),
        sa.Column(
            "valor_sugerido",
            postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "estado",
            postgresql.ENUM(name="observacion_estado", schema="planificacion", create_type=False),
            nullable=False,
            server_default="abierta",
        ),
        sa.Column("etapa_origen", sa.SmallInteger, nullable=False, server_default="3"),
        sa.Column(
            "created_by",
            sa.BigInteger,
            sa.ForeignKey("core.usuario.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "resuelta_por",
            sa.BigInteger,
            sa.ForeignKey("core.usuario.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resuelta_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolucion_comentario", sa.Text, nullable=True),
        schema="planificacion",
    )
    op.create_index(
        "ix_observacion_solicitud", "observacion", ["solicitud_id", "estado"],
        schema="planificacion",
    )

    # 4) Tabla de snapshots de solicitud
    op.create_table(
        "snapshot_solicitud",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "solicitud_id",
            sa.BigInteger,
            sa.ForeignKey("planificacion.solicitud.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("etapa", sa.SmallInteger, nullable=False),
        sa.Column(
            "motivo",
            postgresql.ENUM(name="snapshot_motivo", schema="planificacion", create_type=False),
            nullable=False,
        ),
        sa.Column("monto_total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column(
            "created_by",
            sa.BigInteger,
            sa.ForeignKey("core.usuario.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        schema="planificacion",
    )
    op.create_index(
        "ix_snapshot_solicitud_sid", "snapshot_solicitud", ["solicitud_id"],
        schema="planificacion",
    )

    # 5) Réplica inmutable de cada línea al momento del snapshot
    op.create_table(
        "snapshot_linea",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.BigInteger,
            sa.ForeignKey("planificacion.snapshot_solicitud.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "linea_id",
            sa.BigInteger,
            sa.ForeignKey("planificacion.linea_solicitud.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("item_codigo", sa.String(40), nullable=True),
        sa.Column("cuenta_codigo", sa.String(40), nullable=True),
        sa.Column("plan_codigo", sa.String(40), nullable=True),
        sa.Column("parametros", postgresql.JSONB, nullable=True),
        sa.Column("monto_solicitado", sa.Numeric(18, 2), nullable=True),
        sa.Column("monto_objetivos", sa.Numeric(18, 2), nullable=True),
        sa.Column("monto_presidencia", sa.Numeric(18, 2), nullable=True),
        sa.Column("monto_directorio", sa.Numeric(18, 2), nullable=True),
        sa.Column("justificacion", sa.Text, nullable=True),
        schema="planificacion",
    )
    op.create_index(
        "ix_snapshot_linea_snapshot", "snapshot_linea", ["snapshot_id"],
        schema="planificacion",
    )


def downgrade() -> None:
    op.drop_index("ix_snapshot_linea_snapshot", table_name="snapshot_linea", schema="planificacion")
    op.drop_table("snapshot_linea", schema="planificacion")
    op.drop_index("ix_snapshot_solicitud_sid", table_name="snapshot_solicitud", schema="planificacion")
    op.drop_table("snapshot_solicitud", schema="planificacion")
    op.drop_index("ix_observacion_solicitud", table_name="observacion", schema="planificacion")
    op.drop_table("observacion", schema="planificacion")
    op.execute("DROP TYPE planificacion.snapshot_motivo")
    op.execute("DROP TYPE planificacion.observacion_estado")
    op.execute("DROP TYPE planificacion.observacion_accion")
    op.execute("DROP TYPE planificacion.observacion_alcance")
    # No removemos los valores nuevos del enum evento_accion (PostgreSQL no soporta
    # DROP VALUE en enums; quedan disponibles pero inocuos).
