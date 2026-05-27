"""Modelos del schema `planificacion` — la parte transaccional del sistema.

Tablas (en orden de relación):
  Solicitud            paquete de cargado para (ciclo, VP). Única por (ciclo, VP).
  LineaSolicitud       cada renglón cargado (item × cuenta + monto + params).
  EventoSolicitud      audit log inmutable de las acciones (append-only).
  Observacion          notas del revisor (VP/Presidencia) hacia el cargador.
  SnapshotSolicitud    foto inmutable del estado en un hito del workflow.
  SnapshotLinea        foto de cada línea dentro de un snapshot.
  AdjuntoLinea         archivos PDF/Word/Excel justificativos.

Estos modelos son la fuente de verdad para SQLAlchemy (originalmente el
schema vivía solo en migraciones Alembic como SQL crudo, lo que impedía
usar autogenerate y relationships tipados).

Tipos: `JSONB` de PG → `JSON` de SQLAlchemy → `NVARCHAR(MAX)` en MSSQL.
Los operadores `?` y `@>` de PG no existen en MSSQL; para leer un campo
puntual se usa `JSON_VALUE(col, '$.key')` (ver queries de
`eliminar_lineas_grupo` en `api/solicitudes.py`).
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


# ============================================================================
# Solicitud — expediente raíz por (ciclo, VP)
# ============================================================================
class Solicitud(Base):
    __tablename__ = "solicitud"
    __table_args__ = (
        # Una sola solicitud por ciclo × VP — regla de negocio en BD (no app).
        UniqueConstraint("ciclo_id", "vp_codigo", name="uq_solicitud_ciclo_vp"),
        {"schema": "planificacion"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ciclo_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.ciclo_presupuestario.id"), nullable=False)
    vp_codigo: Mapped[str] = mapped_column(String(8), nullable=False)
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    etapa_actual: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("'0'"))
    estado_workflow: Mapped[str] = mapped_column(
        # Workflow nuevo (mig 030) + legacy. Mantener sincronía con
        # app/domain/enums.py::EstadoWorkflow y con el enum en DB.
        Enum(
            "en_elaboracion",
            # Nuevos
            "en_revision_vp", "observado_vp", "devuelto_vp",
            "en_revision_presidencia", "observado_presidencia", "devuelto_presidencia",
            "aprobado", "cerrado",
            # Legacy (filas históricas)
            "enviado_revision", "en_revision", "observado", "devuelto", "validado",
            name="solicitud_estado_wf", schema="planificacion",
        ),
        nullable=False,
        server_default=text("'en_elaboracion'"),
    )
    monto_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, server_default=text("0"))
    monto_aprobado: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, server_default=text("0"))
    comentario_actual: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core.usuario.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("SYSDATETIMEOFFSET()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("SYSDATETIMEOFFSET()"), nullable=False)
    enviado_a_revision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    aprobado_objetivos_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # legacy
    aprobado_vp_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # mig 030
    aprobado_presidencia_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    aprobado_directorio_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # legacy

    lineas: Mapped[list["LineaSolicitud"]] = relationship(
        back_populates="solicitud", cascade="all, delete-orphan", passive_deletes=True
    )
    eventos: Mapped[list["EventoSolicitud"]] = relationship(
        back_populates="solicitud", cascade="all, delete-orphan", passive_deletes=True
    )
    observaciones: Mapped[list["Observacion"]] = relationship(
        back_populates="solicitud", cascade="all, delete-orphan", passive_deletes=True
    )
    snapshots: Mapped[list["SnapshotSolicitud"]] = relationship(
        back_populates="solicitud", cascade="all, delete-orphan", passive_deletes=True
    )


# ============================================================================
# Línea de solicitud — fila imputada (item × cuenta × monto)
# ============================================================================
class LineaSolicitud(Base):
    __tablename__ = "linea_solicitud"
    __table_args__ = {"schema": "planificacion"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    solicitud_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("planificacion.solicitud.id", ondelete="CASCADE"), nullable=False
    )
    planilla_template_id: Mapped[int] = mapped_column(Integer, nullable=False)  # FK a catalogo.planilla_template
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    cuenta_id: Mapped[int] = mapped_column(Integer, nullable=False)
    gestor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_id: Mapped[int] = mapped_column(Integer, nullable=False)
    modalidad: Mapped[str] = mapped_column(String(16), nullable=False)  # 'parametrizada' | 'directa'
    formula_codigo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parametros: Mapped[dict | None] = mapped_column(JSON, server_default=text("'{}'"))
    monto_solicitado: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, server_default=text("0"))
    monto_objetivos: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)   # legacy
    monto_vp: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)          # mig 030
    monto_presidencia: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    monto_directorio: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)  # legacy
    justificacion: Mapped[str | None] = mapped_column(Text, nullable=True)
    estado_linea: Mapped[str] = mapped_column(
        Enum("borrador", "validada", "observada", "aprobada", "rechazada",
             name="linea_estado", schema="planificacion"),
        nullable=False, server_default=text("'borrador'"),
    )
    observacion: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core.usuario.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("SYSDATETIMEOFFSET()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("SYSDATETIMEOFFSET()"), nullable=False)

    solicitud: Mapped[Solicitud] = relationship(back_populates="lineas")
    adjuntos: Mapped[list["AdjuntoLinea"]] = relationship(
        back_populates="linea", cascade="all, delete-orphan", passive_deletes=True
    )


# ============================================================================
# Evento auditable
# ============================================================================
class EventoSolicitud(Base):
    __tablename__ = "evento_solicitud"
    __table_args__ = {"schema": "planificacion"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    solicitud_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("planificacion.solicitud.id", ondelete="CASCADE"), nullable=False
    )
    linea_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("planificacion.linea_solicitud.id", ondelete="SET NULL"), nullable=True
    )
    accion: Mapped[str] = mapped_column(
        # Mantener sincronía con app/domain/enums.py::AccionEvento y migración 031.
        Enum(
            "crear_solicitud", "agregar_linea", "modificar_linea", "eliminar_linea",
            # Workflow nuevo (mig 031)
            "enviar_a_revision_vp", "enviar_a_revision_presidencia",
            "aprobar_vp", "observar_vp", "devolver_vp",
            "aprobar_presidencia", "observar_presidencia", "devolver_presidencia",
            "cerrar",
            # Legacy
            "enviar_a_revision", "aprobar_objetivos", "aprobar_directorio", "observar", "devolver",
            # Adjuntos / observaciones / snapshot
            "subir_adjunto", "eliminar_adjunto",
            "crear_observacion", "aplicar_observacion", "rechazar_observacion", "snapshot",
            name="evento_accion", schema="planificacion",
        ),
        nullable=False,
    )
    etapa_anterior: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    etapa_nueva: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    estado_anterior: Mapped[str | None] = mapped_column(String(32), nullable=True)
    estado_nuevo: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    usuario_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core.usuario.id", ondelete="SET NULL"), nullable=True
    )
    comentario: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("SYSDATETIMEOFFSET()"), nullable=False)

    solicitud: Mapped[Solicitud] = relationship(back_populates="eventos")


# ============================================================================
# Observaciones (ciclo revisión Presidencia ↔ VP)
# ============================================================================
class Observacion(Base):
    __tablename__ = "observacion"
    __table_args__ = {"schema": "planificacion"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    solicitud_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("planificacion.solicitud.id", ondelete="CASCADE"), nullable=False
    )
    linea_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("planificacion.linea_solicitud.id", ondelete="SET NULL"), nullable=True
    )
    planilla_template_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    alcance: Mapped[str] = mapped_column(
        Enum("general", "planilla", "linea", name="observacion_alcance", schema="planificacion"),
        nullable=False,
    )
    texto: Mapped[str] = mapped_column(Text, nullable=False)
    accion_sugerida: Mapped[str | None] = mapped_column(
        Enum("modificar_monto", "modificar_parametro", "eliminar_linea", "agregar_linea", "otro",
             name="observacion_accion", schema="planificacion"),
        nullable=True,
    )
    valor_sugerido: Mapped[dict] = mapped_column(JSON, nullable=False, server_default=text("'{}'"))
    estado: Mapped[str] = mapped_column(
        Enum("abierta", "aplicada", "rechazada", name="observacion_estado", schema="planificacion"),
        nullable=False, server_default=text("'abierta'"),
    )
    etapa_origen: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("3"))
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core.usuario.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("SYSDATETIMEOFFSET()"), nullable=False)
    resuelta_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core.usuario.id", ondelete="SET NULL"), nullable=True
    )
    resuelta_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolucion_comentario: Mapped[str | None] = mapped_column(Text, nullable=True)

    solicitud: Mapped[Solicitud] = relationship(back_populates="observaciones")


# ============================================================================
# Adjuntos por línea
# ============================================================================
class AdjuntoLinea(Base):
    __tablename__ = "adjunto_linea"
    __table_args__ = {"schema": "planificacion"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    linea_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("planificacion.linea_solicitud.id", ondelete="CASCADE"), nullable=False
    )
    nombre_original: Mapped[str] = mapped_column(String(255), nullable=False)
    tipo_mime: Mapped[str] = mapped_column(String(120), nullable=False)
    tamano_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    path_relativo: Mapped[str] = mapped_column(String(500), nullable=False)
    subido_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core.usuario.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("SYSDATETIMEOFFSET()"), nullable=False)

    linea: Mapped[LineaSolicitud] = relationship(back_populates="adjuntos")


# ============================================================================
# Snapshots (auditoría e input de dashboards comparativos)
# ============================================================================
class SnapshotSolicitud(Base):
    __tablename__ = "snapshot_solicitud"
    __table_args__ = {"schema": "planificacion"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    solicitud_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("planificacion.solicitud.id", ondelete="CASCADE"), nullable=False
    )
    etapa: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    motivo: Mapped[str] = mapped_column(
        Enum("enviado_a_revision", "devuelto_con_observaciones", "reaprobado_post_ajustes",
             "aprobado_objetivos", "aprobado_presidencia", "aprobado_directorio", "cerrado",
             name="snapshot_motivo", schema="planificacion"),
        nullable=False,
    )
    monto_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, server_default=text("0"))
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core.usuario.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("SYSDATETIMEOFFSET()"), nullable=False)

    solicitud: Mapped[Solicitud] = relationship(back_populates="snapshots")
    lineas: Mapped[list["SnapshotLinea"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan", passive_deletes=True
    )


class SnapshotLinea(Base):
    __tablename__ = "snapshot_linea"
    __table_args__ = {"schema": "planificacion"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("planificacion.snapshot_solicitud.id", ondelete="CASCADE"), nullable=False
    )
    linea_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # SET NULL si la línea desaparece
    item_codigo: Mapped[str | None] = mapped_column(String(40), nullable=True)
    cuenta_codigo: Mapped[str | None] = mapped_column(String(40), nullable=True)
    plan_codigo: Mapped[str | None] = mapped_column(String(40), nullable=True)
    parametros: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    monto_solicitado: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    monto_objetivos: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    monto_presidencia: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    monto_directorio: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    justificacion: Mapped[str | None] = mapped_column(Text, nullable=True)

    snapshot: Mapped[SnapshotSolicitud] = relationship(back_populates="lineas")


# ============================================================================
# core.usuario_planilla_extra (cross-VP por planilla, caso Angel)
# ============================================================================
class UsuarioPlanillaExtra(Base):
    __tablename__ = "usuario_planilla_extra"
    __table_args__ = {"schema": "core"}

    usuario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("core.usuario.id", ondelete="CASCADE"), primary_key=True
    )
    planilla_codigo: Mapped[str] = mapped_column(String(64), primary_key=True)
    # FK declarada en migración 026 contra catalogo.planilla_template(codigo)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("SYSDATETIMEOFFSET()"), nullable=False)
