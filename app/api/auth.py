"""Login, cambio de password, MFA TOTP, emisión de JWT.

La identidad siempre se lee del token (JWT firmado). Los schemas Pydantic
todavía aceptan un campo `usuario_id` por compatibilidad con clientes viejos,
pero el backend lo ignora — la fuente es siempre `Depends(get_current_user)`.

Flujos:
  POST /auth/login
    ├─ user inexistente      → 401 (mismo texto que pw inválida — anti-enum)
    ├─ pw inválida           → 401
    ├─ requiere MFA          → 401 con detail.code='mfa_required'
    ├─ requiere cambio pw    → 200 + token scope='pwd_change' (limitado)
    └─ ok                    → 200 + token scope='full'

  POST /auth/cambiar-password
    Acepta tokens 'full' (cambio voluntario) y 'pwd_change' (cambio obligado).
    Exige password actual en ambos casos — protección contra hijack del token.

  POST /auth/mfa/{setup,enable,disable}
    Setup pide pw actual y devuelve secret + otpauth_uri.
    Enable confirma con TOTP válido → activa MFA.
    Disable pide pw + TOTP (anti-takeover por celular robado).

Cada intento de login (exitoso o no) deja registro en `auditoria.login_evento`.
Si la tabla no existe (setup incompleto), se loguea WARNING y el login sigue
funcionando — un audit log caído no debería tirar el sistema.
"""
import binascii
import logging
import re
from typing import Any

