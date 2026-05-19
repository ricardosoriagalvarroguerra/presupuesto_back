"""Catálogo de objetivos estratégicos + columna Objetivos en planillas

Revision ID: 014_objetivos_estrategicos
Revises: 013_remove_etapa_objetivos
Create Date: 2026-05-13

Agrega:
- catalogo.objetivo_estrategico: tabla con los 6 objetivos institucionales
  para vincular el esfuerzo monetario del presupuesto con la estrategia.
- Columna "Objetivos" (tipo lookup_objetivo) en los templates de:
    Misiones de Servicio, Misiones de Consultores, Honorarios de Consultores,
    Servicios y Licencias, Reuniones y Eventos, Salarios y Beneficios.
- Renombre del label de "cant_viajes" en planillas de misiones a "N° personas"
  (la clave sigue siendo cant_viajes para compatibilidad de datos existentes).
"""
from collections.abc import Sequence
import json as _json

import sqlalchemy as sa
from alembic import op

revision: str = "014_objetivos_estrategicos"
down_revision: str | None = "013_remove_etapa_objetivos"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OBJETIVOS_SEED = [
    ("OBJ-CREC", "Promover el crecimiento y la integración económica", 1),
    ("OBJ-INCL", "Impulsar la inclusión social",                       2),
    ("OBJ-AMB",  "Fomentar la sostenibilidad ambiental",               3),
    ("OBJ-CUL",  "Cultura",                                            4),
    ("OBJ-EFE",  "Eficiencia y efectividad",                           5),
    ("OBJ-VAL",  "Propuesta de Valor",                                 6),
]

# Códigos de planilla a los que se agrega la columna Objetivos.
PLANILLAS_CON_OBJ = [
    "PL-MISIONES-SERV",
    "PL-MISIONES-CONS",
    "PL-CONSULTORES",
    "PL-SERVICIOS-LIC",
    "PL-REUNIONES-EVENTOS",
    "PL-SALARIOS-BENEF",
]


def upgrade() -> None:
    op.create_table(
        "objetivo_estrategico",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("codigo", sa.String(20), nullable=False, unique=True),
        sa.Column("nombre", sa.Text, nullable=False),
        sa.Column("orden", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("activo", sa.Boolean, nullable=False, server_default=sa.text("true")),
        schema="catalogo",
    )
    op.execute(
        "INSERT INTO catalogo.objetivo_estrategico (codigo, nombre, orden) VALUES "
        + ",".join(f"('{c}', '{n}', {o})" for c, n, o in OBJETIVOS_SEED)
    )

    # Agregar la columna "Objetivos" a cada template afectado y renombrar
    # cant_viajes → "N° personas" en los templates de misiones.
    bind = op.get_bind()
    rows = list(bind.execute(sa.text(
        "SELECT id, codigo, columnas_visibles FROM catalogo.planilla_template "
        f"WHERE codigo = ANY(ARRAY{PLANILLAS_CON_OBJ!r})"
    )))
    nueva_col = {
        "key": "objetivo_id",
        "label": "Objetivos",
        "tipo": "lookup_objetivo",
        "required": False,
    }
    for r in rows:
        # columnas_visibles llega como list (JSONB en Pydantic).
        cols = list(r.columnas_visibles or [])
        # Renombrar label de cant_viajes solo en los dos templates de misiones.
        if r.codigo in ("PL-MISIONES-SERV", "PL-MISIONES-CONS"):
            for c in cols:
                if c.get("key") == "cant_viajes":
                    c["label"] = "N° personas"
        # Insertar Objetivos antes de "justificacion" si existe; si no, al final.
        if any(c.get("key") == "objetivo_id" for c in cols):
            continue  # idempotente
        idx_just = next(
            (i for i, c in enumerate(cols) if c.get("key") == "justificacion"),
            len(cols),
        )
        cols.insert(idx_just, nueva_col.copy())
        bind.execute(
            sa.text(
                "UPDATE catalogo.planilla_template SET columnas_visibles = CAST(:c AS jsonb) "
                "WHERE id = :id"
            ),
            {"c": _json.dumps(cols), "id": r.id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = list(bind.execute(sa.text(
        "SELECT id, codigo, columnas_visibles FROM catalogo.planilla_template "
        f"WHERE codigo = ANY(ARRAY{PLANILLAS_CON_OBJ!r})"
    )))
    for r in rows:
        cols = list(r.columnas_visibles or [])
        if r.codigo in ("PL-MISIONES-SERV", "PL-MISIONES-CONS"):
            for c in cols:
                if c.get("key") == "cant_viajes":
                    c["label"] = "N° viajes"
        cols = [c for c in cols if c.get("key") != "objetivo_id"]
        bind.execute(
            sa.text(
                "UPDATE catalogo.planilla_template SET columnas_visibles = CAST(:c AS jsonb) "
                "WHERE id = :id"
            ),
            {"c": _json.dumps(cols), "id": r.id},
        )
    op.drop_table("objetivo_estrategico", schema="catalogo")
