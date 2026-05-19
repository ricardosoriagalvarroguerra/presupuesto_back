"""usuarios por VP + permisos de visibilidad

Revision ID: 006_usuarios_por_vp
Revises: 005_ejecucion
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006_usuarios_por_vp"
down_revision: str | None = "005_ejecucion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# password hash precomputado para "demo1234" — solo para entorno de prueba
DEMO_HASH = "$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewKyBkGdH9ZDVlNW"


def upgrade() -> None:
    # 1. Agregar columnas de permiso al usuario
    op.add_column("usuario", sa.Column("vp_codigo", sa.String(8)), schema="core")
    op.add_column("usuario", sa.Column("ver_todo", sa.Boolean, server_default=sa.text("false"), nullable=False), schema="core")
    op.add_column("usuario", sa.Column("cargo", sa.String(128)), schema="core")

    bind = op.get_bind()

    # 2. Reset usuarios existentes
    bind.exec_driver_sql("DELETE FROM core.usuario_rol")
    bind.exec_driver_sql("DELETE FROM core.usuario")

    # 3. Roles adicionales (extiende los 5 base)
    bind.exec_driver_sql("""
        INSERT INTO core.rol (codigo, nombre, descripcion) VALUES
          ('presidente',         'Presidente',           'Visibilidad total del presupuesto institucional.'),
          ('vicepresidente',     'Vicepresidente',       'Aprueba y consolida el presupuesto de su VP.'),
          ('jefe_unidad',        'Jefe de Unidad',       'Carga y revisa la solicitud de su unidad.'),
          ('jefe_gabinete',      'Jefe de Gabinete',     'Apoyo a Presidencia.'),
          ('jefe_contabilidad',  'Jefe de Contabilidad', 'CYP — consolida y carga al ERP.'),
          ('jefe_division',      'Jefe de División',     'Soporte planificación VPF.'),
          ('adm_sistema',        'Administrador Sistema','Configuración general y catálogos.')
        ON CONFLICT (codigo) DO NOTHING
    """)

    # 4. Insertar usuarios con su VP y permisos
    # (email, nombre, apellido, vp_codigo, ver_todo, cargo, rol)
    USERS = [
        # Presidencia (ven todo)
        ("presidente@fonplata.org",         "Luciana",  "Botafogo",       "PRE", True,  "Presidenta",                "presidente"),
        ("jefe.gabinete@fonplata.org",      "Maria",    "Calvino",        "PRE", True,  "Jefe de Gabinete",          "jefe_gabinete"),
        # VPF (ven todo — área de consolidación)
        ("vpf@fonplata.org",                "Matias",   "Mednik",         "VPF", True,  "VP Finanzas",               "vicepresidente"),
        ("jefe.contabilidad@fonplata.org",  "German",   "Cepparo",        "VPF", True,  "Jefe Contabilidad",         "jefe_contabilidad"),
        ("adm.sistema@fonplata.org",        "Ricardo",  "Soria Galvarro", "VPF", True,  "Administrador",             "adm_sistema"),
        ("jefe.division.vpf@fonplata.org",  "Rafael",   "Robles",         "VPF", True,  "Jefe División VPF",         "jefe_division"),
        # VPD (solo VPD)
        ("vpd@fonplata.org",                "Viviana",  "Gonzáles",       "VPD", False, "VP Desarrollo Estratégico", "vicepresidente"),
        ("jefe.unidad.vpd@fonplata.org",    "Leonardo", "Chagas",         "VPD", False, "Jefe Unidad VPD",           "jefe_unidad"),
        # VPO (solo VPO)
        ("vpo@fonplata.org",                "Eliana",   "Dam",            "VPO", False, "VP Operaciones y Países",   "vicepresidente"),
        ("jefe.unidad.vpo@fonplata.org",    "Carlos",   "Molina",         "VPO", False, "Jefe Unidad VPO",           "jefe_unidad"),
        # VPE (solo VPE)
        ("vpe@fonplata.org",                "Elke",     "Groterhorst",    "VPE", False, "VP Ejecutiva",              "vicepresidente"),
        ("jefe.unidad.vpe@fonplata.org",    "Angel",    "Flores",         "VPE", False, "Jefe Unidad VPE",           "jefe_unidad"),
    ]
    for email, nombre, apellido, vp, ver_todo, cargo, rol in USERS:
        bind.exec_driver_sql(f"""
            INSERT INTO core.usuario (email, nombre, apellido, password_hash, vp_codigo, ver_todo, cargo, mfa_habilitado)
            VALUES ('{email}', '{nombre}', '{apellido}', '{DEMO_HASH}',
                    '{vp}', {str(ver_todo).lower()}, '{cargo}', false)
        """)
        bind.exec_driver_sql(f"""
            INSERT INTO core.usuario_rol (usuario_id, rol_id)
            SELECT u.id, r.id FROM core.usuario u, core.rol r
            WHERE u.email='{email}' AND r.codigo='{rol}'
        """)


def downgrade() -> None:
    op.execute("DELETE FROM core.usuario_rol")
    op.execute("DELETE FROM core.usuario")
    op.drop_column("usuario", "cargo", schema="core")
    op.drop_column("usuario", "ver_todo", schema="core")
    op.drop_column("usuario", "vp_codigo", schema="core")
    op.execute("""
        DELETE FROM core.rol WHERE codigo IN
        ('presidente','vicepresidente','jefe_unidad','jefe_gabinete','jefe_contabilidad','jefe_division','adm_sistema')
    """)
