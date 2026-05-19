"""Seed de passwords reales para los usuarios institucionales.

Lee credenciales desde variables de entorno (formato:
`FONPLATA_PWD_<USERNAME_UPPER>`) y actualiza `core.usuario.password_hash`
con un bcrypt fresco. NO se ejecuta en CI ni en el flujo normal de
`alembic upgrade head` — es un script de bootstrap manual para entornos
limpios (un fork recién clonado).

Uso típico (en local, una sola vez tras crear la BD):
    export FONPLATA_PWD_MMEDNIK='Matias2026!'
    export FONPLATA_PWD_LBOTAFOGO='Luciana2026!'
    # ... resto de usuarios
    python -m scripts.seed_users

En Railway no hace falta correrlo: la BD viene de un pg_dump que ya trae
los hashes reales.

Por qué este enfoque y no commitear los hashes:
las passwords actuales son débiles por diseño (entrega manual al cliente).
Si los hashes entran al repo público, son rompibles. Acá los hashes nunca
salen de la máquina del admin.
"""
from __future__ import annotations

import os
import sys
from typing import Iterable

import bcrypt
import psycopg2


USERS: list[str] = [
    "mmednik", "lbotafogo", "mcalvino", "gcepparo", "rsoria", "amiranda",
    "vmoreira", "vgonzales", "edam", "egroterhorst",
    "aflores", "mgarcia", "awetzel", "jpinto", "ajustiniano",
]


def faltantes() -> list[str]:
    return [u for u in USERS if not os.environ.get(f"FONPLATA_PWD_{u.upper()}")]


def main() -> int:
    pendientes = faltantes()
    if pendientes:
        print("Faltan variables de entorno para:")
        for u in pendientes:
            print(f"  export FONPLATA_PWD_{u.upper()}='<password>'")
        print("\nAbortando — no se modifica nada.")
        return 1

    dsn = os.environ.get("DATABASE_URL_SYNC") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("Falta DATABASE_URL_SYNC (postgresql+psycopg2://...) o DATABASE_URL.")
        return 1
    # psycopg2 no entiende el prefijo SQLAlchemy "postgresql+psycopg2://".
    if dsn.startswith("postgresql+psycopg2://"):
        dsn = "postgresql://" + dsn[len("postgresql+psycopg2://"):]
    elif dsn.startswith("postgresql+asyncpg://"):
        dsn = "postgresql://" + dsn[len("postgresql+asyncpg://"):]

    con = psycopg2.connect(dsn)
    cur = con.cursor()
    total = 0
    for u in USERS:
        pwd = os.environ[f"FONPLATA_PWD_{u.upper()}"]
        h = bcrypt.hashpw(pwd.encode("utf-8"), bcrypt.gensalt(10)).decode("utf-8")
        cur.execute(
            """UPDATE core.usuario
               SET password_hash = %s, requiere_cambio_password = false
               WHERE username = %s
               RETURNING id, username""",
            (h, u),
        )
        row = cur.fetchone()
        if row:
            total += 1
            print(f"  OK  {u}  (id={row[0]})")
        else:
            print(f"  WARN  username '{u}' no existe en core.usuario — skip")
    con.commit()
    print(f"\nActualizadas {total}/{len(USERS)} passwords.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
