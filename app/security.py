"""JWT + dependency `get_current_user` para FastAPI.

Source of truth de autenticación: el header `Authorization: Bearer <jwt>` es
validado en cada request protegida. Hasta hoy el backend confiaba en el
`usuario_id` del payload (suplantación trivial); con esto, ese campo se
ignora — la identidad viene del token firmado.
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
    """Firma un JWT con sub=usuario_id, email y claims adicionales (roles, vp)."""
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
    settings = get_settings()
    try:
        return jwt.decode(token, settings.app_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token inválido o expirado",
            headers={"WWW-Authenticate": "Bearer"},
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
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = decode_token(creds.credentials)
    try:
        uid = int(claims["sub"])
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(401, "Token sin sub válido") from e

    row = (await db.execute(
        text("""
            SELECT u.id, u.email, u.username, u.vp_codigo, u.ver_todo, u.estado,
                   COALESCE(array_agg(r.codigo) FILTER (WHERE r.codigo IS NOT NULL), '{}') AS roles
            FROM core.usuario u
            LEFT JOIN core.usuario_rol ur ON ur.usuario_id = u.id
            LEFT JOIN core.rol r ON r.id = ur.rol_id
            WHERE u.id = :u
            GROUP BY u.id
        """),
        {"u": uid},
    )).mappings().first()
    if not row or row["estado"] != "activo":
        raise HTTPException(401, "Usuario inválido o inactivo")

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
        roles=list(row["roles"] or []),
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
