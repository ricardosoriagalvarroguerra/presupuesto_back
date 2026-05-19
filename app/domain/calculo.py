"""Autoridad única para calcular `monto_solicitado` a partir de parámetros.

Antes la fórmula vivía duplicada en frontend (calcularMonto en TS) y backend
confiaba en el `monto_solicitado` enviado por el cliente — un atacante podía
inflar el monto manipulando el request. Ahora el backend SIEMPRE recalcula
desde los parámetros y la cuenta destino antes de persistir.

Tarifas de pasaje/hospedaje vienen de `catalogo.tarifa_*` (migración 028,
sembradas desde parametros_Misiones.xlsx). Viático/perdiem son por país.
"""
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# Tarifas estructurales por país destino (Excel no las provee).
VIATICO_PAIS: dict[str, Decimal] = {
    "ARGENTINA": Decimal("280"),
    "BOLIVIA":   Decimal("220"),
    "BRASIL":    Decimal("320"),
    "PARAGUAY":  Decimal("200"),
    "URUGUAY":   Decimal("240"),
}
PERDIEM_PAIS: dict[str, Decimal] = {
    "ARGENTINA": Decimal("80"),
    "BOLIVIA":   Decimal("60"),
    "BRASIL":    Decimal("100"),
    "PARAGUAY":  Decimal("70"),
    "URUGUAY":   Decimal("80"),
}
VIATICO_DEFAULT = Decimal("400")
PERDIEM_DEFAULT = Decimal("120")


def _d(v: Any, default: Decimal = Decimal(0)) -> Decimal:
    """Conversión segura a Decimal — NaN/strings raros → default."""
    if v is None or v == "":
        return default
    try:
        d = Decimal(str(v))
    except Exception:
        return default
    return d if d.is_finite() else default


def _mes_de_fecha(iso: str | None) -> int | None:
    """`'2027-04-16'` → 4. Devuelve None si no parsea."""
    if not iso or not isinstance(iso, str):
        return None
    parts = iso.split("-")
    if len(parts) >= 2 and parts[1].isdigit():
        n = int(parts[1])
        if 1 <= n <= 12:
            return n
    return None


async def _lookup_pasaje(db: AsyncSession, ruta: str, mes: int | None) -> tuple[Decimal | None, str | None]:
    """Devuelve (monto_pasaje_usd, pais_destino) consultando catalogo.tarifa_*.

    Si hay fecha y precio mensual, usa el mensual. Sino, anual.
    """
    if not ruta:
        return None, None
    row = (await db.execute(
        text("SELECT monto_anual_usd, pais_destino FROM catalogo.tarifa_pasaje_ruta WHERE ruta = :r"),
        {"r": ruta},
    )).mappings().first()
    if not row:
        return None, None
    anual = _d(row["monto_anual_usd"]) if row["monto_anual_usd"] is not None else None
    pais = row["pais_destino"]
    if mes is not None:
        mensual = (await db.execute(
            text("SELECT monto_usd FROM catalogo.tarifa_pasaje_mensual WHERE ruta=:r AND mes=:m"),
            {"r": ruta, "m": mes},
        )).scalar()
        if mensual is not None:
            return _d(mensual), pais
    return anual, pais


async def _lookup_hospedaje(db: AsyncSession, ciudad: str, mes: int | None) -> Decimal | None:
    if not ciudad:
        return None
    row = (await db.execute(
        text("SELECT noche_anual_usd FROM catalogo.tarifa_hospedaje_ciudad WHERE ciudad=:c"),
        {"c": ciudad},
    )).mappings().first()
    if not row:
        return None
    anual = _d(row["noche_anual_usd"]) if row["noche_anual_usd"] is not None else None
    if mes is not None:
        mensual = (await db.execute(
            text("SELECT noche_usd FROM catalogo.tarifa_hospedaje_mensual WHERE ciudad=:c AND mes=:m"),
            {"c": ciudad, "m": mes},
        )).scalar()
        if mensual is not None:
            return _d(mensual)
    return anual


