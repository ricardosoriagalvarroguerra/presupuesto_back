"""PL-SALARIOS-BENEF: simplifica a Item + Cuenta + Total USD, scope estricto

Revision ID: 022_salarios_simplificar
Revises: 021_gastos_admin_columnas_calc
Create Date: 2026-05-18

Cambios PL-SALARIOS-BENEF:
1. Quita columnas: N° posiciones, Salario promedio anual, Objetivos, Justificación.
2. Renombra "Monto solicitado USD" → "Total USD".
3. Restringe scope_filter.cuenta_path a las 5 cuentas 5.2.1.01-05 (antes 5.2.*).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "022_salarios_simplificar"
down_revision: str | None = "021_gastos_admin_columnas_calc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COLUMNAS_NEW = """[
  {"key": "item", "tipo": "lookup_item", "label": "Item", "required": true},
  {"key": "cuenta", "tipo": "lookup_cuenta", "label": "Cuenta", "required": true},
  {"key": "monto_total", "tipo": "moneda", "label": "Total USD", "required": true}
]"""

COLUMNAS_OLD = """[
  {"key": "item", "tipo": "lookup_item", "label": "Item", "required": true},
  {"key": "cuenta", "tipo": "lookup_cuenta", "label": "Cuenta", "required": true},
  {"key": "posiciones", "min": 0, "tipo": "int", "label": "N° posiciones", "required": false},
  {"key": "salario_promedio", "tipo": "moneda", "label": "Salario promedio anual"},
  {"key": "monto_total", "tipo": "moneda", "label": "Monto solicitado USD", "required": true},
  {"key": "objetivo_id", "tipo": "lookup_objetivo", "label": "Objetivos", "required": false},
  {"key": "justificacion", "tipo": "text", "label": "Justificación", "required": true}
]"""

SCOPE_NEW = '{"cuenta_path": "(5.2.1.01|5.2.1.02|5.2.1.03|5.2.1.04|5.2.1.05)", "plan_codigo": ["PRESUPDEGASTOS"]}'
SCOPE_OLD = '{"cuenta_path": "5.2.*", "plan_codigo": ["PRESUPDEGASTOS"]}'


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("UPDATE catalogo.planilla_template SET columnas_visibles = CAST(:cv AS jsonb), scope_filter = CAST(:sf AS jsonb) WHERE codigo = 'PL-SALARIOS-BENEF'"),
        {"cv": COLUMNAS_NEW, "sf": SCOPE_NEW},
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("UPDATE catalogo.planilla_template SET columnas_visibles = CAST(:cv AS jsonb), scope_filter = CAST(:sf AS jsonb) WHERE codigo = 'PL-SALARIOS-BENEF'"),
        {"cv": COLUMNAS_OLD, "sf": SCOPE_OLD},
    )
