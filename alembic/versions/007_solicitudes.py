"""solicitudes presupuestarias + lineas + eventos (trazabilidad)

Revision ID: 007_solicitudes
Revises: 006_usuarios_por_vp
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "007_solicitudes"
down_revision: str | None = "006_usuarios_por_vp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # ============================================================
    # planificacion.solicitud — un expediente por (ciclo × VP)
    # ============================================================
    op.create_table(
        "solicitud",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("ciclo_id", sa.Integer, sa.ForeignKey("core.ciclo_presupuestario.id"), nullable=False),
        sa.Column("vp_codigo", sa.String(8), nullable=False),
        sa.Column("nombre", sa.String(255), nullable=False),
        # Etapa del workflow (1..4) — al llegar a 4 (Directorio) la solicitud queda como aprobada
        sa.Column("etapa_actual", sa.SmallInteger, nullable=False, server_default="0"),
        # Estado granular dentro de la etapa
        sa.Column(
            "estado_workflow",
            sa.Enum(
                "en_elaboracion",
                "enviado_revision",
                "en_revision",
                "observado",
                "devuelto",
                "validado",
                "aprobado",
                "cerrado",
                name="solicitud_estado_wf",
                schema="planificacion",
            ),
            nullable=False,
            server_default="en_elaboracion",
        ),
        sa.Column("monto_total", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("monto_aprobado", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("comentario_actual", sa.Text),
        sa.Column("created_by", sa.BigInteger, sa.ForeignKey("core.usuario.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("enviado_a_revision_at", sa.DateTime(timezone=True)),
        sa.Column("aprobado_objetivos_at", sa.DateTime(timezone=True)),
        sa.Column("aprobado_presidencia_at", sa.DateTime(timezone=True)),
        sa.Column("aprobado_directorio_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("ciclo_id", "vp_codigo", "nombre", name="uq_solicitud_ciclo_vp_nombre"),
        schema="planificacion",
    )
    op.create_index("ix_solicitud_ciclo_vp", "solicitud", ["ciclo_id", "vp_codigo"], schema="planificacion")
    op.create_index("ix_solicitud_estado", "solicitud", ["estado_workflow"], schema="planificacion")

    # ============================================================
    # planificacion.linea_solicitud — las filas que se cargan en el wizard
    # ============================================================
    op.create_table(
        "linea_solicitud",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("solicitud_id", sa.BigInteger, sa.ForeignKey("planificacion.solicitud.id", ondelete="CASCADE"), nullable=False),
        sa.Column("planilla_template_id", sa.Integer, sa.ForeignKey("catalogo.planilla_template.id"), nullable=False),
        # Las 4 dimensiones que K2B exige
        sa.Column("item_id", sa.Integer, sa.ForeignKey("catalogo.item_planificacion.id"), nullable=False),
        sa.Column("cuenta_id", sa.Integer, sa.ForeignKey("catalogo.cuenta_planificacion.id"), nullable=False),
        sa.Column("gestor_id", sa.Integer, sa.ForeignKey("catalogo.gestor.id")),
        sa.Column("plan_id", sa.Integer, sa.ForeignKey("catalogo.plan_presupuestario.id"), nullable=False),
        # Modalidad y fórmula
        sa.Column("modalidad", sa.String(16), nullable=False),  # 'parametrizada' | 'directa'
        sa.Column("formula_codigo", sa.String(64)),
        sa.Column("parametros", postgresql.JSONB, server_default="{}"),
        # Montos por etapa (trazabilidad de qué pidió y qué se aprobó)
        sa.Column("monto_solicitado", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("monto_objetivos", sa.Numeric(18, 2)),
        sa.Column("monto_presidencia", sa.Numeric(18, 2)),
        sa.Column("monto_directorio", sa.Numeric(18, 2)),
        sa.Column("justificacion", sa.Text),
        sa.Column(
            "estado_linea",
            sa.Enum(
                "borrador", "validada", "observada", "aprobada", "rechazada",
                name="linea_estado", schema="planificacion",
            ),
            nullable=False,
            server_default="borrador",
        ),
        sa.Column("observacion", sa.Text),
        sa.Column("created_by", sa.BigInteger, sa.ForeignKey("core.usuario.id"), nullable=False),
        sa.Column("updated_by", sa.BigInteger, sa.ForeignKey("core.usuario.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        schema="planificacion",
    )
    op.create_index("ix_linea_solicitud", "linea_solicitud", ["solicitud_id"], schema="planificacion")
    op.create_index("ix_linea_template", "linea_solicitud", ["planilla_template_id"], schema="planificacion")
    op.create_index("ix_linea_item_cuenta", "linea_solicitud", ["item_id", "cuenta_id"], schema="planificacion")

    # ============================================================
    # planificacion.evento_solicitud — log de cada cambio (trazabilidad)
    # ============================================================
    op.create_table(
        "evento_solicitud",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("solicitud_id", sa.BigInteger, sa.ForeignKey("planificacion.solicitud.id", ondelete="CASCADE"), nullable=False),
        sa.Column("linea_id", sa.BigInteger, sa.ForeignKey("planificacion.linea_solicitud.id", ondelete="SET NULL")),
        sa.Column(
            "accion",
            sa.Enum(
                "crear_solicitud",
                "agregar_linea",
                "modificar_linea",
                "eliminar_linea",
                "enviar_a_revision",
                "aprobar_objetivos",
                "aprobar_presidencia",
                "aprobar_directorio",
                "observar",
                "devolver",
                "cerrar",
                name="evento_accion",
                schema="planificacion",
            ),
            nullable=False,
        ),
        sa.Column("etapa_anterior", sa.SmallInteger),
        sa.Column("etapa_nueva", sa.SmallInteger),
        sa.Column("estado_anterior", sa.String(32)),
        sa.Column("estado_nuevo", sa.String(32)),
        sa.Column("payload", postgresql.JSONB),  # diff o snapshot relevante
        sa.Column("usuario_id", sa.BigInteger, sa.ForeignKey("core.usuario.id"), nullable=False),
        sa.Column("comentario", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        schema="planificacion",
    )
    op.create_index("ix_evento_solicitud", "evento_solicitud", ["solicitud_id", "created_at"], schema="planificacion")

    # ============================================================
    # Trigger para mantener updated_at en solicitud y linea
    # ============================================================
    bind.exec_driver_sql("""
        CREATE OR REPLACE FUNCTION planificacion.set_updated_at()
        RETURNS trigger AS $$
        BEGIN
          NEW.updated_at := now();
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_solicitud_updated
          BEFORE UPDATE ON planificacion.solicitud
          FOR EACH ROW EXECUTE FUNCTION planificacion.set_updated_at();

        CREATE TRIGGER trg_linea_updated
          BEFORE UPDATE ON planificacion.linea_solicitud
          FOR EACH ROW EXECUTE FUNCTION planificacion.set_updated_at();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_linea_updated ON planificacion.linea_solicitud")
    op.execute("DROP TRIGGER IF EXISTS trg_solicitud_updated ON planificacion.solicitud")
    op.execute("DROP FUNCTION IF EXISTS planificacion.set_updated_at()")
    op.execute("DROP TABLE IF EXISTS planificacion.evento_solicitud CASCADE")
    op.execute("DROP TABLE IF EXISTS planificacion.linea_solicitud CASCADE")
    op.execute("DROP TABLE IF EXISTS planificacion.solicitud CASCADE")
    op.execute("DROP TYPE IF EXISTS planificacion.solicitud_estado_wf")
    op.execute("DROP TYPE IF EXISTS planificacion.linea_estado")
    op.execute("DROP TYPE IF EXISTS planificacion.evento_accion")