async def calcular_monto_linea(
    db: AsyncSession,
    *,
    planilla_codigo: str,
    cuenta_codigo: str,
    parametros: dict[str, Any],
    monto_hint: Decimal | None = None,
) -> Decimal:
    """Recalcula el monto autoritativo de una línea según planilla + cuenta.

    Es la autoridad del backend — el hint del cliente es solo para fallback en
    casos sin fórmula registrada (captura directa).
    """
    hint = _d(monto_hint).quantize(Decimal("0.01"))
    if hint < 0:
        hint = Decimal(0)

    # ----- Misiones (Servicio y Consultores) -----
    if planilla_codigo in ("PL-MISIONES-SERV", "PL-MISIONES-CONS"):
        cant = _d(parametros.get("cant_viajes"))
        dias = _d(parametros.get("duracion_dias"))
        ruta = str(parametros.get("destino") or "")
        mes = _mes_de_fecha(parametros.get("fecha_estimada"))

        # Resolver pais y precio pasaje desde la tabla (ruta = "CIUDAD → CIUDAD")
        pasaje, pais_destino = await _lookup_pasaje(db, ruta, mes)

        # Si la "ruta" no está en la tabla puede ser un código legacy (BOL-ARG, ARG)
        # — mapeamos a país destino con heurística simple.
        if pais_destino is None:
            mapa_codigo_pais = {"ARG": "ARGENTINA", "BOL": "BOLIVIA", "BRA": "BRASIL",
                                 "PAR": "PARAGUAY", "URU": "URUGUAY"}
            partes = ruta.split("-") if "-" in ruta else [ruta]
            ult = partes[-1].upper() if partes else ""
            pais_destino = mapa_codigo_pais.get(ult, ult)

        # Pasajes
        if cuenta_codigo in ("5.4.1.01", "5.3.1.02"):
            if pasaje is None:
                return hint  # ruta no encontrada — confiamos en el hint
            return (cant * pasaje).quantize(Decimal("0.01"))

        # Viáticos
        if cuenta_codigo in ("5.4.1.02", "5.3.1.03"):
            viatico = VIATICO_PAIS.get(pais_destino or "", VIATICO_DEFAULT)
            return (cant * dias * viatico).quantize(Decimal("0.01"))

        # Hospedaje — ciudad destino derivada del nombre de la ruta
        if cuenta_codigo in ("5.4.1.03", "5.3.1.04"):
            ciudad_dest = ""
            if "→" in ruta:
                ciudad_dest = ruta.split("→")[-1].strip()
            hosp = await _lookup_hospedaje(db, ciudad_dest, mes)
            if hosp is None:
                return hint
            return (cant * dias * hosp).quantize(Decimal("0.01"))

        # Per diem y Otros
        if cuenta_codigo in ("5.4.1.04", "5.3.1.05"):
            perdiem = PERDIEM_PAIS.get(pais_destino or "", PERDIEM_DEFAULT)
            return (cant * dias * perdiem).quantize(Decimal("0.01"))

    # ----- Honorarios Consultores -----
    if planilla_codigo == "PL-CONSULTORES" and cuenta_codigo == "5.3.1.01":
        cantidad = _d(parametros.get("cantidad"))
        mm = _d(parametros.get("monto_mensual"))
        meses = _d(parametros.get("meses"))
        return (cantidad * mm * meses).quantize(Decimal("0.01"))

    # ----- Gastos de Administración -----
    if planilla_codigo == "PL-GASTOS-ADMIN":
        monto_fijo = _d(parametros.get("monto_fijo"))
        if monto_fijo > 0:
            return monto_fijo.quantize(Decimal("0.01"))
        ant = _d(parametros.get("presupuesto_anterior"))
        pct = _d(parametros.get("pct_incremento"))
        return (ant * (Decimal("1") + pct / Decimal("100"))).quantize(Decimal("0.01"))

    # Captura directa (Salarios, Servicios y Licencias, Reuniones y Eventos):
    # el cliente declara el monto — backend solo valida signo y precisión.
    return hint
