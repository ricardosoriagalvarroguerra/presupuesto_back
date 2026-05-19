"""usuarios adicionales + scope cross-VP por planilla

Revision ID: 015_usuarios_extra_planilla
Revises: 014_objetivos_estrategicos
Create Date: 2026-05-18

Cambios:
1. Nueva tabla core.usuario_planilla_extra: permite que un usuario con scope
   a una VP específica también pueda ver/editar una planilla concreta en TODAS
   las demás VPs (caso Angel Flores, que es VPE pero además ve Salarios y
   Beneficios de las otras vicepresidencias).
2. Inserta 6 usuarios nuevos pedidos por el negocio.
3. Asigna a Angel Flores el permiso extra sobre PL-SALARIOS-BENEF.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "015_usuarios_extra_planilla"
down_revision: str | None = "014_objetivos_estrategicos"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


DEMO_HASH = "$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewKyBkGdH9ZDVlNW"


def upgrade() -> None:
    # 1. Tabla de permisos extra por planilla (cross-VP)
    op.create_table(
        "usuario_planilla_extra",
        sa.Column("usuario_id", sa.Integer(),
                  sa.ForeignKey("core.usuario.id", ondelete="CASCADE"),
                  primary_key=True, nullable=False),
        sa.Column("planilla_codigo", sa.String(64), primary_key=True, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        schema="core",
    )

    bind = op.get_bind()

    # 2. Usuarios nuevos pedidos por negocio (los demás ya existían en la 006)
    # (email, nombre, apellido, vp_codigo, ver_todo, cargo, rol)
    USERS = [
        ("virginia.moreira@fonplata.org", "Virginia", "Moreira",    "VPD", False, "Jefe Unidad VPD",   "jefe_unidad"),
        ("alvaro.miranda@fonplata.org",   "Alvaro",   "Miranda",    "VPF", True,  "Administrador",     "adm_sistema"),
        ("mauricio.garcia@fonplata.org",  "Mauricio", "Garcia",     "VPE", False, "Jefe Unidad VPE",   "jefe_unidad"),
        ("amanda.justiniano@fonplata.org","Amanda",   "Justiniano", "PRE", False, "Jefe Unidad PRE",   "jefe_unidad"),
        ("arturo.wetzel@fonplata.org",    "Arturo",   "Wetzel",     "VPE", False, "Jefe Unidad VPE",   "jefe_unidad"),
        ("javier.pinto@fonplata.org",     "Javier",   "Pinto",      "VPO", False, "Jefe Unidad VPO",   "jefe_unidad"),
    ]
    for email, nombre, apellido, vp, ver_todo, cargo, rol in USERS:
        bind.exec_driver_sql(f"""
            INSERT INTO core.usuario (email, nombre, apellido, password_hash, vp_codigo, ver_todo, cargo, mfa_habilitado)
            VALUES ('{email}', '{nombre}', '{apellido}', '{DEMO_HASH}',
                    '{vp}', {str(ver_todo).lower()}, '{cargo}', false)
            ON CONFLICT (email) DO NOTHING
        """)
        bind.exec_driver_sql(f"""
            INSERT INTO core.usuario_rol (usuario_id, rol_id)
            SELECT u.id, r.id FROM core.usuario u, core.rol r
            WHERE u.email='{email}' AND r.codigo='{rol}'
            ON CONFLICT DO NOTHING
        """)

    # 3. Angel Flores: VPE + acceso cross-VP a la planilla Salarios y Beneficios
    bind.exec_driver_sql("""
        INSERT INTO core.usuario_planilla_extra (usuario_id, planilla_codigo)
        SELECT u.id, 'PL-SALARIOS-BENEF'
        FROM core.usuario u
        WHERE u.email = 'jefe.unidad.vpe@fonplata.org'
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM core.usuario_rol WHERE usuario_id IN (
          SELECT id FROM core.usuario WHERE email IN (
            'virginia.moreira@fonplata.org',
            'alvaro.miranda@fonplata.org',
            'mauricio.garcia@fonplata.org',
            'amanda.justiniano@fonplata.org',
            'arturo.wetzel@fonplata.org',
            'javier.pinto@fonplata.org'
          )
        )
    """)
    op.execute("""
        DELETE FROM core.usuario WHERE email IN (
          'virginia.moreira@fonplata.org',
          'alvaro.miranda@fonplata.org',
          'mauricio.garcia@fonplata.org',
          'amanda.justiniano@fonplata.org',
          'arturo.wetzel@fonplata.org',
          'javier.pinto@fonplata.org'
        )
    """)
    op.drop_table("usuario_planilla_extra", schema="core")
