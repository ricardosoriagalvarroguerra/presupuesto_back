"""usuarios seed por rol — basados en las 14 historias de usuario

Revision ID: 004_usuarios_seed
Revises: 003_catalogo
Create Date: 2026-05-10
"""
from collections.abc import Sequence

from alembic import op

revision: str = "004_usuarios_seed"
down_revision: str | None = "003_catalogo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Placeholder de hash (no se puede loguear con esto). Las passwords reales no
# se versionan en el repo — se setean post-migración con `scripts/seed_users.py`
# leyendo de variables de entorno, o se restauran desde un dump fuera-de-banda.
# Esto evita que un fork del repo público herede passwords débiles conocidas.
DEMO_HASH = "$2b$10$DISABLED.PLACEHOLDER.HASH.NO.SE.PUEDE.LOGUEAR.CON.ESTO.aaaaa"


def upgrade() -> None:
    bind = op.get_bind()
    # Inserta 6 usuarios de prueba cubriendo los 5 roles + un usuario K2B externo simulado.
    bind.exec_driver_sql(
        f"""
        INSERT INTO core.usuario (email, nombre, apellido, password_hash, mfa_habilitado) VALUES
          ('maria.solicitante@fonplata.org',  'María',    'Mendoza',   '{DEMO_HASH}', false),
          ('juan.solicitante@fonplata.org',   'Juan',     'Ramírez',   '{DEMO_HASH}', false),
          ('carla.validador@fonplata.org',    'Carla',    'Vargas',    '{DEMO_HASH}', false),
          ('alfonso.consolidador@fonplata.org','Alfonso','Fernández',  '{DEMO_HASH}', false),
          ('alvaro.admin@fonplata.org',       'Álvaro',   'Miranda',   '{DEMO_HASH}', false),
          ('lia.auditor@fonplata.org',        'Lía',      'Rivera',    '{DEMO_HASH}', false);
        """
    )

    # Asigna roles
    bind.exec_driver_sql(
        """
        INSERT INTO core.usuario_rol (usuario_id, rol_id)
        SELECT u.id, r.id FROM core.usuario u, core.rol r
        WHERE (u.email = 'maria.solicitante@fonplata.org'   AND r.codigo = 'solicitante')
           OR (u.email = 'juan.solicitante@fonplata.org'    AND r.codigo = 'solicitante')
           OR (u.email = 'carla.validador@fonplata.org'     AND r.codigo = 'validador')
           OR (u.email = 'alfonso.consolidador@fonplata.org' AND r.codigo = 'consolidador')
           OR (u.email = 'alvaro.admin@fonplata.org'        AND r.codigo = 'administrador')
           OR (u.email = 'lia.auditor@fonplata.org'         AND r.codigo = 'auditor');
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM core.usuario_rol WHERE usuario_id IN (
          SELECT id FROM core.usuario WHERE email LIKE '%@fonplata.org'
        );
        DELETE FROM core.usuario WHERE email LIKE '%@fonplata.org';
        """
    )
