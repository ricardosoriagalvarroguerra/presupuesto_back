"""agrega columna 'tarifa_perdiem' (Per diem y Otros) a planillas de Misiones

Revision ID: 017_perdiem_columna
Revises: 016_username_passwords
Create Date: 2026-05-18

Cambios:
1. PL-MISIONES-SERV: inserta `tarifa_perdiem` después de `tarifa_hospedaje`.
   - Splitter en el frontend imputa el monto a la cuenta 5.4.1.04
     (Otros Gastos de Viaje Misiones), con item el que la fila eligió.
2. PL-MISIONES-CONS: mismo cambio, imputación va a la cuenta 5.3.1.05
     (Otros Gastos Viaje Consultores).
3. Las relaciones item↔cuenta para ambas cuentas ya existen en
   catalogo.relacion_item_cuenta (cargadas desde relaciones.xlsx).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "017_perdiem_columna"
down_revision: str | None = "016_username_passwords"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COLUMNAS_MISIONES_SERV = """[
  {"key": "item", "tipo": "lookup_item", "label": "Unidad", "required": true},
  {"key": "destino", "tipo": "lookup_destino", "label": "Destino", "required": true},
  {"key": "cant_viajes", "min": 1, "tipo": "int", "label": "N° personas", "required": true},
  {"key": "duracion_dias", "min": 1, "tipo": "int", "label": "Días", "required": true},
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
  {"key": "tarifa_pasaje", "tipo": "moneda", "label": "Tarifa pasaje", "calculado": true},
  {"key": "tarifa_viatico", "tipo": "moneda", "label": "Viático/día", "calculado": true},
  {"key": "tarifa_hospedaje", "tipo": "moneda", "label": "Hospedaje/día", "calculado": true},
  {"key": "tarifa_perdiem", "tipo": "moneda", "label": "Per diem y Otros", "calculado": true},
  {"key": "monto_total", "tipo": "moneda", "label": "Total USD", "calculado": true},
  {"key": "objetivo_id", "tipo": "lookup_objetivo", "label": "Objetivos", "required": false},
  {"key": "justificacion", "tipo": "text", "label": "Justificación", "required": true}
]"""


COLUMNAS_MISIONES_SERV_OLD = """[
  {"key": "item", "tipo": "lookup_item", "label": "Unidad", "required": true},
  {"key": "destino", "tipo": "lookup_destino", "label": "Destino", "required": true},
  {"key": "cant_viajes", "min": 1, "tipo": "int", "label": "N° personas", "required": true},
  {"key": "duracion_dias", "min": 1, "tipo": "int", "label": "Días", "required": true},
  {"key": "tarifa_pasaje", "tipo": "moneda", "label": "Tarifa pasaje", "calculado": true},
  {"key": "tarifa_viatico", "tipo": "moneda", "label": "Viático/día", "calculado": true},
  {"key": "tarifa_hospedaje", "tipo": "moneda", "label": "Hospedaje/día", "calculado": true},
  {"key": "monto_total", "tipo": "moneda", "label": "Total USD", "calculado": true},
  {"key": "objetivo_id", "tipo": "lookup_objetivo", "label": "Objetivos", "required": false},
  {"key": "justificacion", "tipo": "text", "label": "Justificación", "required": true}
]"""

COLUMNAS_MISIONES_CONS_OLD = """[
  {"key": "item", "tipo": "lookup_item", "label": "Unidad", "required": true},
  {"key": "destino", "tipo": "lookup_destino", "label": "Destino", "required": true},
  {"key": "cant_viajes", "tipo": "int", "label": "N° personas", "required": true},
  {"key": "duracion_dias", "tipo": "int", "label": "Días", "required": true},
  {"key": "tarifa_pasaje", "tipo": "moneda", "label": "Tarifa pasaje", "calculado": true},
  {"key": "tarifa_viatico", "tipo": "moneda", "label": "Viático/día", "calculado": true},
  {"key": "tarifa_hospedaje", "tipo": "moneda", "label": "Hospedaje/día", "calculado": true},
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
    bind = op.get_bind()
    bind.exec_driver_sql(f"""
        UPDATE catalogo.planilla_template
        SET columnas_visibles = '{COLUMNAS_MISIONES_SERV_OLD}'::jsonb
        WHERE codigo = 'PL-MISIONES-SERV'
    """)
    bind.exec_driver_sql(f"""
        UPDATE catalogo.planilla_template
        SET columnas_visibles = '{COLUMNAS_MISIONES_CONS_OLD}'::jsonb
        WHERE codigo = 'PL-MISIONES-CONS'
    """)
