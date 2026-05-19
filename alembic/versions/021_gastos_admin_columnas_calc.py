"""PL-GASTOS-ADMIN: agrega presupuesto_anterior, pct_incremento, monto_fijo

Revision ID: 021_gastos_admin_columnas_calc
Revises: 020_gastos_admin_item
Create Date: 2026-05-18

Cambios columnas_visibles de PL-GASTOS-ADMIN:
  + presupuesto_anterior: USD del ciclo anterior
  + pct_incremento:       % incremento sobre el presupuesto anterior (0 = mantener)
  + monto_fijo:           override directo (tiene prioridad si está cargado)
  ~ monto_total ahora es CALCULADO (no editable):
      Si monto_fijo está cargado y > 0  →  monto_fijo
      Si no                             →  presupuesto_anterior × (1 + pct_incremento/100)
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "021_gastos_admin_columnas_calc"
down_revision: str | None = "020_gastos_admin_item"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COLUMNAS_NEW = """[
  {"key": "item", "tipo": "lookup_item", "label": "Item", "required": true},
  {"key": "cuenta", "tipo": "lookup_cuenta", "label": "Cuenta", "required": true},
  {"key": "proveedor", "tipo": "text", "label": "Proveedor / contraparte"},
  {"key": "fecha_estimada", "tipo": "date", "label": "Fecha estimada"},
  {"key": "presupuesto_anterior", "tipo": "moneda", "label": "Presupuesto anterior"},
  {"key": "pct_incremento", "tipo": "moneda", "label": "% Incremento"},
  {"key": "monto_fijo", "tipo": "moneda", "label": "Monto fijo"},
  {"key": "monto_total", "tipo": "moneda", "label": "Total USD", "calculado": true},
  {"key": "justificacion", "tipo": "text", "label": "Justificación", "required": true}
]"""

COLUMNAS_OLD = """[
  {"key": "item", "tipo": "lookup_item", "label": "Item", "required": true},
  {"key": "cuenta", "tipo": "lookup_cuenta", "label": "Cuenta", "required": true},
  {"key": "proveedor", "tipo": "text", "label": "Proveedor / contraparte"},
  {"key": "fecha_estimada", "tipo": "date", "label": "Fecha estimada"},
  {"key": "monto_total", "tipo": "moneda", "label": "Monto solicitado USD", "required": true},
  {"key": "justificacion", "tipo": "text", "label": "Justificación", "required": true}
]"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("UPDATE catalogo.planilla_template SET columnas_visibles = CAST(:cv AS jsonb) WHERE codigo = 'PL-GASTOS-ADMIN'"),
        {"cv": COLUMNAS_NEW},
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("UPDATE catalogo.planilla_template SET columnas_visibles = CAST(:cv AS jsonb) WHERE codigo = 'PL-GASTOS-ADMIN'"),
        {"cv": COLUMNAS_OLD},
    )
