"""agrega columna 'fecha_estimada' (date) entre Días y Pasajes en planillas de Misiones

Revision ID: 018_fecha_estimada
Revises: 017_perdiem_columna
Create Date: 2026-05-18

La fecha estimada del viaje permite usar el precio mensual del pasaje (si está
disponible en parametros_Misiones.xlsx) y caer al anual cuando no hay dato del
mes específico. Solo afecta al cálculo paramétrico — la fecha no se imputa
al backend más que como parámetro JSONB de la línea.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "018_fecha_estimada"
down_revision: str | None = "017_perdiem_columna"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COLUMNAS_MISIONES_SERV = """[
  {"key": "item", "tipo": "lookup_item", "label": "Unidad", "required": true},
  {"key": "destino", "tipo": "lookup_destino", "label": "Destino", "required": true},
  {"key": "cant_viajes", "min": 1, "tipo": "int", "label": "N° personas", "required": true},
  {"key": "duracion_dias", "min": 1, "tipo": "int", "label": "Días", "required": true},
  {"key": "fecha_estimada", "tipo": "date", "label": "Fecha estimada", "required": false},
  {"key": "tarifa_pasaje", "tipo": "moneda", "label": "Tarifa pasaje", "calculado": true},
  {"key": "tarifa_viatico", "tipo": "moneda", "label": "Viático/día", "calculado": true},
  {"key": "tarifa_hospedaje", "tipo": "moneda", "label": "Hospedaje/día", "calculado": true},
  {"key": "tarifa_perdiem", "tipo": "moneda", "label": "Per diem y Otros", "calculado": true},
  {"key": "monto_total", "tipo": "moneda", "label": "Total USD", "calculado": true},
  {"key": "objetivo_id", "tipo": "lookup_objetivo", "label": "Objetivos", "required": false},
  {"key": "justificacion", "tipo": "text", "label": "Justificación", "required": true}
]"""

COLUMNAS_MISIONES_CONS = """[
  {"key": "item", "tipo": "lookup_item", "label": "Unidad", "required": true},
  {"key": "destino", "tipo": "lookup_destino", "label": "Destino", "required": true},
  {"key": "cant_viajes", "tipo": "int", "label": "N° personas", "required": true},
  {"key": "duracion_dias", "tipo": "int", "label": "Días", "required": true},
  {"key": "fecha_estimada", "tipo": "date", "label": "Fecha estimada", "required": false},
  {"key": "tarifa_pasaje", "tipo": "moneda", "label": "Tarifa pasaje", "calculado": true},
  {"key": "tarifa_viatico", "tipo": "moneda", "label": "Viático/día", "calculado": true},
  {"key": "tarifa_hospedaje", "tipo": "moneda", "label": "Hospedaje/día", "calculado": true},
  {"key": "tarifa_perdiem", "tipo": "moneda", "label": "Per diem y Otros", "calculado": true},
  {"key": "monto_total", "tipo": "moneda", "label": "Total USD", "calculado": true},
  {"key": "objetivo_id", "tipo": "lookup_objetivo", "label": "Objetivos", "required": false},
  {"key": "justificacion", "tipo": "text", "label": "Justificación", "required": true}
]"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(f"""
        UPDATE catalogo.planilla_template
        SET columnas_visibles = '{COLUMNAS_MISIONES_SERV}'::jsonb
        WHERE codigo = 'PL-MISIONES-SERV'
    """)
    bind.exec_driver_sql(f"""
        UPDATE catalogo.planilla_template
        SET columnas_visibles = '{COLUMNAS_MISIONES_CONS}'::jsonb
        WHERE codigo = 'PL-MISIONES-CONS'
    """)


def downgrade() -> None:
    # rollback: vuelve a la versión sin fecha_estimada (la del 017)
    pass
