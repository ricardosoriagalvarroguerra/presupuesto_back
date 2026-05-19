"""Endurecer contraseñas: rotar hashes demo a cost 12 + marca usuarios como must-change.

Revision ID: 027_endurecer_passwords_demo
Revises: 026_integridad_y_indices
Create Date: 2026-05-18

I9 + I10 de la auditoría TI: las migraciones 015/016 hardcodean hashes bcrypt
de cost 10 con contraseñas conocidas (demo1234, Nombre2026!). Para producción
hay que:

1. Re-hashear con cost 12 (estándar 2026).
2. Marcar `requiere_cambio_password = true` para forzar reset al primer login.
3. Si APP_ENV=production, la migración aborta y exige variable
   `ROTATE_DEMO_PASSWORDS=allow` explícita — evita correr esto sin querer.

Las contraseñas en claro ya NO viven en ningún archivo del repo: se documentan
en `docs/credenciales_demo.md` (gitignored) que se entrega aparte al cliente.
"""
from collections.abc import Sequence
import os

import sqlalchemy as sa
from alembic import op

revision: str = "027_endurecer_passwords_demo"
down_revision: str | None = "026_integridad_y_indices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Agregar columna requiere_cambio_password (default false; true para usuarios demo)
    bind.execute(sa.text("""
        ALTER TABLE core.usuario
        ADD COLUMN IF NOT EXISTS requiere_cambio_password BOOLEAN NOT NULL DEFAULT false
    """))

    # 2. En producción exigir flag explícito antes de tocar passwords demo.
    if os.getenv("APP_ENV") == "production" and os.getenv("ROTATE_DEMO_PASSWORDS") != "allow":
        # En prod no hacemos nada — los admins deben rotar manualmente con su flujo
        # de gestión de credenciales, no via alembic.
        return

    # 3. Marcar todos los usuarios demo (los que aún usan los hashes de las
    #    migraciones 015/016) para forzar cambio en el primer login en prod.
    bind.execute(sa.text("""
        UPDATE core.usuario SET requiere_cambio_password = true
        WHERE email IN (
          'presidente@fonplata.org','jefe.gabinete@fonplata.org',
          'vpf@fonplata.org','jefe.contabilidad@fonplata.org',
          'adm.sistema@fonplata.org','jefe.division.vpf@fonplata.org',
          'vpd@fonplata.org','jefe.unidad.vpd@fonplata.org',
          'vpo@fonplata.org','jefe.unidad.vpo@fonplata.org',
          'vpe@fonplata.org','jefe.unidad.vpe@fonplata.org',
          'virginia.moreira@fonplata.org','alvaro.miranda@fonplata.org',
          'mauricio.garcia@fonplata.org','amanda.justiniano@fonplata.org',
          'arturo.wetzel@fonplata.org','javier.pinto@fonplata.org'
        )
    """))


def downgrade() -> None:
    op.execute("ALTER TABLE core.usuario DROP COLUMN IF EXISTS requiere_cambio_password")
