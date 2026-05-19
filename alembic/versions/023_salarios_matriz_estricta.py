"""PL-SALARIOS-BENEF: matriz estricta (solo items SALARIOS Y BENEFICIOS *)

Revision ID: 023_salarios_matriz_estricta
Revises: 022_salarios_simplificar
Create Date: 2026-05-18

La lista oficial de negocio incluye solo los 6 items "SALARIOS Y BENEFICIOS [VP]".
Se quitan de la matriz item↔cuenta dos entradas extras que apuntaban a 5.2.1.04
(Capacitación) desde items no listados (PRESIDENCIA EJECUTIVA 02.01.02 y
GESTIÓN DE TALENTO HUMANO 02.02.03).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "023_salarios_matriz_estricta"
down_revision: str | None = "022_salarios_simplificar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("""
        DELETE FROM catalogo.relacion_item_cuenta
        WHERE item_id IN (SELECT id FROM catalogo.item_planificacion WHERE codigo IN ('02.01.02','02.02.03'))
          AND cuenta_id IN (SELECT id FROM catalogo.cuenta_planificacion WHERE codigo LIKE :pat)
    """), {"pat": "5.2.1.%"})


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("""
        INSERT INTO catalogo.relacion_item_cuenta (item_id, cuenta_id)
        SELECT i.id, c.id
        FROM catalogo.item_planificacion i, catalogo.cuenta_planificacion c
        WHERE i.codigo IN ('02.01.02','02.02.03') AND c.codigo = '5.2.1.04'
        ON CONFLICT DO NOTHING
    """))
