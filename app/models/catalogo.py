"""Modelos del schema `catalogo` — datos maestros del sistema.

Items (lo que se gasta) y Cuentas (en qué cuenta contable se imputa) son las
dos jerarquías centrales. Cada una arma un árbol N-ario con código dot-notation
(items: 02.05.01) o segmentado por punto (cuentas: 5.4.1.01), más una columna
`path` con notación tipo ltree (n02.n05.n01) que en PG usaba el tipo nativo
y en MSSQL emulamos con LIKE prefijo.

Otros catálogos:
  Gestor              dueño funcional del item.
  GestorItem          asignación N a N gestor ↔ item.
  RelacionItemCuenta  matriz de combinaciones VÁLIDAS item↔cuenta. Es el
                      guard duro: si la combinación no está acá, el backend
                      rechaza la línea con 400.
  TipoMovimiento      tipos K2B con categoría (inicial/modificacion/...).
  PlanPresupuestario  PRESUPDEGASTOS, PRESUPBUSO (Capital), etc.
  PlanillaTemplate    metadatos de cada planilla cargable.
  Formula/FormulaParametro  fórmulas paramétricas (vacíos por ahora).
  Posicion/ParametroDestino otros (vacíos por ahora).
  Tarifa*             tarifas de pasaje y hospedaje por ruta/ciudad/mes.
"""
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PlanPresupuestario(Base):
    __tablename__ = "plan_presupuestario"
    __table_args__ = {"schema": "catalogo"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    codigo: Mapped[str] = mapped_column(String(64), unique=True)
    nombre: Mapped[str] = mapped_column(String(255))
    tipo: Mapped[str] = mapped_column(
        Enum("operativo", "capital", "especial", name="plan_tipo", schema="catalogo")
    )
    k2b_plan_prefix: Mapped[str | None] = mapped_column(String(64), nullable=True)
    estado: Mapped[str] = mapped_column(String(16), default="activo")


class Gestor(Base):
    __tablename__ = "gestor"
    __table_args__ = {"schema": "catalogo"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    codigo: Mapped[str] = mapped_column(String(64), unique=True)
    nombre: Mapped[str] = mapped_column(String(255))
    vp_padre: Mapped[str | None] = mapped_column(String(255), nullable=True)
    k2b_gestor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estado: Mapped[str] = mapped_column(String(16), default="activo")


class ItemPlanificacion(Base):
    __tablename__ = "item_planificacion"
    __table_args__ = {"schema": "catalogo"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    codigo: Mapped[str] = mapped_column(String(64), unique=True)
    descripcion: Mapped[str] = mapped_column(String(512))
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("catalogo.item_planificacion.id"), nullable=True
    )
    nivel: Mapped[int] = mapped_column(SmallInteger)
    # path: ltree column managed via raw SQL — exposed as text for ORM I/O
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    imputable: Mapped[bool] = mapped_column(Boolean, default=False)
    tipo_presupuesto: Mapped[str] = mapped_column(
        Enum(
            "gastos",
            "inversiones_capital",
            "salarios",
            name="item_tipo_presup",
            schema="catalogo",
        )
    )
    k2b_item_id: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    estado: Mapped[str] = mapped_column(String(16), default="activo")
    vigente_desde: Mapped[date | None] = mapped_column(Date, nullable=True)
    vigente_hasta: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("SYSDATETIMEOFFSET()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("SYSDATETIMEOFFSET()"))


class CuentaPlanificacion(Base):
    __tablename__ = "cuenta_planificacion"
    __table_args__ = {"schema": "catalogo"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    codigo: Mapped[str] = mapped_column(String(32), unique=True)
    descripcion: Mapped[str] = mapped_column(String(512))
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("catalogo.cuenta_planificacion.id"), nullable=True
    )
    nivel: Mapped[int] = mapped_column(SmallInteger)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    imputable: Mapped[bool] = mapped_column(Boolean, default=False)
    modalidad_default: Mapped[str] = mapped_column(
        Enum("parametrizada", "directa", name="cuenta_modalidad", schema="catalogo"),
        default="directa",
    )
    k2b_cuenta_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estado: Mapped[str] = mapped_column(String(16), default="activo")


class RelacionItemCuenta(Base):
    __tablename__ = "relacion_item_cuenta"
    __table_args__ = {"schema": "catalogo"}

    item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("catalogo.item_planificacion.id"), primary_key=True
    )
    cuenta_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("catalogo.cuenta_planificacion.id"), primary_key=True
    )
    obligatoria: Mapped[bool] = mapped_column(Boolean, default=False)
    modalidad_override: Mapped[str | None] = mapped_column(
        Enum("parametrizada", "directa", name="cuenta_modalidad", schema="catalogo"),
        nullable=True,
    )


class GestorItem(Base):
    __tablename__ = "gestor_item"
    __table_args__ = {"schema": "catalogo"}

    gestor_id: Mapped[int] = mapped_column(Integer, ForeignKey("catalogo.gestor.id"), primary_key=True)
    item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("catalogo.item_planificacion.id"), primary_key=True
    )


