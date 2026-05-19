from typing import Any

from pydantic import BaseModel, ConfigDict


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ItemOut(_ORM):
    id: int
    codigo: str
    descripcion: str
    parent_id: int | None
    nivel: int
    imputable: bool
    tipo_presupuesto: str
    k2b_item_id: int | None


class CuentaOut(_ORM):
    id: int
    codigo: str
    descripcion: str
    parent_id: int | None
    nivel: int
    imputable: bool
    modalidad_default: str
    k2b_cuenta_id: int | None


class GestorOut(_ORM):
    id: int
    codigo: str
    nombre: str
    vp_padre: str | None


class PlanOut(_ORM):
    id: int
    codigo: str
    nombre: str
    tipo: str


class TipoMovimientoOut(_ORM):
    id: int
    k2b_codigo: str
    nombre: str
    categoria: str
    signo: int


class PlanillaTemplateOut(_ORM):
    id: int
    codigo: str
    nombre: str
    descripcion: str | None
    scope_filter: dict[str, Any]
    modalidad_permitida: str
    formula_default_codigo: str | None
    columnas_visibles: list[dict[str, Any]]
    reglas_validacion: dict[str, Any] | None
    orden: int
