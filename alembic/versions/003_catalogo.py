"""catalogo: item, cuenta, gestor, plan, tipo_movimiento, posicion, formula, planilla_template

Revision ID: 003_catalogo
Revises: 002_core
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003_catalogo"
down_revision: str | None = "002_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # plan_presupuestario (3 planes paralelos por año)
    op.create_table(
        "plan_presupuestario",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("codigo", sa.String(64), nullable=False, unique=True),
        sa.Column("nombre", sa.String(255), nullable=False),
        sa.Column(
            "tipo",
            sa.Enum("operativo", "capital", "especial", name="plan_tipo", schema="catalogo"),
            nullable=False,
        ),
        sa.Column("k2b_plan_prefix", sa.String(64)),
        sa.Column("estado", sa.String(16), nullable=False, server_default="activo"),
        schema="catalogo",
    )

    # gestor
    op.create_table(
        "gestor",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("codigo", sa.String(64), nullable=False, unique=True),
        sa.Column("nombre", sa.String(255), nullable=False),
        sa.Column("vp_padre", sa.String(255)),
        sa.Column("k2b_gestor_id", sa.Integer),
        sa.Column("estado", sa.String(16), nullable=False, server_default="activo"),
        schema="catalogo",
    )

    # item_planificacion (jerárquico, ltree, profundidad variable)
    op.create_table(
        "item_planificacion",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("codigo", sa.String(64), nullable=False, unique=True),
        sa.Column("descripcion", sa.String(512), nullable=False),
        sa.Column("parent_id", sa.Integer),
        sa.Column("nivel", sa.SmallInteger, nullable=False),
        sa.Column("path_tmp", sa.String(255)),  # placeholder, replaced by ltree below
        sa.Column("imputable", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "tipo_presupuesto",
            sa.Enum("gastos", "inversiones_capital", "salarios", name="item_tipo_presup", schema="catalogo"),
            nullable=False,
        ),
        sa.Column("k2b_item_id", sa.Integer, unique=True),
        sa.Column("estado", sa.String(16), nullable=False, server_default="activo"),
        sa.Column("vigente_desde", sa.Date),
        sa.Column("vigente_hasta", sa.Date),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["parent_id"], ["catalogo.item_planificacion.id"]),
        schema="catalogo",
    )
    # Reemplazar la columna placeholder por una columna ltree real
    op.execute("ALTER TABLE catalogo.item_planificacion DROP COLUMN path_tmp")
    op.execute("ALTER TABLE catalogo.item_planificacion ADD COLUMN path ltree")
    op.execute("CREATE INDEX ix_item_path_gist ON catalogo.item_planificacion USING gist (path)")
    op.execute("CREATE INDEX ix_item_path_btree ON catalogo.item_planificacion USING btree (path)")
    op.execute("CREATE INDEX ix_item_parent ON catalogo.item_planificacion (parent_id)")

    # cuenta_planificacion (jerárquico, chart of accounts)
    op.create_table(
        "cuenta_planificacion",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("codigo", sa.String(32), nullable=False, unique=True),
        sa.Column("descripcion", sa.String(512), nullable=False),
        sa.Column("parent_id", sa.Integer),
        sa.Column("nivel", sa.SmallInteger, nullable=False),
        sa.Column("imputable", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "modalidad_default",
            sa.Enum("parametrizada", "directa", name="cuenta_modalidad", schema="catalogo"),
            nullable=False,
            server_default="directa",
        ),
        sa.Column("k2b_cuenta_id", sa.Integer),
        sa.Column("estado", sa.String(16), nullable=False, server_default="activo"),
        sa.ForeignKeyConstraint(["parent_id"], ["catalogo.cuenta_planificacion.id"]),
        schema="catalogo",
    )
    op.execute("ALTER TABLE catalogo.cuenta_planificacion ADD COLUMN path ltree")
    op.execute("CREATE INDEX ix_cuenta_path_gist ON catalogo.cuenta_planificacion USING gist (path)")
    op.execute("CREATE INDEX ix_cuenta_path_btree ON catalogo.cuenta_planificacion USING btree (path)")

    # relacion_item_cuenta (la matriz del Excel)
    op.create_table(
        "relacion_item_cuenta",
        sa.Column("item_id", sa.Integer, primary_key=True),
        sa.Column("cuenta_id", sa.Integer, primary_key=True),
        sa.Column("obligatoria", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "modalidad_override",
            sa.Enum("parametrizada", "directa", name="cuenta_modalidad", schema="catalogo"),
        ),
        sa.ForeignKeyConstraint(["item_id"], ["catalogo.item_planificacion.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cuenta_id"], ["catalogo.cuenta_planificacion.id"], ondelete="CASCADE"),
        schema="catalogo",
    )
    op.execute("CREATE INDEX ix_ric_cuenta ON catalogo.relacion_item_cuenta (cuenta_id)")

    # gestor_item (qué gestor opera qué items)
    op.create_table(
        "gestor_item",
        sa.Column("gestor_id", sa.Integer, primary_key=True),
        sa.Column("item_id", sa.Integer, primary_key=True),
        sa.ForeignKeyConstraint(["gestor_id"], ["catalogo.gestor.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["catalogo.item_planificacion.id"], ondelete="CASCADE"),
        schema="catalogo",
    )

    # tipo_movimiento (catálogo K2B con 14 valores seed)
    op.create_table(
        "tipo_movimiento",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("k2b_codigo", sa.String(64), nullable=False, unique=True),
        sa.Column("nombre", sa.String(255), nullable=False),
        sa.Column(
            "categoria",
            sa.Enum(
                "inicial",
                "modificacion",
                "compromiso",
                "devengado",
                "pagado",
                "reverso",
                "especial",
                name="tipo_mov_categoria",
                schema="catalogo",
            ),
            nullable=False,
        ),
        sa.Column("signo", sa.SmallInteger, nullable=False),
        sa.Column("estado", sa.String(16), nullable=False, server_default="activo"),
        sa.CheckConstraint("signo IN (-1, 1)", name="ck_signo"),
        schema="catalogo",
    )

    # posicion (Cuadro 13 DPP)
    op.create_table(
        "posicion",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("codigo", sa.String(64), nullable=False, unique=True),
        sa.Column("nombre", sa.String(255), nullable=False),
        sa.Column("grado", sa.String(32)),
        sa.Column("monto_promedio_anual", sa.Numeric(18, 2)),
        sa.Column("estado", sa.String(16), nullable=False, server_default="activo"),
        schema="catalogo",
    )

    # formula
    op.create_table(
        "formula",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("codigo", sa.String(64), nullable=False, unique=True),
        sa.Column("nombre", sa.String(255), nullable=False),
        sa.Column("expresion", sa.Text, nullable=False),
        sa.Column("cuenta_id", sa.Integer),
        sa.Column("vigencia_desde", sa.Date),
        sa.Column("vigencia_hasta", sa.Date),
        sa.Column("created_by", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["cuenta_id"], ["catalogo.cuenta_planificacion.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["core.usuario.id"]),
        schema="catalogo",
    )

    op.create_table(
        "formula_parametro",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("formula_id", sa.Integer, nullable=False),
        sa.Column("codigo", sa.String(64), nullable=False),
        sa.Column(
            "tipo",
            sa.Enum("numero", "fecha", "texto", "lista", "ref_destino", name="param_tipo", schema="catalogo"),
            nullable=False,
        ),
        sa.Column("obligatorio", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("orden", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("unidad", sa.String(32)),
        sa.UniqueConstraint("formula_id", "codigo", name="uq_param_formula_codigo"),
        sa.ForeignKeyConstraint(["formula_id"], ["catalogo.formula.id"], ondelete="CASCADE"),
        schema="catalogo",
    )

    # parametro_destino (tabla de tarifas por país/destino)
    op.create_table(
        "parametro_destino",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("destino", sa.String(8), nullable=False),
        sa.Column(
            "tipo",
            sa.Enum("pasaje", "viatico", "hospedaje", "otros", name="dest_tipo", schema="catalogo"),
            nullable=False,
        ),
        sa.Column("monto", sa.Numeric(18, 2), nullable=False),
        sa.Column("vigente_desde", sa.Date, nullable=False),
        sa.Column("vigente_hasta", sa.Date),
        sa.UniqueConstraint("destino", "tipo", "vigente_desde", name="uq_dest_tipo_vig"),
        schema="catalogo",
    )

    # planilla_template (5 planillas seed)
    op.create_table(
        "planilla_template",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("codigo", sa.String(64), nullable=False, unique=True),
        sa.Column("nombre", sa.String(255), nullable=False),
        sa.Column("descripcion", sa.Text),
        sa.Column("scope_filter", postgresql.JSONB, nullable=False),
        sa.Column(
            "modalidad_permitida",
            sa.Enum("parametrizada", "directa", "ambas", name="planilla_modalidad", schema="catalogo"),
            nullable=False,
        ),
        sa.Column("formula_default_codigo", sa.String(64)),
        sa.Column("columnas_visibles", postgresql.JSONB, nullable=False),
        sa.Column("reglas_validacion", postgresql.JSONB),
        sa.Column("orden", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("vigente_desde", sa.Date),
        sa.Column("vigente_hasta", sa.Date),
        sa.Column("estado", sa.String(16), nullable=False, server_default="activo"),
        schema="catalogo",
    )

    bind = op.get_bind()

    # SEED: tipos de movimiento (14 valores reales del histórico K2B)
    bind.exec_driver_sql(
        """
        INSERT INTO catalogo.tipo_movimiento (k2b_codigo, nombre, categoria, signo) VALUES
          ('PRESUPLIBERACIONPLAN',  'Planificación',           'inicial',     1),
          ('AJUSTECREDITOINICIAL',  'Ajuste de planificación', 'modificacion', 1),
          ('PRESUPORDENCOMPRA',     'Orden de compra',         'compromiso',  1),
          ('PRESUPANUORDENCOMPRA',  'Anula orden de compra',   'reverso',    -1),
          ('PRESUPFACTCONREF',      'Factura orden de compra', 'devengado',   1),
          ('PRESUPFACTSINREF',      'Factura directa',         'devengado',   1),
          ('PRESUPANUDOCCOM',       'Anula factura',           'reverso',    -1),
          ('PRESUPNOTACREDDEVOL',   'Nota de crédito',         'reverso',    -1),
          ('PRESUPCANCNCRED',       'Anula nota de crédito',   'reverso',     1),
          ('PRESUPCANCDOCCOM',      'Pago de factura',         'pagado',      1),
          ('PRESUPMOVFONDOS',       'Pago de gastos',          'pagado',      1),
          ('DEVOLUCIONMOVFONDO',    'Devolución gastos',       'reverso',    -1),
          ('INGRESORRHH',           'Ingreso RRHH',            'especial',    1);
        """
    )

    # SEED: planes presupuestarios K2B
    bind.exec_driver_sql(
        """
        INSERT INTO catalogo.plan_presupuestario (codigo, nombre, tipo, k2b_plan_prefix) VALUES
          ('PRESUPDEGASTOS', 'Presupuesto de Gastos',                  'operativo', 'PRESUPDEGASTOS'),
          ('PRESUPBUSO',     'Presupuesto de Capital',                 'capital',   'PRESUPBUSO'),
          ('PREFONESP',      'Fondo Especial Terminación de Personal', 'especial',  'PREFONESP');
        """
    )

    # SEED: 5 planillas iniciales (usa exec_driver_sql para evitar que SQLAlchemy
    # interprete los ":" del JSON como bind params)
    bind.exec_driver_sql(
        """
        INSERT INTO catalogo.planilla_template (codigo, nombre, descripcion, scope_filter, modalidad_permitida, formula_default_codigo, columnas_visibles, reglas_validacion, orden) VALUES
        ('PL-MISIONES-SERV',
         'Misiones de Servicio',
         'Pasajes, viáticos, hospedaje y otros gastos de viaje del personal en misión.',
         '{"plan_codigo":["PRESUPDEGASTOS"],"cuenta_path":"5.4.1.*"}'::jsonb,
         'parametrizada',
         'MISIONES_PASAJES_VIATICOS_HOSPEDAJE',
         '[
            {"key":"item","label":"Unidad","tipo":"lookup_item","required":true},
            {"key":"destino","label":"Destino","tipo":"lookup_destino","required":true},
            {"key":"cant_viajes","label":"N° viajes","tipo":"int","required":true,"min":1},
            {"key":"duracion_dias","label":"Días","tipo":"int","required":true,"min":1},
            {"key":"tarifa_pasaje","label":"Tarifa pasaje","tipo":"moneda","calculado":true},
            {"key":"tarifa_viatico","label":"Viático/día","tipo":"moneda","calculado":true},
            {"key":"tarifa_hospedaje","label":"Hospedaje/día","tipo":"moneda","calculado":true},
            {"key":"monto_total","label":"Total USD","tipo":"moneda","calculado":true},
            {"key":"justificacion","label":"Justificación","tipo":"text","required":true}
          ]'::jsonb,
         '{"requiere_justificacion":true,"duracion_max":30}'::jsonb,
         1),
        ('PL-MISIONES-CONS',
         'Misiones de Consultores',
         'Pasajes, viáticos y hospedaje de consultores en misión.',
         '{"plan_codigo":["PRESUPDEGASTOS"],"cuenta_path":"5.3.1.0[2-5]"}'::jsonb,
         'parametrizada',
         'MISIONES_PASAJES_VIATICOS_HOSPEDAJE',
         '[
            {"key":"item","label":"Unidad","tipo":"lookup_item","required":true},
            {"key":"destino","label":"Destino","tipo":"lookup_destino","required":true},
            {"key":"cant_viajes","label":"N° viajes","tipo":"int","required":true},
            {"key":"duracion_dias","label":"Días","tipo":"int","required":true},
            {"key":"tarifa_pasaje","label":"Tarifa pasaje","tipo":"moneda","calculado":true},
            {"key":"tarifa_viatico","label":"Viático/día","tipo":"moneda","calculado":true},
            {"key":"tarifa_hospedaje","label":"Hospedaje/día","tipo":"moneda","calculado":true},
            {"key":"monto_total","label":"Total USD","tipo":"moneda","calculado":true},
            {"key":"justificacion","label":"Justificación","tipo":"text","required":true}
          ]'::jsonb,
         '{"requiere_justificacion":true}'::jsonb,
         2),
        ('PL-CONSULTORES',
         'Honorarios de Consultores',
         'Honorarios profesionales (cuenta 5.3.1.01).',
         '{"plan_codigo":["PRESUPDEGASTOS"],"cuenta_path":"5.3.1.01"}'::jsonb,
         'parametrizada',
         'CONSULTORES_HONORARIOS',
         '[
            {"key":"item","label":"Unidad","tipo":"lookup_item","required":true},
            {"key":"tipo_consultoria","label":"Tipo","tipo":"select","options":["individual","firma"],"required":true},
            {"key":"valor_hora","label":"Valor hora USD","tipo":"moneda","required":true},
            {"key":"horas_mes","label":"Horas/mes","tipo":"int","required":true},
            {"key":"meses","label":"Meses","tipo":"int","required":true,"min":1,"max":12},
            {"key":"monto_total","label":"Total USD","tipo":"moneda","calculado":true},
            {"key":"justificacion","label":"Justificación","tipo":"text","required":true}
          ]'::jsonb,
         '{"requiere_justificacion":true}'::jsonb,
         3),
        ('PL-SERVICIOS-LIC',
         'Servicios y Licencias',
         'Servicios contratados, mantenimiento y licencias de software.',
         '{"plan_codigo":["PRESUPDEGASTOS"],"cuenta_path":"5.6.5.*"}'::jsonb,
         'directa',
         null,
         '[
            {"key":"item","label":"Unidad","tipo":"lookup_item","required":true},
            {"key":"cuenta","label":"Cuenta","tipo":"lookup_cuenta","required":true},
            {"key":"proveedor","label":"Proveedor","tipo":"text"},
            {"key":"vigencia_meses","label":"Vigencia (meses)","tipo":"int"},
            {"key":"monto_total","label":"Monto USD","tipo":"moneda","required":true},
            {"key":"justificacion","label":"Justificación","tipo":"text","required":true},
            {"key":"adjunto","label":"Contrato","tipo":"file"}
          ]'::jsonb,
         '{"requiere_justificacion":true,"requiere_adjunto":true}'::jsonb,
         4),
        ('PL-REUNIONES-EVENTOS',
         'Reuniones y Eventos',
         'Eventos institucionales, reuniones de gobernanza y comunicación.',
         '{"plan_codigo":["PRESUPDEGASTOS"],"cuenta_path":"(5.6.1.03|5.5.1.*)"}'::jsonb,
         'directa',
         null,
         '[
            {"key":"item","label":"Unidad","tipo":"lookup_item","required":true},
            {"key":"cuenta","label":"Cuenta","tipo":"lookup_cuenta","required":true},
            {"key":"evento","label":"Evento","tipo":"text","required":true},
            {"key":"fecha_estimada","label":"Fecha estimada","tipo":"date"},
            {"key":"monto_total","label":"Monto USD","tipo":"moneda","required":true},
            {"key":"justificacion","label":"Justificación","tipo":"text","required":true}
          ]'::jsonb,
         '{"requiere_justificacion":true}'::jsonb,
         5);
        """
    )


def downgrade() -> None:
    for t in [
        "planilla_template",
        "parametro_destino",
        "formula_parametro",
        "formula",
        "posicion",
        "tipo_movimiento",
        "gestor_item",
        "relacion_item_cuenta",
        "cuenta_planificacion",
        "item_planificacion",
        "gestor",
        "plan_presupuestario",
    ]:
        op.execute(f'DROP TABLE IF EXISTS catalogo."{t}" CASCADE')
    for e in [
        "planilla_modalidad",
        "dest_tipo",
        "param_tipo",
        "tipo_mov_categoria",
        "cuenta_modalidad",
        "item_tipo_presup",
        "plan_tipo",
    ]:
        op.execute(f'DROP TYPE IF EXISTS catalogo."{e}"')
