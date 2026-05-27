# Sistema de Gestión Presupuestaria — Backend

API REST async (FastAPI + SQLAlchemy 2 + SQL Server) que soporta el flujo de
planificación, aprobación por Vicepresidente, Presidencia y cierre de
solicitudes presupuestarias.

## Stack

- Python 3.11+ · FastAPI 0.115 · Uvicorn
- SQLAlchemy 2.0 (async, aioodbc / pyodbc) · Alembic
- Pydantic 2 · python-jose (JWT) · bcrypt · pyotp (MFA TOTP)
- pytest + pytest-asyncio (28 tests)
- SQL Server 2019+ con "ODBC Driver 18 for SQL Server" instalado en el host.
  Las jerarquías de cuenta/ítem (antes `ltree` en PG) viven ahora en columnas
  `NVARCHAR(255)` y se filtran con `LIKE` prefijo (ver `app/api/catalogo.py`).

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

El DDL para SQL Server fue migrado desde Postgres (ver `migracion_sqlserver/`).
Si tenés que rearmar una instancia nueva, ejecutá el DDL exportado:

```bash
# 1) Crear la BD y los schemas, y aplicar el DDL (tablas, FKs, CHECKs, índices).
sqlcmd -S localhost,1433 -U sa -P 'Fonplata2027!' -Q "CREATE DATABASE presupuesto2027"
sqlcmd -S localhost,1433 -U sa -P 'Fonplata2027!' -d presupuesto2027 \
       -i ../migracion_sqlserver/ddl_sqlserver.sql

# 2) (opcional) cargar datos desde el PG origen — ver migracion_sqlserver/migrate.py.

# 3) Marcar Alembic como ya aplicado (el baseline 001_baseline_mssql es un
#    upgrade vacío — el DDL real lo aplicó el paso anterior).
alembic stamp head
```

Una vez la BD existe y Alembic está stampeado, las migraciones nuevas (002+)
se aplican normalmente con `alembic upgrade head`.

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

## Deploy

`start.sh` corre `alembic upgrade head` antes de levantar uvicorn en el
puerto definido por `$PORT` (default 8000). Apto para correrlo bajo cualquier
orquestador que pueda ejecutar un shell script y exponer un puerto:
systemd, supervisor, docker, k8s, etc.

### Variables mínimas

| Variable | Detalle |
|---|---|
| `DATABASE_URL` | DSN SQL Server. Acepta `mssql://...`, `mssql+aioodbc://...`, `mssql+pyodbc://...` o una cadena ODBC nativa (`Driver={...};Server=...;...`). El código normaliza al driver async (aioodbc) y deriva el sync (pyodbc) para Alembic. |
| `APP_SECRET_KEY` | Generar con `python -c "import secrets; print(secrets.token_urlsafe(48))"`. **Rotar antes del go-live**. |
| `APP_ENV` | `production` (activa HSTS, cierra `/docs`). |
| `APP_CORS_ORIGINS` | Dominio público del frontend (lista separada por comas). |

Ver `.env.example` para el set completo.

## Estructura

```
backend/
├── app/
│   ├── api/             # Routers (auth, planificacion, solicitudes, catalogo, analisis, ejecucion)
│   ├── domain/          # Reglas de negocio (authz, calculo, enums)
│   ├── models/          # SQLAlchemy models
│   ├── schemas/         # Pydantic I/O
│   ├── config.py        # Settings (pydantic-settings) — normaliza el DSN de la BDR
│   ├── db.py            # Engine + Session factory async
│   ├── main.py          # FastAPI app factory + middlewares (rate-limit, security headers, CORS)
│   └── security.py      # JWT + get_current_user
├── alembic/             # Migraciones DDL versionadas (baseline + nuevas)
├── tests/               # pytest
├── requirements.txt     # Deps producción
├── pyproject.toml       # Deps + tooling dev
└── start.sh             # Entry point (alembic upgrade head + uvicorn)
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
