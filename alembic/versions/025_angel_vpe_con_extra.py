"""Angel Flores: vuelve a VPE — carga todas las planillas en su VP + Salarios cross-VP

Revision ID: 025_angel_vpe_con_extra
Revises: 024_angel_sin_vp
Create Date: 2026-05-18

Refina el modelo de Angel:
  - vp_codigo = VPE → en la solicitud de VPE actúa como responsable normal y
    carga todas las planillas (Misiones, Consultores, Honorarios, Servicios y
    Licencias, Reuniones y Eventos, Salarios, etc.).
  - planillas_extra = [PL-SALARIOS-BENEF] → además, entra a las solicitudes de
    PRE/VPD/VPO/VPF y solo puede cargar la planilla de Salarios y Beneficios.

La regla "una solicitud por (ciclo, VP)" ya impide duplicados, así que si
existe la solicitud VPE Angel la abre, no crea otra.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "025_angel_vpe_con_extra"
down_revision: str | None = "024_angel_sin_vp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("""
        UPDATE core.usuario
        SET vp_codigo = 'VPE',
            cargo = 'Jefe Unidad VPE · RRHH Salarios cross-VP'
        WHERE email = 'jefe.unidad.vpe@fonplata.org'
    """))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("""
        UPDATE core.usuario
        SET vp_codigo = NULL,
            cargo = 'RRHH · Salarios y Beneficios'
        WHERE email = 'jefe.unidad.vpe@fonplata.org'
    """))
