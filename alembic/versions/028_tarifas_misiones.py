"""Carga tarifas de Misiones (pasajes por ruta, hospedaje por ciudad) a la BD.

Revision ID: 028_tarifas_misiones
Revises: 027_endurecer_passwords_demo
Create Date: 2026-05-18

Cierra el último hueco de B1/I6: el backend ahora puede recalcular pasaje y
hospedaje de Misiones de forma autoritativa sin depender del hint del cliente.

Origen de datos: misiones_parametrizado/parametros_Misiones.xlsx (Mediana USD
constantes 2025). Reflejado también en frontend/src/data/parametrosMisiones.ts
para mostrar valores en la UI; el backend es la autoridad.
"""
from collections.abc import Sequence
import json
import os

import sqlalchemy as sa
from alembic import op

revision: str = "028_tarifas_misiones"
down_revision: str | None = "027_endurecer_passwords_demo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # Tablas
    bind.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS catalogo.tarifa_pasaje_ruta (
            ruta              VARCHAR(160) PRIMARY KEY,
            origen            VARCHAR(80),
            destino           VARCHAR(80),
            pais_origen       VARCHAR(40),
            pais_destino      VARCHAR(40),
            monto_anual_usd   NUMERIC(12, 2)
        )
    """))
    bind.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS catalogo.tarifa_pasaje_mensual (
            ruta              VARCHAR(160),
            mes               SMALLINT CHECK (mes BETWEEN 1 AND 12),
            monto_usd         NUMERIC(12, 2) NOT NULL,
            PRIMARY KEY (ruta, mes),
            FOREIGN KEY (ruta) REFERENCES catalogo.tarifa_pasaje_ruta(ruta) ON DELETE CASCADE
        )
    """))
    bind.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS catalogo.tarifa_hospedaje_ciudad (
            ciudad            VARCHAR(80) PRIMARY KEY,
            pais              VARCHAR(40),
            noche_anual_usd   NUMERIC(12, 2)
        )
    """))
    bind.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS catalogo.tarifa_hospedaje_mensual (
            ciudad            VARCHAR(80),
            mes               SMALLINT CHECK (mes BETWEEN 1 AND 12),
            noche_usd         NUMERIC(12, 2) NOT NULL,
            PRIMARY KEY (ciudad, mes),
            FOREIGN KEY (ciudad) REFERENCES catalogo.tarifa_hospedaje_ciudad(ciudad) ON DELETE CASCADE
        )
    """))
    bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_tarifa_pasaje_destino ON catalogo.tarifa_pasaje_ruta(pais_destino)"))
    bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_tarifa_hospedaje_pais ON catalogo.tarifa_hospedaje_ciudad(pais)"))

    # Cargar datos del JSON intermedio
    data_path = os.path.join(os.path.dirname(__file__), "_tarifas_data.json")
    if not os.path.exists(data_path):
        # En CI/prod sin el JSON, las tablas quedan vacías — no es bloqueante.
        return
    with open(data_path) as f:
        data = json.load(f)

    # Limpiar (idempotencia)
    bind.execute(sa.text("DELETE FROM catalogo.tarifa_hospedaje_mensual"))
    bind.execute(sa.text("DELETE FROM catalogo.tarifa_hospedaje_ciudad"))
    bind.execute(sa.text("DELETE FROM catalogo.tarifa_pasaje_mensual"))
    bind.execute(sa.text("DELETE FROM catalogo.tarifa_pasaje_ruta"))

    # Insertar pasajes
    for ruta, info in data.get("pasajes", {}).items():
        bind.execute(sa.text("""
            INSERT INTO catalogo.tarifa_pasaje_ruta
              (ruta, origen, destino, pais_origen, pais_destino, monto_anual_usd)
            VALUES (:r, :o, :d, :po, :pd, :a)
        """), {"r": ruta, "o": info["origen"], "d": info["destino"],
               "po": info["pais_origen"], "pd": info["pais_destino"],
               "a": info["anual"]})
        for i, m in enumerate(info.get("mensual") or []):
            if m is not None:
                bind.execute(sa.text("""
                    INSERT INTO catalogo.tarifa_pasaje_mensual (ruta, mes, monto_usd)
                    VALUES (:r, :mes, :m)
                """), {"r": ruta, "mes": i + 1, "m": m})

    # Insertar hospedaje
    for ciudad, info in data.get("hospedaje", {}).items():
        bind.execute(sa.text("""
            INSERT INTO catalogo.tarifa_hospedaje_ciudad (ciudad, pais, noche_anual_usd)
            VALUES (:c, :p, :a)
        """), {"c": ciudad, "p": info["pais"], "a": info["anual"]})
        for i, m in enumerate(info.get("mensual") or []):
            if m is not None:
                bind.execute(sa.text("""
                    INSERT INTO catalogo.tarifa_hospedaje_mensual (ciudad, mes, noche_usd)
                    VALUES (:c, :mes, :m)
                """), {"c": ciudad, "mes": i + 1, "m": m})


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS catalogo.tarifa_hospedaje_mensual")
    op.execute("DROP TABLE IF EXISTS catalogo.tarifa_hospedaje_ciudad")
    op.execute("DROP TABLE IF EXISTS catalogo.tarifa_pasaje_mensual")
    op.execute("DROP TABLE IF EXISTS catalogo.tarifa_pasaje_ruta")
