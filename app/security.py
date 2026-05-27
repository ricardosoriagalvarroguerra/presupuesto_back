"""JWT firmado + dependency `get_current_user` que usan los endpoints protegidos.

Regla central: la identidad siempre se lee del header `Authorization: Bearer
<jwt>`. El JWT está firmado con `app_secret_key`; cualquier modificación
invalida la firma. Extraemos `sub` (= usuario_id) del payload — los schemas
Pydantic siguen aceptando un campo `usuario_id` por compat, pero el backend
lo ignora.

Dos dependencias públicas:
  - `get_current_user`             → exige scope='full'. Para todos los endpoints.
  - `get_current_user_any_scope`   → acepta también 'pwd_change'. Solo para
                                     /auth/cambiar-password y /auth/me.
"""
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db

bearer_scheme = HTTPBearer(auto_error=False)


def create_access_token(
    usuario_id: int,
    email: str,
    *,
    extra_claims: dict[str, Any] | None = None,
    expires_minutes: int | None = None,
) -> str:
    """Genera un JWT firmado con HS256 (claves simétricas).

    Los claims estándar (sub, iat, exp) los pone la función. Los específicos
    del proyecto (vp, roles, scope) los pasa el caller en `extra_claims`. La
    expiración default sale de `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` (8 horas);
    `expires_minutes` se usa para tokens de scope reducido (15 min en el
    caso de pwd_change).
    """
    settings = get_settings()
    exp_minutes = expires_minutes or settings.jwt_access_token_expire_minutes
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(usuario_id),
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=exp_minutes)).timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.app_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """Valida firma + expiración. Si algo falla → 401 con mensaje genérico.

    El mensaje "Token inválido o expirado" es deliberadamente vago — no
    distingue entre firma incorrecta, token expirado o token malformado.
    Mismo principio que el "Usuario o contraseña inválidos" del login.
    """
    settings = get_settings()
    try:
        return jwt.decode(token, settings.app_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token inválido o expirado",
            headers={"WWW-Authenticate": "Bearer realm=\"presupuesto-fonplata\""},
        ) from e


class CurrentUser:
    """Snapshot del usuario autenticado, hidratado en cada request.

    `scope` viene del JWT:
      - 'full'        → token de operación normal
      - 'pwd_change'  → token de un solo uso, válido únicamente para
                        /auth/cambiar-password. Cualquier otro endpoint
                        debe rechazarlo con `require_full_scope`.
    """
    __slots__ = ("id", "email", "username", "vp_codigo", "ver_todo", "roles", "planillas_extra", "scope")

    def __init__(self, **kwargs: Any) -> None:
        for k in self.__slots__:
            setattr(self, k, kwargs.get(k))


def require_full_scope(current: "CurrentUser") -> None:
    """Falla 403 si el token es de scope reducido (pwd_change)."""
    scope = getattr(current, "scope", "full") or "full"
    if scope != "full":
        raise HTTPException(
            status_code=403,
            detail="Token con scope limitado. Cambiá tu contraseña en /auth/cambiar-password.",
        )


async def _resolve_current_user(
    creds: HTTPAuthorizationCredentials | None,
    db: AsyncSession,
) -> CurrentUser:
    """Implementación compartida — sin guard de scope. Usado por las dos
    variantes públicas (get_current_user / get_current_user_any_scope).
    """
    if not creds or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticación requerida",
            headers={"WWW-Authenticate": "Bearer realm=\"presupuesto-fonplata\""},
        )
    claims = decode_token(creds.credentials)
    try:
        uid = int(claims["sub"])
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(401, "Token sin sub válido") from e

    # MSSQL no tiene `array_agg` ni `FILTER (WHERE ...)`. Agregamos roles con
    # STRING_AGG (SQL Server 2017+) y parseamos en Python; los nulos se filtran
    # con `WHERE r.codigo IS NOT NULL` antes del agregado (subquery).
    row = (await db.execute(
        text("""
            SELECT u.id, u.email, u.username, u.vp_codigo, u.ver_todo, u.estado,
                   (SELECT STRING_AGG(r.codigo, ',')
                    FROM core.usuario_rol ur
                    JOIN core.rol r ON r.id = ur.rol_id
                    WHERE ur.usuario_id = u.id) AS roles
            FROM core.usuario u
            WHERE u.id = :u
        """),
        {"u": uid},
    )).mappings().first()
    if not row or row["estado"] != "activo":
        raise HTTPException(401, "Usuario inválido o inactivo")
    roles_list: list[str] = []
    if row["roles"]:
        roles_list = [r for r in row["roles"].split(",") if r]

    extras = (await db.execute(
        text("SELECT planilla_codigo FROM core.usuario_planilla_extra WHERE usuario_id=:u"),
        {"u": uid},
    )).scalars().all()

    return CurrentUser(
        id=row["id"],
        email=row["email"],
        username=row["username"],
        vp_codigo=row["vp_codigo"],
        ver_todo=row["ver_todo"],
        roles=roles_list,
        planillas_extra=list(extras),
        scope=(claims.get("scope") or "full"),
    )


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """Dependencia DEFAULT — exige token de scope='full'. Cualquier endpoint
    que no use /auth/cambiar-password debe usar esta. Tokens scope='pwd_change'
    son rechazados con 403, así no se pueden usar para operar.
    """
    user = await _resolve_current_user(creds, db)
    require_full_scope(user)
    return user


async def get_current_user_any_scope(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """Variante que ACEPTA tokens de scope reducido. Solo /auth/me y
    /auth/cambiar-password la usan — el resto debe usar `get_current_user`.
    """
    return await _resolve_current_user(creds, db)