class TipoMovimiento(Base):
    __tablename__ = "tipo_movimiento"
    __table_args__ = (
        CheckConstraint("signo IN (-1, 1)", name="ck_signo"),
        {"schema": "catalogo"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    k2b_codigo: Mapped[str] = mapped_column(String(64), unique=True)
    nombre: Mapped[str] = mapped_column(String(255))
    categoria: Mapped[str] = mapped_column(
        Enum(
            "inicial",
            "modificacion",
            "compromiso",
            "devengado",
            "pagado",
            "reverso",
            "especial",
            name="tipo_mov_categoria",
            schema="catalogo",
        )
    )
    signo: Mapped[int] = mapped_column(SmallInteger)
    estado: Mapped[str] = mapped_column(String(16), default="activo")


class Posicion(Base):
    __tablename__ = "posicion"
    __table_args__ = {"schema": "catalogo"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    codigo: Mapped[str] = mapped_column(String(64), unique=True)
    nombre: Mapped[str] = mapped_column(String(255))
    grado: Mapped[str | None] = mapped_column(String(32), nullable=True)
    monto_promedio_anual: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    estado: Mapped[str] = mapped_column(String(16), default="activo")


class Formula(Base):
    __tablename__ = "formula"
    __table_args__ = {"schema": "catalogo"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    codigo: Mapped[str] = mapped_column(String(64), unique=True)
    nombre: Mapped[str] = mapped_column(String(255))
    expresion: Mapped[str] = mapped_column(Text)
    cuenta_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("catalogo.cuenta_planificacion.id"), nullable=True
    )
    vigencia_desde: Mapped[date | None] = mapped_column(Date, nullable=True)
    vigencia_hasta: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.usuario.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("SYSDATETIMEOFFSET()"))


class FormulaParametro(Base):
    __tablename__ = "formula_parametro"
    __table_args__ = (
        UniqueConstraint("formula_id", "codigo", name="uq_param_formula_codigo"),
        {"schema": "catalogo"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    formula_id: Mapped[int] = mapped_column(Integer, ForeignKey("catalogo.formula.id"))
    codigo: Mapped[str] = mapped_column(String(64))
    tipo: Mapped[str] = mapped_column(
        Enum(
            "numero", "fecha", "texto", "lista", "ref_destino",
            name="param_tipo", schema="catalogo"
        )
    )
    obligatorio: Mapped[bool] = mapped_column(Boolean, default=True)
    orden: Mapped[int] = mapped_column(SmallInteger, default=0)
    unidad: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ParametroDestino(Base):
    __tablename__ = "parametro_destino"
    __table_args__ = (
        UniqueConstraint("destino", "tipo", "vigente_desde", name="uq_dest_tipo_vig"),
        {"schema": "catalogo"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    destino: Mapped[str] = mapped_column(String(8))
    tipo: Mapped[str] = mapped_column(
        Enum("pasaje", "viatico", "hospedaje", "perdiem", "otros",
             name="dest_tipo", schema="catalogo")
    )
    monto: Mapped[float] = mapped_column(Numeric(18, 2))
    vigente_desde: Mapped[date] = mapped_column(Date)
    vigente_hasta: Mapped[date | None] = mapped_column(Date, nullable=True)


class PlanillaTemplate(Base):
    __tablename__ = "planilla_template"
    __table_args__ = {"schema": "catalogo"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    codigo: Mapped[str] = mapped_column(String(64), unique=True)
    nombre: Mapped[str] = mapped_column(String(255))
    descripcion: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope_filter: Mapped[dict] = mapped_column(JSON)
    modalidad_permitida: Mapped[str] = mapped_column(
        Enum("parametrizada", "directa", "ambas", name="planilla_modalidad", schema="catalogo")
    )
    formula_default_codigo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    columnas_visibles: Mapped[list[dict]] = mapped_column(JSON)
    reglas_validacion: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    orden: Mapped[int] = mapped_column(SmallInteger, default=0)
    vigente_desde: Mapped[date | None] = mapped_column(Date, nullable=True)
    vigente_hasta: Mapped[date | None] = mapped_column(Date, nullable=True)
    estado: Mapped[str] = mapped_column(String(16), default="activo")
    # Si `solo_cross_vp` = True, la planilla solo puede cargarse por usuarios que
    # la tengan en `core.usuario_planilla_extra` — la regla por defecto "estoy en
    # mi propia VP, puedo todas las planillas" NO aplica. Usado para planillas
    # institucionales con dueño único (ej. Gastos de Administración → mgarcia).
    solo_cross_vp: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
