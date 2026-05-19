# Sistema de Gestión Presupuestaria — Backend

API REST async (FastAPI + SQLAlchemy 2 + PostgreSQL) que soporta el flujo de
planificación, aprobación por Vicepresidente, Presidencia y cierre de
solicitudes presupuestarias.

## Stack

- Python 3.11+ · FastAPI 0.115 · Uvicorn
- SQLAlchemy 2.0 (async, asyncpg) · Alembic
- Pydantic 2 · python-jose (JWT) · bcrypt · pyotp (MFA TOTP)
- pytest + pytest-asyncio (28 tests)
- PostgreSQL 14+ con extensión `ltree`

## Setup local

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# editar .env con la URL real de Postgres y APP_SECRET_KEY
```

### Base de datos

```sql
CREATE DATABASE presupuesto2027;
\c presupuesto2027
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS catalogo;
CREATE SCHEMA IF NOT EXISTS planificacion;
CREATE SCHEMA IF NOT EXISTS workflow;
CREATE SCHEMA IF NOT EXISTS ejecucion;
CREATE SCHEMA IF NOT EXISTS analisis;
CREATE SCHEMA IF NOT EXISTS auditoria;
CREATE SCHEMA IF NOT EXISTS integracion_k2b;
CREATE EXTENSION IF NOT EXISTS ltree;
```

```bash
alembic upgrade head    # aplica las 32 migraciones

# Bootstrap de passwords reales (solo entornos NUEVOS — en Railway no hace
# falta, los hashes vienen del pg_dump). Ver scripts/seed_users.py.
export FONPLATA_PWD_MMEDNIK='...'
# ... resto de usuarios
python -m scripts.seed_users
```

## Run

```bash
uvicorn app.main:app --reload --port 8000
# OpenAPI: http://localhost:8000/docs
```

## Tests

```bash
pytest          # 28 tests: auth, autorización cross-VP, workflow, concurrencia
pytest --cov    # con cobertura
ruff check .    # linter
mypy app/       # type checker
```

## Deploy a Railway

Railway detecta automáticamente `requirements.txt` y `start.sh`
(`railway.json` + `Procfile`). `start.sh` corre `alembic upgrade head` antes
de levantar uvicorn en el `$PORT` que Railway asigna.

### Variables mínimas a configurar en Railway

| Variable | Detalle |
|---|---|
| `DATABASE_URL` | Inyectada automática al conectar el plugin Postgres. Acepta `postgresql://...`; el código lo normaliza a `postgresql+asyncpg://`. |
| `APP_SECRET_KEY` | Generar con `python -c "import secrets; print(secrets.token_urlsafe(48))"`. **Rotar antes del go-live**. |
| `APP_ENV` | `production` (activa HSTS). |
| `APP_CORS_ORIGINS` | Dominio público del frontend (ej. `https://presupuesto-front.up.railway.app`). |

Ver `.env.example` para el set completo.

## Estructura

```
backend/
├── app/
│   ├── api/             # Routers (auth, planificacion, solicitudes, catalogo, analisis, ejecucion)
│   ├── domain/          # Reglas de negocio (authz, calculo, enums)
│   ├── models/          # SQLAlchemy models
│   ├── schemas/         # Pydantic I/O
│   ├── config.py        # Settings (pydantic-settings) — normaliza DSN para Railway
│   ├── db.py            # Engine + Session factory async
│   ├── main.py          # FastAPI app factory + middlewares (rate-limit, security headers, CORS)
│   └── security.py      # JWT + get_current_user
├── alembic/             # Migraciones DDL versionadas (32)
├── scripts/             # Tooling fuera-de-banda (seed_users.py)
├── tests/               # pytest
├── requirements.txt     # Deps producción
├── pyproject.toml       # Deps + tooling dev
├── start.sh             # Entry point Railway
├── railway.json         # Config Railway (NIXPACKS + healthcheck /health)
└── Procfile             # Fallback Heroku-style
```

## Seguridad

- JWT HS256 (8 h), bcrypt 10 rounds, MFA TOTP opcional por usuario.
- Scope por token (`full` / `pwd_change`) — endpoints sensibles rechazan `pwd_change`.
- RBAC por VP + `planillas_extra` para acceso cross-VP por planilla.
- Rate-limit in-memory en `/auth/login` (5/15 min en prod, 50/15 min en dev).
- Headers OWASP (CSP, X-Frame-Options, HSTS condicional, Referrer-Policy, Permissions-Policy).
- Auditoría inmutable: `auditoria.login_evento` + `planificacion.evento_solicitud`.
- Locks pesimistas (`SELECT FOR UPDATE`) en transiciones de workflow → evitan TOCTOU multi-usuario.

## Workflow de aprobación

```
Etapa 0  Elaboración              cargadores de la VP
Etapa 1  Revisión Vicepresidente  VP titular (solo VPF/VPD/VPO/VPE; PRE y GOB saltan)
Etapa 2  Revisión Presidencia     Presidenta / Jefe Gabinete
Etapa 3  Aprobado
Etapa 4  Cerrado
```

Cada etapa admite **aprobar / observar / devolver**. Devolución vuelve a etapa 0
y la solicitud debe re-pasar por todas las revisiones siguientes.
