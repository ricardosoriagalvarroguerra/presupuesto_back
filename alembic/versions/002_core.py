"""core: usuario, rol, ciclo, periodo, moneda

Revision ID: 002_core
Revises: 001_init_schemas
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002_core"
down_revision: str | None = "001_init_schemas"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "moneda",
        sa.Column("codigo", sa.String(3), primary_key=True),
        sa.Column("nombre", sa.String(64), nullable=False),
        sa.Column("decimales", sa.SmallInteger, nullable=False, server_default="2"),
        schema="core",
    )

    op.create_table(
        "tipo_cambio",
        sa.Column("fecha", sa.Date, primary_key=True),
        sa.Column("moneda_origen", sa.String(3), primary_key=True),
        sa.Column("moneda_destino", sa.String(3), primary_key=True),
        sa.Column("tasa", sa.Numeric(18, 6), nullable=False),
        sa.ForeignKeyConstraint(["moneda_origen"], ["core.moneda.codigo"]),
        sa.ForeignKeyConstraint(["moneda_destino"], ["core.moneda.codigo"]),
        schema="core",
    )

    op.create_table(
        "rol",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("codigo", sa.String(32), nullable=False, unique=True),
        sa.Column("nombre", sa.String(128), nullable=False),
        sa.Column("descripcion", sa.Text),
        schema="core",
    )

    op.create_table(
        "usuario",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("nombre", sa.String(128), nullable=False),
        sa.Column("apellido", sa.String(128), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "estado",
            sa.Enum("activo", "suspendido", "baja", name="usuario_estado", schema="core"),
            nullable=False,
            server_default="activo",
        ),
        sa.Column("mfa_habilitado", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("mfa_secret", sa.String(64)),
        sa.Column("ultimo_login", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        schema="core",
    )

    op.create_table(
        "usuario_rol",
        sa.Column("usuario_id", sa.BigInteger, primary_key=True),
        sa.Column("rol_id", sa.Integer, primary_key=True),
        sa.ForeignKeyConstraint(["usuario_id"], ["core.usuario.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rol_id"], ["core.rol.id"], ondelete="CASCADE"),
        schema="core",
    )

    op.create_table(
        "ciclo_presupuestario",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("anio", sa.SmallInteger, nullable=False, unique=True),
        sa.Column("nombre", sa.String(128), nullable=False),
        sa.Column("fecha_apertura", sa.Date),
        sa.Column("fecha_cierre_solicitud", sa.Date),
        sa.Column("fecha_cierre_directorio", sa.Date),
        sa.Column(
            "estado",
            sa.Enum("planificacion", "vigente", "cerrado", name="ciclo_estado", schema="core"),
            nullable=False,
            server_default="planificacion",
        ),
        sa.Column("created_by", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["created_by"], ["core.usuario.id"]),
        schema="core",
    )

    op.create_table(
        "periodo",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ciclo_id", sa.Integer, nullable=False),
        sa.Column(
            "granularidad",
            sa.Enum("anual", "semestral", "trimestral", "mensual", name="periodo_granularidad", schema="core"),
            nullable=False,
        ),
        sa.Column("numero", sa.SmallInteger, nullable=False),
        sa.Column("fecha_inicio", sa.Date, nullable=False),
        sa.Column("fecha_fin", sa.Date, nullable=False),
        sa.UniqueConstraint("ciclo_id", "granularidad", "numero", name="uq_periodo_ciclo_gran_num"),
        sa.ForeignKeyConstraint(["ciclo_id"], ["core.ciclo_presupuestario.id"]),
        schema="core",
    )

    # seed mínimo
    op.execute(
        """
        INSERT INTO core.moneda (codigo, nombre, decimales) VALUES ('USD', 'Dólar estadounidense', 2);
        INSERT INTO core.rol (codigo, nombre, descripcion) VALUES
          ('solicitante',   'Solicitante',   'Carga necesidades presupuestarias en su unidad.'),
          ('validador',     'Validador',     'Aprueba solicitudes dentro de su unidad.'),
          ('consolidador',  'Consolidador',  'CYP — consolida y exporta a K2B.'),
          ('administrador', 'Administrador', 'Configura reglas de carga, validación y catálogos.'),
          ('auditor',       'Auditor',       'Solo lectura sobre datos y log de auditoría.');
        """
    )


def downgrade() -> None:
    for t in ["periodo", "ciclo_presupuestario", "usuario_rol", "usuario", "rol", "tipo_cambio", "moneda"]:
        op.execute(f'DROP TABLE IF EXISTS core."{t}" CASCADE')
    for e in ["periodo_granularidad", "ciclo_estado", "usuario_estado"]:
        op.execute(f'DROP TYPE IF EXISTS core."{e}"')
