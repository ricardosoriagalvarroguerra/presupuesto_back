"""Modelos del schema `core` — datos institucionales: usuarios, roles, monedas, ciclos.

Estructura:
  Moneda               código ISO de moneda.
  TipoCambio           FX por par moneda × fecha.
  Rol                  roles del sistema (vicepresidente, jefe_unidad, etc.).
  Usuario              usuarios + flags para MFA y cross-VP.
  UsuarioRol           N a N usuarios ↔ roles.
  CicloPresupuestario  un registro por año presupuestario.
  Periodo              sub-períodos del ciclo (semestre/trimestre/mes).

Nota sobre IDs: la mayoría son INT pero `usuario.id` es BIGINT por
compatibilidad con datos importados; los FKs hacia usuario también son BIGINT.
"""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Moneda(Base):
    __tablename__ = "moneda"
    __table_args__ = {"schema": "core"}

    codigo: Mapped[str] = mapped_column(String(3), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(64))
    decimales: Mapped[int] = mapped_column(SmallInteger, default=2)


class TipoCambio(Base):
    __tablename__ = "tipo_cambio"
    __table_args__ = {"schema": "core"}

    fecha: Mapped[date] = mapped_column(Date, primary_key=True)
    moneda_origen: Mapped[str] = mapped_column(String(3), ForeignKey("core.moneda.codigo"), primary_key=True)
    moneda_destino: Mapped[str] = mapped_column(String(3), ForeignKey("core.moneda.codigo"), primary_key=True)
    tasa: Mapped[float] = mapped_column(Numeric(18, 6))


class Rol(Base):
    __tablename__ = "rol"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    codigo: Mapped[str] = mapped_column(String(32), unique=True)
    nombre: Mapped[str] = mapped_column(String(128))
    descripcion: Mapped[str | None] = mapped_column(String, nullable=True)


class Usuario(Base):
    __tablename__ = "usuario"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    username: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    nombre: Mapped[str] = mapped_column(String(128))
    apellido: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(255))
    estado: Mapped[str] = mapped_column(
        Enum("activo", "suspendido", "baja", name="usuario_estado", schema="core"),
        default="activo",
    )
    mfa_habilitado: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vp_codigo: Mapped[str | None] = mapped_column(String(8), nullable=True)
    ver_todo: Mapped[bool] = mapped_column(Boolean, default=False)
    cargo: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Flag agregado por migración 027: si está activo, el login devuelve un
    # token scope='pwd_change' que solo sirve para /auth/cambiar-password.
    requiere_cambio_password: Mapped[bool] = mapped_column(Boolean, default=False)
    ultimo_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("SYSDATETIMEOFFSET()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("SYSDATETIMEOFFSET()"))

    roles: Mapped[list[Rol]] = relationship(secondary="core.usuario_rol")


class UsuarioRol(Base):
    __tablename__ = "usuario_rol"
    __table_args__ = {"schema": "core"}

    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.usuario.id"), primary_key=True)
    rol_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.rol.id"), primary_key=True)


class CicloPresupuestario(Base):
    __tablename__ = "ciclo_presupuestario"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    anio: Mapped[int] = mapped_column(SmallInteger, unique=True)
    nombre: Mapped[str] = mapped_column(String(128))
    fecha_apertura: Mapped[date | None] = mapped_column(Date, nullable=True)
    fecha_cierre_solicitud: Mapped[date | None] = mapped_column(Date, nullable=True)
    fecha_cierre_directorio: Mapped[date | None] = mapped_column(Date, nullable=True)
    estado: Mapped[str] = mapped_column(
        Enum("planificacion", "vigente", "cerrado", name="ciclo_estado", schema="core"),
        default="planificacion",
    )
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.usuario.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("SYSDATETIMEOFFSET()"))


class Periodo(Base):
    __tablename__ = "periodo"
    __table_args__ = (
        UniqueConstraint("ciclo_id", "granularidad", "numero", name="uq_periodo_ciclo_gran_num"),
        {"schema": "core"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ciclo_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.ciclo_presupuestario.id"))
    granularidad: Mapped[str] = mapped_column(
        Enum("anual", "semestral", "trimestral", "mensual", name="periodo_granularidad", schema="core"),
    )
    numero: Mapped[int] = mapped_column(SmallInteger)
    fecha_inicio: Mapped[date] = mapped_column(Date)
    fecha_fin: Mapped[date] = mapped_column(Date)
