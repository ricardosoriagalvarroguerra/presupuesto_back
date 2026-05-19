"""username único + contraseñas individuales por usuario

Revision ID: 016_username_passwords
Revises: 015_usuarios_extra_planilla
Create Date: 2026-05-18

Cambios:
1. Agrega columna core.usuario.username (único, opcional) para permitir login
   con usuario corto en vez de (o además de) el email.
2. Asigna username + password_hash (bcrypt) para los 12 usuarios pedidos por
   negocio, con contraseñas individuales por persona.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "016_username_passwords"
down_revision: str | None = "015_usuarios_extra_planilla"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Asigna username a los usuarios reales y deja un hash PLACEHOLDER que no
# permite login. Los hashes reales:
#   - Se preservan en producción a través del pg_dump (no se re-aplica esta
#     migración en Railway porque la BD ya viene poblada).
#   - Para entornos nuevos (otro fork, otro servidor), un admin debe correr
#     `python scripts/seed_users.py` que toma las passwords desde variables
#     de entorno (FONPLATA_USER_<username>_PASSWORD) y setea hashes reales.
#
# Por qué placeholder en el repo público: las passwords iniciales siguen un
# patrón débil (`Nombre2026!`); si los hashes bcrypt entraran al repo, un
# atacante podría romperlas en minutos. Acá la migración solo crea la
# columna `username` y el mapeo email→username, sin secretos.
_DISABLED = "$2b$10$DISABLED.PLACEHOLDER.HASH.NO.SE.PUEDE.LOGUEAR.CON.ESTO.aaaaa"
USERS = [
    ("vmoreira",    "virginia.moreira@fonplata.org",  _DISABLED),
    ("amiranda",    "alvaro.miranda@fonplata.org",    _DISABLED),
    ("rsoria",      "adm.sistema@fonplata.org",       _DISABLED),
    ("lbotafogo",   "presidente@fonplata.org",        _DISABLED),
    ("mcalvino",    "jefe.gabinete@fonplata.org",     _DISABLED),
    ("mmednik",     "vpf@fonplata.org",               _DISABLED),
    ("gcepparo",    "jefe.contabilidad@fonplata.org", _DISABLED),
    ("mgarcia",     "mauricio.garcia@fonplata.org",   _DISABLED),
    ("aflores",     "jefe.unidad.vpe@fonplata.org",   _DISABLED),
    ("ajustiniano", "amanda.justiniano@fonplata.org", _DISABLED),
    ("awetzel",     "arturo.wetzel@fonplata.org",     _DISABLED),
    ("jpinto",      "javier.pinto@fonplata.org",      _DISABLED),
]


def upgrade() -> None:
    op.add_column("usuario", sa.Column("username", sa.String(64), unique=True, nullable=True), schema="core")
    op.create_index("ix_usuario_username", "usuario", ["username"], unique=True, schema="core")

    bind = op.get_bind()
    for username, email, pwhash in USERS:
        bind.exec_driver_sql(f"""
            UPDATE core.usuario
            SET username = '{username}', password_hash = '{pwhash}'
            WHERE email = '{email}'
        """)


def downgrade() -> None:
    op.drop_index("ix_usuario_username", table_name="usuario", schema="core")
    op.drop_column("usuario", "username", schema="core")
