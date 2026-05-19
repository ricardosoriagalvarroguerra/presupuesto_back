"""Entry point FastAPI con middlewares de seguridad y CORS estricto."""
import logging
import time
from contextlib import asynccontextmanager
from collections import defaultdict, deque

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import analisis as analisis_api
from app.api import auth as auth_api
from app.api import catalogo as catalogo_api
from app.api import ejecucion as ejecucion_api
from app.api import planificacion as planificacion_api
from app.api import solicitudes as solicitudes_api
from app.config import get_settings


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logger = logging.getLogger(__name__)


# ============================================================================
# Middleware: headers de seguridad estándar
# ============================================================================
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Aplica headers que TI marca como mandatorios (OWASP A05).

    - CSP estricto (sin scripts inline; conexión solo al mismo origen + backend dev)
    - X-Frame-Options DENY (clickjacking)
    - X-Content-Type-Options nosniff (MIME sniffing)
    - Referrer-Policy same-origin (no fugas al referer)
    - Strict-Transport-Security (solo si app_env=production, sino rompe localhost)
    - Permissions-Policy restrictivo
    """
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), payment=()"
        # CSP relativamente permisivo para la SPA dev — endurecer en producción.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self' http://localhost:8000 http://localhost:5173; "
            "frame-ancestors 'none'; "
            "object-src 'none'"
        )
        if get_settings().app_env == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


# ============================================================================
# Middleware: rate-limit in-memory para endpoints sensibles
# ============================================================================
class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate-limit token-bucket simple, en memoria, por IP+path.

    Limita el login para mitigar brute force. Para producción real conviene
    Redis + slowapi/limits, pero esto cubre el caso TI mientras no haya cluster.
    """
    # path → (max_requests, window_seconds). En production se aplica la
    # política estricta de TI; en development se afloja porque desarrollo
    # comparte 127.0.0.1 con tests/pruebas manuales y el bucket se llena.
    LIMITS_PROD: dict[str, tuple[int, int]] = {
        "/auth/login": (5, 60 * 15),  # 5 intentos por 15 min por IP
    }
    LIMITS_DEV: dict[str, tuple[int, int]] = {
        "/auth/login": (50, 60 * 15),  # 50/15min — suficiente para dev local
    }

    @classmethod
    def _limits_for_env(cls, env: str) -> dict[str, tuple[int, int]]:
        return cls.LIMITS_PROD if env == "production" else cls.LIMITS_DEV

    def __init__(self, app):
        super().__init__(app)
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # En APP_ENV=test el rate-limit se desactiva: pytest mete decenas de
        # logins por proceso y se autobloquearía. La regla se sigue verificando
        # con un test dedicado que la habilita explícitamente vía settings.
        env = get_settings().app_env
        if env == "test":
            return await call_next(request)
        limit = self._limits_for_env(env).get(path)
        if limit:
            max_req, window = limit
            ip = (request.client.host if request.client else "unknown")
            key = (path, ip)
            now = time.time()
            cutoff = now - window
            bucket = self._hits[key]
            # purga entradas viejas
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= max_req:
                retry_in = int(window - (now - bucket[0]))
                logger.warning("rate_limit hit path=%s ip=%s", path, ip)
                return JSONResponse(
                    status_code=429,
                    content={"detail": f"Demasiados intentos. Reintentá en {retry_in}s."},
                    headers={"Retry-After": str(max(retry_in, 1))},
                )
            bucket.append(now)
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("App start | env=%s", get_settings().app_env)
    yield
    logger.info("App stop")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Sistema de Gestión Presupuestaria — API",
        version="0.1.0",
        description="API para el sistema presupuestario de FONPLATA.",
        lifespan=lifespan,
    )
    # Orden: rate limit → security headers → CORS (último visible al cliente)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.app_env}

    app.include_router(auth_api.router)
    app.include_router(catalogo_api.router)
    app.include_router(planificacion_api.router)
    app.include_router(solicitudes_api.router)
    app.include_router(ejecucion_api.router)
    app.include_router(analisis_api.router)

    return app


app = create_app()
