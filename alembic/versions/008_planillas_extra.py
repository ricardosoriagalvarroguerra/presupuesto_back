"""planillas adicionales: salarios y beneficios + gastos de administración (cargadas por VPE)

Revision ID: 008_planillas_extra
Revises: 007_solicitudes
Create Date: 2026-05-10
"""
from collections.abc import Sequence

from alembic import op

revision: str = "008_planillas_extra"
down_revision: str | None = "007_solicitudes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        INSERT INTO catalogo.planilla_template
          (codigo, nombre, descripcion, scope_filter, modalidad_permitida, formula_default_codigo, columnas_visibles, reglas_validacion, orden) VALUES
        ('PL-SALARIOS-BENEF',
         'Salarios y Beneficios',
         'Gastos del personal: salarios, beneficios, capacitación, programa ahorro, pasantes. Cargado por VPE para todas las unidades organizacionales.',
         '{"plan_codigo":["PRESUPDEGASTOS"],"cuenta_path":"5.2.*"}'::jsonb,
         'directa',
         null,
         '[
            {"key":"item","label":"Unidad organizacional","tipo":"lookup_item","required":true},
            {"key":"cuenta","label":"Cuenta","tipo":"lookup_cuenta","required":true},
            {"key":"posiciones","label":"N° posiciones","tipo":"int","required":false,"min":0},
            {"key":"salario_promedio","label":"Salario promedio anual","tipo":"moneda"},
            {"key":"monto_total","label":"Monto solicitado USD","tipo":"moneda","required":true},
            {"key":"justificacion","label":"Justificación","tipo":"text","required":true}
          ]'::jsonb,
         '{"requiere_justificacion":true,"rol_carga":"VPE"}'::jsonb,
         6),

        ('PL-GASTOS-ADMIN',
         'Gastos de Administración',
         'Gastos operativos generales (servicios al edificio, abastecimiento, comunicación). Cargado por VPE para todas las VPs.',
         '{"plan_codigo":["PRESUPDEGASTOS"],"cuenta_path":"5.6.(1|2|3|4)\\\\..*"}'::jsonb,
         'directa',
         null,
         '[
            {"key":"item","label":"Unidad organizacional","tipo":"lookup_item","required":true},
            {"key":"cuenta","label":"Cuenta","tipo":"lookup_cuenta","required":true},
            {"key":"proveedor","label":"Proveedor / contraparte","tipo":"text"},
            {"key":"fecha_estimada","label":"Fecha estimada","tipo":"date"},
            {"key":"monto_total","label":"Monto solicitado USD","tipo":"moneda","required":true},
            {"key":"justificacion","label":"Justificación","tipo":"text","required":true}
          ]'::jsonb,
         '{"requiere_justificacion":true,"rol_carga":"VPE"}'::jsonb,
         7);
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM catalogo.planilla_template WHERE codigo IN ('PL-SALARIOS-BENEF','PL-GASTOS-ADMIN')")
