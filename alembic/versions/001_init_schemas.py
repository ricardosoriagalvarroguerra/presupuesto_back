"""init schemas + ltree extension

Revision ID: 001_init_schemas
Revises:
Create Date: 2026-05-10
"""
from collections.abc import Sequence

from alembic import op

revision: str = "001_init_schemas"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMAS = [
    "core",
    "catalogo",
    "planificacion",
    "workflow",
    "ejecucion",
    "analisis",
    "integracion_k2b",
    "auditoria",
]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS ltree")
    for s in SCHEMAS:
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{s}"')


def downgrade() -> None:
    for s in reversed(SCHEMAS):
        op.execute(f'DROP SCHEMA IF EXISTS "{s}" CASCADE')
