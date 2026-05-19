"""rename label 'Unidad'/'Unidad organizacional' → 'Item' + scope estricto PL-GASTOS-ADMIN

Revision ID: 020_gastos_admin_item
Revises: 019_consultores_cant_monto
Create Date: 2026-05-18
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "020_gastos_admin_item"
down_revision: str | None = "019_consultores_cant_monto"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RENAMES = [
    ('"label": "Unidad organizacional"', '"label": "Item"'),
    ('"label": "Unidad"', '"label": "Item"'),
]

SCOPE_FILTER_ADMIN_NEW = (
    '{"cuenta_path": "(5.6.3.01|5.6.3.02|5.6.3.03|5.6.3.04|5.6.3.06|5.6.3.07|5.6.3.08|5.6.3.09|5.6.3.10)", '
    '"plan_codigo": ["PRESUPDEGASTOS"]}'
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Rename labels en columnas_visibles. Usamos parámetros bindeados para
    #    evitar que SQLAlchemy interprete `%` como placeholder.
    for old, new in RENAMES:
        bind.execute(
            sa.text("""
                UPDATE catalogo.planilla_template
                SET columnas_visibles = CAST(REPLACE(columnas_visibles::text, :old, :new) AS jsonb)
                WHERE columnas_visibles::text LIKE :pat
            """),
            {"old": old, "new": new, "pat": f"%{old}%"},
        )

    # 2. Restringir cuentas válidas para PL-GASTOS-ADMIN.
    bind.execute(
        sa.text("UPDATE catalogo.planilla_template SET scope_filter = CAST(:sf AS jsonb) WHERE codigo = 'PL-GASTOS-ADMIN'"),
        {"sf": SCOPE_FILTER_ADMIN_NEW},
    )

    # 3. Quitar (02.02.04.*, 5.6.3.11) de la matriz item↔cuenta.
    bind.execute(sa.text("""
        DELETE FROM catalogo.relacion_item_cuenta
        WHERE item_id IN (SELECT id FROM catalogo.item_planificacion WHERE codigo LIKE :ipat)
          AND cuenta_id = (SELECT id FROM catalogo.cuenta_planificacion WHERE codigo = '5.6.3.11')
    """), {"ipat": "02.02.04.%"})


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("""
        UPDATE catalogo.planilla_template
        SET columnas_visibles = REPLACE(columnas_visibles::text, '"label": "Item"', '"label": "Unidad"')::jsonb
        WHERE columnas_visibles::text LIKE :pat
    """), {"pat": '%"label": "Item"%'})
    bind.execute(sa.text("""
        UPDATE catalogo.planilla_template
        SET scope_filter = CAST(:sf AS jsonb)
        WHERE codigo = 'PL-GASTOS-ADMIN'
    """), {"sf": '{"cuenta_path": "5.6.(1|2|3|4)\\\\..*", "plan_codigo": ["PRESUPDEGASTOS"]}'})