import bcrypt
import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.models.core import Usuario
from app.security import (
    CurrentUser,
    create_access_token,
    get_current_user,
    get_current_user_any_scope,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

MFA_ISSUER = "FONPLATA Presupuesto"


# ============================================================
# Schemas
# ============================================================

class UsuarioOut(BaseModel):
    id: int
    email: str
    username: str | None = None
    nombre: str
    apellido: str
    roles: list[str]
    vp_codigo: str | None = None
    ver_todo: bool = False
    cargo: str | None = None
    planillas_extra: list[str] = []
    mfa_habilitado: bool = False
    requiere_cambio_password: bool = False


class LoginIn(BaseModel):
    model_config = {"extra": "forbid"}
    usuario: str
    password: str
    mfa_code: str | None = None  # opcional; obligatorio si user tiene MFA habilitado


class LoginOut(BaseModel):
    """Respuesta del login.

    `scope` indica si el token está limitado:
      - 'full'        → puede operar normalmente
      - 'pwd_change'  → solo /auth/cambiar-password está permitido
    """
    access_token: str
    token_type: str = "bearer"
    scope: str = "full"
    requiere_cambio_password: bool = False
    user: UsuarioOut


class CambiarPasswordIn(BaseModel):
    model_config = {"extra": "forbid"}
    password_actual: str
    password_nueva: str = Field(min_length=10, max_length=128)


class MfaSetupIn(BaseModel):
    model_config = {"extra": "forbid"}
    password_actual: str


class MfaSetupOut(BaseModel):
    secret: str
    otpauth_uri: str  # para generar QR en el cliente


class MfaEnableIn(BaseModel):
    model_config = {"extra": "forbid"}
    codigo: str


class MfaDisableIn(BaseModel):
    model_config = {"extra": "forbid"}
    password_actual: str
    codigo: str


# ============================================================
# Helpers
# ============================================================

async def _planillas_extra_for(db: AsyncSession, usuario_id: int) -> list[str]:
    rows = (await db.execute(
        text("SELECT planilla_codigo FROM core.usuario_planilla_extra WHERE usuario_id=:u"),
        {"u": usuario_id},
    )).scalars().all()
    return list(rows)


async def _to_out(db: AsyncSession, u: Usuario) -> UsuarioOut:
    return UsuarioOut(
        id=u.id,
        email=u.email,
        username=u.username,
        nombre=u.nombre,
        apellido=u.apellido,
        roles=[r.codigo for r in u.roles],
        vp_codigo=u.vp_codigo,
        ver_todo=u.ver_todo,
        cargo=u.cargo,
        planillas_extra=await _planillas_extra_for(db, u.id),
        mfa_habilitado=bool(getattr(u, "mfa_habilitado", False)),
        requiere_cambio_password=bool(getattr(u, "requiere_cambio_password", False)),
    )


async def _registrar_login(
    db: AsyncSession,
    *,
    usuario_id: int | None,
    usuario_intentado: str,
    resultado: str,
    mfa_usado: bool,
    request: Request | None,
) -> None:
    """Append-only en auditoria.login_evento. Tolerante si la migración 029
    todavía no corrió — no debe romper el flujo de auth.
    """
    ip = None
    ua = None
    if request is not None:
        ip = request.client.host if request.client else None
        ua = (request.headers.get("user-agent") or "")[:512] or None
    try:
        await db.execute(
            text("""
                INSERT INTO auditoria.login_evento
                  (usuario_id, usuario_intentado, resultado, mfa_usado, ip, user_agent)
                VALUES (:uid, :ui, :r, :m, :ip, :ua)
            """),
            {"uid": usuario_id, "ui": usuario_intentado[:254], "r": resultado,
             "m": mfa_usado, "ip": ip, "ua": ua},
        )
        await db.commit()
    except ProgrammingError as e:
        # Tabla no existe (migración 029 pendiente) — no rompemos el login por eso.
        await db.rollback()
        logger.warning("auditoria.login_evento no disponible (correr alembic upgrade head): %s", e)
    except SQLAlchemyError as e:
        # Cualquier otro error de DB (FK rota, deadlock, conexión perdida).
        # No romper login por una falla del audit log.
        await db.rollback()
        logger.warning("no se pudo registrar login_evento: %s", e)


def _password_cumple_reglas(pwd: str) -> tuple[bool, str | None]:
    """Reglas mínimas: 10+ chars, mayúscula, minúscula, dígito, símbolo.

    Devuelve (ok, mensaje_de_error). Si todo está bien, mensaje=None.

    No chequeamos contra diccionario (passwords comunes tipo "Password123!")
    — eso queda como mejora futura. La rotación obligatoria se maneja aparte
    con el flag `requiere_cambio_password`.
    """
    if len(pwd) < 10:
        return False, "La contraseña debe tener al menos 10 caracteres."
    if not re.search(r"[A-Z]", pwd):
        return False, "La contraseña debe tener al menos una mayúscula."
    if not re.search(r"[a-z]", pwd):
        return False, "La contraseña debe tener al menos una minúscula."
    if not re.search(r"\d", pwd):
        return False, "La contraseña debe tener al menos un dígito."
    if not re.search(r"[^A-Za-z0-9]", pwd):
        return False, "La contraseña debe tener al menos un símbolo."
    return True, None


def _hash_password(pwd: str) -> str:
    # bcrypt con 10 rounds: balance entre seguridad y latencia. Cada round
    # duplica el costo (12 sería ~4x más lento por login). Si se sube el cost
    # factor, cambiar acá y regenerar hashes en el próximo cambio obligatorio.
    return bcrypt.hashpw(pwd.encode("utf-8"), bcrypt.gensalt(10)).decode("utf-8")


def _verify_password(pwd: str, hashed: str) -> bool:
    # Hashes corruptos, vacíos o no-bcrypt → False. No levantamos excepción
    # porque el caller termina cayendo por la rama "credencial inválida" igual.
    try:
        return bcrypt.checkpw(pwd.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _verify_totp(secret: str | None, codigo: str | None) -> bool:
    """Valida un código TOTP de 6 dígitos. valid_window=1 → tolera ±30s.

    Si secret o código son falsy → False (defensa en profundidad; los callers
    ya chequean antes).
    """
    if not secret or not codigo:
        return False
    try:
        return pyotp.TOTP(secret).verify(codigo.strip(), valid_window=1)
    except (ValueError, TypeError, binascii.Error):
        # pyotp tira ValueError/binascii si el secret base32 está corrupto.
        # Devolvemos False
        # para que el flujo termine en "código inválido" en vez de 500.
        return False


def _token_full(user: Usuario, roles: list[str]) -> str:
    """JWT scope='full' — el token que se usa para el día a día.

    Claims útiles para el frontend (evita un /me extra al login):
      vp, ver_todo, roles, scope
    Expira en 8 horas por default (configurable vía JWT_ACCESS_TOKEN_EXPIRE_MINUTES).
    """
    return create_access_token(
        usuario_id=user.id,
        email=user.email,
        extra_claims={
            "vp": user.vp_codigo,
            "ver_todo": user.ver_todo,
            "roles": roles,
            "scope": "full",
        },
    )


def _token_pwd_change(user: Usuario) -> str:
    """JWT scope='pwd_change' — limitado, expira en 15 min.

    Se emite cuando el user tiene `requiere_cambio_password=true`. Con este
    token NO se puede operar nada del sistema, solo POST a /auth/cambiar-password.
    Cuando el cambio se completa, ese endpoint emite un token full nuevo.
    El frontend detecta el scope y abre el wizard de cambio obligatorio.
    """
    return create_access_token(
        usuario_id=user.id,
        email=user.email,
        extra_claims={"scope": "pwd_change"},
        expires_minutes=15,
    )


# ============================================================
# Endpoints
# ============================================================

@router.post("/login", response_model=LoginOut)
async def login(
    payload: LoginIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    ident = (payload.usuario or "").strip().lower()
    if not ident or not payload.password:
        raise HTTPException(status_code=400, detail="Usuario y contraseña son requeridos")

    stmt = (
        select(Usuario)
        .options(selectinload(Usuario.roles))
        .where(or_(Usuario.email == ident, Usuario.username == ident))
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        await _registrar_login(db, usuario_id=None, usuario_intentado=ident,
                               resultado="usuario_inexistente", mfa_usado=False, request=request)
        raise HTTPException(status_code=401, detail="Usuario o contraseña inválidos")

    if not _verify_password(payload.password, user.password_hash):
        await _registrar_login(db, usuario_id=user.id, usuario_intentado=ident,
                               resultado="password_invalido", mfa_usado=False, request=request)
        logger.info("login fallo email=%s", user.email)
        raise HTTPException(status_code=401, detail="Usuario o contraseña inválidos")

    if getattr(user, "estado", "activo") != "activo":
        await _registrar_login(db, usuario_id=user.id, usuario_intentado=ident,
                               resultado="usuario_inactivo", mfa_usado=False, request=request)
        raise HTTPException(status_code=401, detail="Usuario inactivo")

    # MFA: si el usuario lo tiene habilitado, exigir código TOTP en este mismo POST.
    mfa_usado = False
    if getattr(user, "mfa_habilitado", False):
        if not payload.mfa_code:
            await _registrar_login(db, usuario_id=user.id, usuario_intentado=ident,
                                   resultado="mfa_requerido", mfa_usado=False, request=request)
            raise HTTPException(
                status_code=401,
                detail={"code": "mfa_required",
                        "mensaje": "Cuenta protegida con MFA. Reenviá el login con `mfa_code`."},
            )
        if not _verify_totp(user.mfa_secret, payload.mfa_code):
            await _registrar_login(db, usuario_id=user.id, usuario_intentado=ident,
                                   resultado="mfa_invalido", mfa_usado=False, request=request)
            raise HTTPException(status_code=401, detail="Código MFA inválido o vencido.")
        mfa_usado = True

    user_out = await _to_out(db, user)

    # Cambio de password obligatorio: token scope=pwd_change, frontend debe
    # llamar a /auth/cambiar-password antes de operar.
    if getattr(user, "requiere_cambio_password", False):
        token = _token_pwd_change(user)
        await _registrar_login(db, usuario_id=user.id, usuario_intentado=ident,
                               resultado="cambio_password_pendiente", mfa_usado=mfa_usado, request=request)
        logger.info("login ok user_id=%s email=%s (requiere cambio pw)", user.id, user.email)
        return LoginOut(
            access_token=token, scope="pwd_change",
            requiere_cambio_password=True, user=user_out,
        )

    token = _token_full(user, user_out.roles)
    await _registrar_login(db, usuario_id=user.id, usuario_intentado=ident,
                           resultado="ok", mfa_usado=mfa_usado, request=request)
    # actualizar ultimo_login (best-effort)
    try:
        await db.execute(
            text("UPDATE core.usuario SET ultimo_login = SYSDATETIMEOFFSET() WHERE id = :u"),
            {"u": user.id},
        )
        await db.commit()
    except SQLAlchemyError as e:
        await db.rollback()
        logger.warning("no se pudo actualizar ultimo_login: %s", e)
    logger.info("login ok user_id=%s email=%s mfa=%s", user.id, user.email, mfa_usado)
    return LoginOut(access_token=token, scope="full",
                    requiere_cambio_password=False, user=user_out)


@router.get("/me", response_model=UsuarioOut)
async def me(
    current: CurrentUser = Depends(get_current_user_any_scope),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Re-hidrata el usuario actual desde el token. /me funciona con cualquier
    scope para que el frontend pueda mostrar 'cambiá tu contraseña'."""
    stmt = (
        select(Usuario)
        .options(selectinload(Usuario.roles))
        .where(Usuario.id == current.id)
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise HTTPException(401, "Usuario inválido")
    return await _to_out(db, user)


@router.post("/cambiar-password")
async def cambiar_password(
    payload: CambiarPasswordIn,
    request: Request,
    current: CurrentUser = Depends(get_current_user_any_scope),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Cambia la contraseña del usuario autenticado.

    Acepta tanto tokens scope='full' (cambio voluntario) como scope='pwd_change'
    (cambio obligatorio post-reset). En ambos casos exige la password actual.
    """
    user = (await db.execute(
        select(Usuario)
        .options(selectinload(Usuario.roles))
        .where(Usuario.id == current.id)
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(401, "Usuario inválido")
    if not _verify_password(payload.password_actual, user.password_hash):
        raise HTTPException(400, "La contraseña actual no es correcta.")
    if payload.password_nueva == payload.password_actual:
        raise HTTPException(400, "La contraseña nueva debe ser distinta a la actual.")
    ok, msg = _password_cumple_reglas(payload.password_nueva)
    if not ok:
        raise HTTPException(400, msg or "Contraseña inválida.")

    new_hash = _hash_password(payload.password_nueva)
    await db.execute(
        text("""UPDATE core.usuario
                SET password_hash = :h,
                    requiere_cambio_password = 0,
                    updated_at = SYSDATETIMEOFFSET()
                WHERE id = :u"""),
        {"h": new_hash, "u": user.id},
    )
    await db.commit()
    logger.info("password cambiada user_id=%s", user.id)

    # Emitir nuevo token full para que el cliente pueda seguir operando sin re-login.
    user_out = await _to_out(db, user)
    token = _token_full(user, user_out.roles)
    return {"ok": True, "access_token": token, "scope": "full"}


@router.post("/mfa/setup", response_model=MfaSetupOut)
async def mfa_setup(
    payload: MfaSetupIn,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Genera y guarda un secret MFA pendiente. El usuario debe escanear el
    QR (otpauth_uri) y confirmar con /auth/mfa/enable. Mientras no confirme,
    `mfa_habilitado` queda en false.
    """
    user = (await db.execute(
        select(Usuario).where(Usuario.id == current.id)
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(401, "Usuario inválido")
    if not _verify_password(payload.password_actual, user.password_hash):
        raise HTTPException(400, "La contraseña actual no es correcta.")

    secret = pyotp.random_base32()
    await db.execute(
        text("""UPDATE core.usuario
                SET mfa_secret = :s, mfa_habilitado = 0, updated_at = SYSDATETIMEOFFSET()
                WHERE id = :u"""),
        {"s": secret, "u": user.id},
    )
    await db.commit()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=MFA_ISSUER)
    return MfaSetupOut(secret=secret, otpauth_uri=uri)


@router.post("/mfa/enable")
async def mfa_enable(
    payload: MfaEnableIn,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Confirma el setup MFA con un código TOTP válido. Idempotente."""
    user = (await db.execute(
        select(Usuario).where(Usuario.id == current.id)
    )).scalar_one_or_none()
    if not user or not user.mfa_secret:
        raise HTTPException(400, "Iniciá primero /auth/mfa/setup.")
    if not _verify_totp(user.mfa_secret, payload.codigo):
        raise HTTPException(400, "Código MFA inválido. Verificá que tu reloj esté sincronizado.")
    await db.execute(
        text("UPDATE core.usuario SET mfa_habilitado = 1, updated_at = SYSDATETIMEOFFSET() WHERE id = :u"),
        {"u": user.id},
    )
    await db.commit()
    logger.info("mfa habilitado user_id=%s", user.id)
    return {"ok": True, "mfa_habilitado": True}


@router.post("/mfa/disable")
async def mfa_disable(
    payload: MfaDisableIn,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Desactiva MFA. Requiere password + código TOTP vigente (anti-takeover)."""
    user = (await db.execute(
        select(Usuario).where(Usuario.id == current.id)
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(401, "Usuario inválido")
    if not _verify_password(payload.password_actual, user.password_hash):
        raise HTTPException(400, "La contraseña actual no es correcta.")
    if not _verify_totp(user.mfa_secret, payload.codigo):
        raise HTTPException(400, "Código MFA inválido.")
    await db.execute(
        text("""UPDATE core.usuario
                SET mfa_habilitado = 0, mfa_secret = NULL, updated_at = SYSDATETIMEOFFSET()
                WHERE id = :u"""),
        {"u": user.id},
    )
    await db.commit()
    logger.info("mfa deshabilitado user_id=%s", user.id)
    return {"ok": True, "mfa_habilitado": False}
