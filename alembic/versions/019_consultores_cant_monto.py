"""PL-CONSULTORES: reemplaza valor_hora/horas_mes por cantidad/monto_mensual

Revision ID: 019_consultores_cant_monto
Revises: 018_fecha_estimada
Create Date: 2026-05-18

Cambia el modelo de cálculo de Honorarios Consultores:
  Antes: total = valor_hora × horas_mes × meses
  Ahora: total = cantidad (consultores) × monto_mensual × meses
"""
from collections.abc import Sequence

from alembic import op

revision: str = "019_consultores_cant_monto"
down_revision: str | None = "018_fecha_estimada"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COLUMNAS_NEW = """[
  {"key": "item", "tipo": "lookup_item", "label": "Unidad", "required": true},
  {"key": "tipo_consultoria", "tipo": "select", "label": "Tipo", "options": ["individual", "firma"], "required": true},
  {"key": "cantidad", "min": 1, "tipo": "int", "label": "Cantidad", "required": true},
  {"key": "monto_mensual", "tipo": "moneda", "label": "Monto mensual", "required": true},
  {"key": "meses", "max": 12, "min": 1, "tipo": "int", "label": "Meses", "required": true},
  {"key": "monto_total", "tipo": "moneda", "label": "Total USD", "calculado": true},
  {"key": "objetivo_id", "tipo": "lookup_objetivo", "label": "Objetivos", "required": false},
  {"key": "justificacion", "tipo": "text", "label": "Justificación", "required": true}
]"""

COLUMNAS_OLD = """[
  {"key": "item", "tipo": "lookup_item", "label": "Unidad", "required": true},
  {"key": "tipo_consultoria", "tipo": "select", "label": "Tipo", "options": ["individual", "firma"], "required": true},
  {"key": "valor_hora", "tipo": "moneda", "label": "Valor hora USD", "required": true},
  {"key": "horas_mes", "tipo": "int", "label": "Horas/mes", "required": true},
  {"key": "meses", "max": 12, "min": 1, "tipo": "int", "label": "Meses", "required": true},
  {"key": "monto_total", "tipo": "moneda", "label": "Total USD", "calculado": true},
  {"key": "objetivo_id", "tipo": "lookup_objetivo", "label": "Objetivos", "required": false},
  {"key": "justificacion", "tipo": "text", "label": "Justificación", "required": true}
]"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(f"""
        UPDATE catalogo.planilla_template
        SET columnas_visibles = '{COLUMNAS_NEW}'::jsonb
        WHERE codigo = 'PL-CONSULTORES'
    """)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(f"""
        UPDATE catalogo.planilla_template
        SET columnas_visibles = '{COLUMNAS_OLD}'::jsonb
        WHERE codigo = 'PL-CONSULTORES'
    """)
