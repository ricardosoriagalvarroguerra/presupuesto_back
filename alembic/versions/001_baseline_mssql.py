"""Baseline para SQL Server.

El DDL completo (schemas core/catalogo/planificacion, tablas, FKs, índices,
CHECK constraints reemplazando los enums PG) fue aplicado fuera de Alembic
mediante `migracion_sqlserver/ddl_sqlserver.sql`. Esta migración existe solo
como cabecera de la nueva cadena Alembic post-migración a SQL Server.

Después de aplicar el DDL y migrar los datos, correr UNA sola vez:

    alembic stamp head

para marcar este baseline como aplicado sin re-ejecutar nada. A partir de
ahí, las migraciones nuevas (002+) se generan/aplican normalmente.
"""
from __future__ import annotations

# revision identifiers, used by Alembic.
revision = "001_baseline_mssql"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
