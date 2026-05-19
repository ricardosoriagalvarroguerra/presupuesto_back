"""planificacion.adjunto_linea — adjuntos (PDF/Word/Excel) por línea de solicitud

Revision ID: 010_adjuntos_linea
Revises: 009_snapshots
Create Date: 2026-05-12

Permite cargar documentos (justificativos, contratos, TDR, etc.) atados a una
línea de solicitud presupuestaria. Para planillas con splitter (Misiones),
los adjuntos se asocian a la línea representativa del grupo.

El archivo binario vive en filesystem (storage/adjuntos/{sid}/{lid}/{uuid}_filename);
la tabla guarda solo metadata.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "010_adjuntos_linea"
down_revision: str | None = "009_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Extender el enum evento_accion para registrar subida/eliminación de adjuntos.
    # ALTER TYPE ADD VALUE no puede correr dentro de una transacción → autocommit.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE planificacion.evento_accion ADD VALUE IF NOT EXISTS 'subir_adjunto'")
        op.execute("ALTER TYPE planificacion.evento_accion ADD VALUE IF NOT EXISTS 'eliminar_adjunto'")

    op.create_table(
        "adjunto_linea",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "linea_id",
            sa.BigInteger,
            sa.ForeignKey("planificacion.linea_solicitud.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("nombre_original", sa.String(255), nullable=False),
        sa.Column("tipo_mime", sa.String(120), nullable=False),
        sa.Column("tamano_bytes", sa.BigInteger, nullable=False),
        sa.Column("path_relativo", sa.String(500), nullable=False),
        sa.Column(
            "subido_por",
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
        "ix_adjunto_linea_linea",
        "adjunto_linea",
        ["linea_id"],
        schema="planificacion",
    )


def downgrade() -> None:
    op.drop_index("ix_adjunto_linea_linea", table_name="adjunto_linea", schema="planificacion")
    op.drop_table("adjunto_linea", schema="planificacion")
