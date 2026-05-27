"""Tabla `planificacion.observacion_respuesta` — hilo de respuestas a observaciones.

Habilita la conversación VP ↔ Presidencia dentro de una observación abierta:
el VP puede responder con contexto sin tener que aplicar o rechazar.
La observación sigue siendo la unidad de "estado" (abierta/aplicada/rechazada);
las respuestas son mensajes informativos atados a ella.
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "002_observacion_respuesta"
down_revision = "001_baseline_mssql"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE planificacion.observacion_respuesta (
            id              BIGINT IDENTITY(1,1) PRIMARY KEY,
            observacion_id  BIGINT NOT NULL,
            autor_id        BIGINT NULL,
            texto           NVARCHAR(MAX) NOT NULL,
            created_at      DATETIMEOFFSET NOT NULL CONSTRAINT df_obs_resp_created_at DEFAULT SYSDATETIMEOFFSET(),
            CONSTRAINT fk_obs_resp_observacion
                FOREIGN KEY (observacion_id)
                REFERENCES planificacion.observacion(id)
                ON DELETE CASCADE,
            CONSTRAINT fk_obs_resp_autor
                FOREIGN KEY (autor_id)
                REFERENCES core.usuario(id)
                ON DELETE SET NULL
        )
    """)
    op.execute("""
        CREATE INDEX ix_obs_resp_observacion
            ON planificacion.observacion_respuesta(observacion_id, created_at)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX ix_obs_resp_observacion ON planificacion.observacion_respuesta")
    op.execute("DROP TABLE planificacion.observacion_respuesta")
