#!/usr/bin/env bash
# Entry point del backend. Aplica migraciones Alembic pendientes y arranca
# uvicorn en el puerto indicado por la variable $PORT (default 8000).
#
# Nota sobre el baseline Alembic: 001_baseline_mssql es un upgrade vacío — el
# DDL real se aplicó vía `migracion_sqlserver/ddl_sqlserver.sql`. En la primera
# puesta en marcha contra una BD ya poblada, correr UNA vez:
#       alembic stamp head
# para marcar el baseline como aplicado. A partir de ahí `alembic upgrade head`
# corre las migraciones nuevas que se vayan agregando (002+).
set -euo pipefail

echo "==> Aplicando migraciones Alembic..."
alembic upgrade head

echo "==> Iniciando uvicorn en 0.0.0.0:${PORT:-8000}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
