"""Tabla auditoria.login_evento — registro inmutable de intentos de login.

Revision ID: 029_auditoria_login
Revises: 028_tarifas_misiones
Create Date: 2026-05-18

Cierra el hallazgo de auditoría TI: no había trazabilidad de logins. Cada
intento (éxito o fallo, con o sin MFA) deja un evento con IP + user-agent +
usuario_intentado para soportar análisis de fuerza-bruta y cumplimiento SOX.
La tabla no se actualiza ni borra — es append-only.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "029_auditoria_login"
down_revision: str | None = "028_tarifas_misiones"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS auditoria")
    op.execute("""
        CREATE TABLE IF NOT EXISTS auditoria.login_evento (
            id              BIGSERIAL PRIMARY KEY,
            -- usuario_id puede ser NULL: usuario no existe (intento contra mail inexistente).
            usuario_id      BIGINT REFERENCES core.usuario(id) ON DELETE SET NULL,
            -- siempre guardamos el identificador que mandó el cliente (email/username),
            -- aunque no exista — para detectar enumeration / spray attacks.
            usuario_intentado VARCHAR(254) NOT NULL,
            resultado       VARCHAR(32) NOT NULL,
                -- 'ok' | 'password_invalido' | 'usuario_inexistente'
                -- | 'rate_limit' | 'mfa_requerido' | 'mfa_invalido' | 'cambio_password_pendiente'
                -- | 'usuario_inactivo'
            mfa_usado       BOOLEAN NOT NULL DEFAULT false,
            ip              VARCHAR(64),
            user_agent      VARCHAR(512),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    # Índices: consulta típica es "últimos N fallos por usuario_intentado" o "por IP".
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_login_evento_usuario_created
            ON auditoria.login_evento (usuario_intentado, created_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_login_evento_ip_created
            ON auditoria.login_evento (ip, created_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_login_evento_resultado_created
            ON auditoria.login_evento (resultado, created_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditoria.login_evento")
