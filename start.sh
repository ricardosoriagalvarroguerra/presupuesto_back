#!/usr/bin/env bash
# Entry point para Railway (y cualquier PaaS estilo Heroku).
# Aplica migraciones pendientes y arranca el ASGI server.
#
# Railway inyecta $PORT — uvicorn debe bindar a ese puerto y a 0.0.0.0.
set -euo pipefail

echo "==> Aplicando migraciones Alembic..."
alembic upgrade head

echo "==> Iniciando uvicorn en 0.0.0.0:${PORT:-8000}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
