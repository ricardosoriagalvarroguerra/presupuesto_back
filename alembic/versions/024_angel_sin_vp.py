"""Angel Flores: vp_codigo NULL — su scope es solo planillas_extra, no una VP propia

Revision ID: 024_angel_sin_vp
Revises: 023_salarios_matriz_estricta
Create Date: 2026-05-18

Angel es RRHH cargando Salarios en TODAS las VPs. No tiene VP propia; entra a
cada solicitud existente (PRE/VPE/VPD/VPO/VPF) y carga su planilla. Por eso
movemos su vp_codigo a NULL — el filtro queda solo en core.usuario_planilla_extra.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "024_angel_sin_vp"
down_revision: str | None = "023_salarios_matriz_estricta"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("""
        UPDATE core.usuario
        SET vp_codigo = NULL,
            cargo = 'RRHH · Salarios y Beneficios'
        WHERE email = 'jefe.unidad.vpe@fonplata.org'
    """))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("""
        UPDATE core.usuario
        SET vp_codigo = 'VPE',
            cargo = 'Jefe Unidad VPE'
        WHERE email = 'jefe.unidad.vpe@fonplata.org'
    """))
