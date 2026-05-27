"""Cálculo del monto autoritativo de cada línea.

Toda planilla parametrizada (Misiones, Consultores con cantidad×meses, Gastos
Admin con presupuesto previo + % de incremento) pasa por acá. Para esas
planillas el backend ignora el `monto_solicitado` que mande el cliente y lo
recalcula desde los parámetros — así un POST manipulado con un monto inflado
no logra persistir nada distinto a lo que dicen las tarifas.

Fuentes de tarifas (todas en la BDR, schema `catalogo`):
  - `tarifa_pasaje_ruta` / `tarifa_pasaje_mensual` → pasajes por ruta y mes.
  - `tarifa_hospedaje_ciudad` / `tarifa_hospedaje_mensual` → noches de hotel
    por ciudad destino y mes.
  - `parametro_destino` → viáticos y per-diem por país, con vigencia. La
    fila `destino='*'` actúa como fallback cuando el país no está en la tabla.
  - Estas tarifas se crearon mediante datos historicos de misiones anteriores.
  - Para viaticos y perdiem se usaron datos ficticios, ya que no se cuenta con la reglamentación vigente.

Si una tarifa requerida no está cargada, la línea se rechaza con
`CalculoError` (HTTP 422). No usamos el hint del cliente como fallback porque
abriría la puerta a montos arbitrarios.
PD: Tambien se tienen planillas que son dadas o mejor dicho con valores dados por el usuario y no asi
un calculo que realiza el servidor.
"""
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class CalculoError(ValueError):
    """Una línea parametrizada no resolvió su monto → la rechazamos con 422.

    Se levanta cuando la tarifa (pasaje/hospedaje) requerida no existe en el
    catálogo. La alternativa sería caer al `monto_hint` del cliente, pero eso
    abriría la puerta a montos arbitrarios — preferimos forzar que se cargue
    la tarifa antes de aceptar la línea.
    """


# Mapeo nombre largo (como aparece en la ruta) → código corto (como se guarda
# en `catalogo.parametro_destino`). La tabla usa códigos ISO-ish de 3 letras
# para mantener el campo `destino` chico (VARCHAR(8)) y consistente.
_NOMBRE_A_COD: dict[str, str] = {
    "ARGENTINA": "ARG",
    "BOLIVIA":   "BOL",
    "BRASIL":    "BRA",
    "PARAGUAY":  "PAR",
    "URUGUAY":   "URU",
}

# Fila wildcard en `parametro_destino` — se usa cuando el país de destino no
# tiene su propia fila. Hay una para 'viatico' y otra para 'perdiem'.
_DESTINO_FALLBACK = "*"


def _d(v: Any, default: Decimal = Decimal(0)) -> Decimal:
    """Decimal seguro: si llega None, '', NaN o un string raro, devuelve `default`.

    `parametros` entra como JSON desde el front, así que un campo puede llegar
    como null, "abc" o un número con coma decimal. Sin este wrapper cualquiera
    de esos casos rompería el cálculo entero.
    """
    if v is None or v == "":
        return default
    try:
        d = Decimal(str(v))
    except (ValueError, ArithmeticError, TypeError):
        # Decimal levanta InvalidOperation (subclase de ArithmeticError) ante
        # strings no numéricos; TypeError ante None/lists/etc.
        return default
    return d if d.is_finite() else default


def _mes_de_fecha(iso: str | None) -> int | None:
    """Saca el mes de un string ISO ('2027-04-16' → 4). None si no parsea.

    Evito `datetime.fromisoformat` a propósito: hay filas con formato medio
    flojo (`'2027-4'`, sufijos de zona) que harían fallar el parser estricto.
    Esta versión es tolerante y solo necesita los dos primeros segmentos.
    """
    if not iso or not isinstance(iso, str):
        return None
    parts = iso.split("-")
    if len(parts) >= 2 and parts[1].isdigit():
        n = int(parts[1])
        if 1 <= n <= 12:
            return n
    return None


async def _lookup_pasaje(db: AsyncSession, ruta: str, mes: int | None) -> tuple[Decimal | None, str | None]:
    """(monto_pasaje_usd, pais_destino) desde catalogo.tarifa_pasaje_*.

    Si hay tarifa mensual cargada para ese mes, la usa (la mensual suele estar
    diferenciada para captar variación por temporada). Si no, cae a la anual.
    Si la ruta no existe en la tabla devuelve (None, None) y el caller decide.
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
    # Misma lógica que pasaje pero indexado por ciudad — el catálogo guarda
    # hospedaje por nodo destino, no por par origen-destino, así alcanza con
    # el nombre de la ciudad final.
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


async def _lookup_parametro_destino(
    db: AsyncSession,
    pais_destino: str,
    tipo: str,  # 'viatico' | 'perdiem'
) -> Decimal:
    """Devuelve el monto vigente para un (destino, tipo) desde catalogo.parametro_destino.

    Estrategia:
      1. Mapea el nombre largo del país a su código corto (ARG, BOL, ...).
      2. Busca la fila más vigente para ese código + tipo.
      3. Si no hay fila específica, cae al wildcard (destino='*').
      4. Si tampoco existe el wildcard (no debería pasar), devuelve 0 — la
         tabla está mal sembrada y el cálculo del monto va a quedar en cero;
         es preferible eso a explotar con KeyError.

    "Vigente" se resuelve con `vigente_desde <= hoy AND (vigente_hasta IS NULL
    OR vigente_hasta >= hoy)`. Si hay varias filas vigentes, gana la de
    `vigente_desde` más reciente.
    """
    codigo = _NOMBRE_A_COD.get((pais_destino or "").upper(), pais_destino or "")
    sql = text("""
        SELECT TOP 1 monto
        FROM catalogo.parametro_destino
        WHERE destino IN (:cod, :fallback)
          AND tipo = :tipo
          AND vigente_desde <= CAST(SYSDATETIMEOFFSET() AS date)
          AND (vigente_hasta IS NULL OR vigente_hasta >= CAST(SYSDATETIMEOFFSET() AS date))
        ORDER BY
          CASE WHEN destino = :cod THEN 0 ELSE 1 END,  -- la fila específica tiene prioridad sobre el wildcard
          vigente_desde DESC
    """)
    val = (await db.execute(
        sql,
        {"cod": codigo, "fallback": _DESTINO_FALLBACK, "tipo": tipo},
    )).scalar()
    return _d(val)


async def calcular_monto_linea(
    db: AsyncSession,
    *,
    planilla_codigo: str,
    cuenta_codigo: str,
    parametros: dict[str, Any],
    monto_hint: Decimal | None = None,
) -> Decimal:
    """Resuelve el monto que va a persistir para esta línea.

    Regla central:
      - Planilla parametrizada (Misiones, Consultores, Gastos Admin) →
        recalculo siempre, ignoro el hint. Si la tarifa no está → CalculoError.
      - Planilla de captura directa (Salarios, Servicios/Licencias, Reuniones) →
        uso el hint del cliente; no hay fórmula contra qué validar.

    Cuantizo todo a 2 decimales — más precisión no aporta y los reportes la
    pierden igual al redondear.
    """
    # Normalizo el hint primero. Negativos los piso a cero — el front
    # debería bloquearlo pero un POST artesanal podría meter -1.
    hint = _d(monto_hint).quantize(Decimal("0.01"))
    if hint < 0:
        hint = Decimal(0)

    # ─── Misiones (servicio y consultores comparten lógica, distinto plan de cuentas) ───
    # Servicio       5.4.1.01 pasaje · 5.4.1.02 viático · 5.4.1.03 hosp · 5.4.1.04 perdiem
    # Consultores    5.3.1.02 pasaje · 5.3.1.03 viático · 5.3.1.04 hosp · 5.3.1.05 perdiem
    if planilla_codigo in ("PL-MISIONES-SERV", "PL-MISIONES-CONS"):
        cant = _d(parametros.get("cant_viajes"))
        dias = _d(parametros.get("duracion_dias"))
        ruta = str(parametros.get("destino") or "")
        mes = _mes_de_fecha(parametros.get("fecha_estimada"))

        # Resolvemos pasaje y, de paso, el país destino — lo usamos abajo para viático.
        pasaje, pais_destino = await _lookup_pasaje(db, ruta, mes)

        # Algunos registros vienen con códigos cortos ("BOL-ARG", "ARG") en vez
        # del formato "CIUDAD → CIUDAD". Si la tabla no devolvió país,
        # deducimos del último token con un mapa simple ISO→país.
        if pais_destino is None:
            mapa_codigo_pais = {"ARG": "ARGENTINA", "BOL": "BOLIVIA", "BRA": "BRASIL",
                                 "PAR": "PARAGUAY", "URU": "URUGUAY"}
            partes = ruta.split("-") if "-" in ruta else [ruta]
            ult = partes[-1].upper() if partes else ""
            pais_destino = mapa_codigo_pais.get(ult, ult)

        # Pasajes: cant_viajes × tarifa
        if cuenta_codigo in ("5.4.1.01", "5.3.1.02"):
            if pasaje is None:
                raise CalculoError(
                    f"No hay tarifa de pasaje para la ruta '{ruta}'. Cargá la ruta "
                    f"en catalogo.tarifa_pasaje_ruta antes de imputar esta línea "
                    f"parametrizada — el backend no acepta el monto del cliente."
                )
            return (cant * pasaje).quantize(Decimal("0.01"))

        # Viáticos: cant_viajes × días × viático/país (desde catalogo.parametro_destino).
        if cuenta_codigo in ("5.4.1.02", "5.3.1.03"):
            viatico = await _lookup_parametro_destino(db, pais_destino or "", "viatico")
            return (cant * dias * viatico).quantize(Decimal("0.01"))

        # Hospedaje: cant_viajes × días × noche.
        # La ciudad destino sale del nombre de la ruta ("X → Y" → "Y"). Si la
        # ruta no usa esa convención, no podemos resolver la ciudad y la línea
        # termina rechazada por falta de tarifa.
        if cuenta_codigo in ("5.4.1.03", "5.3.1.04"):
            ciudad_dest = ""
            if "→" in ruta:
                ciudad_dest = ruta.split("→")[-1].strip()
            hosp = await _lookup_hospedaje(db, ciudad_dest, mes)
            if hosp is None:
                raise CalculoError(
                    f"No hay tarifa de hospedaje para la ciudad destino de la ruta "
                    f"'{ruta}'. Cargá la ciudad en catalogo.tarifa_hospedaje_ciudad "
                    f"antes de imputar esta línea parametrizada — el backend no "
                    f"acepta el monto del cliente."
                )
            return (cant * dias * hosp).quantize(Decimal("0.01"))

        # Per-diem: cant_viajes × días × per-diem/país (desde catalogo.parametro_destino).
        if cuenta_codigo in ("5.4.1.04", "5.3.1.05"):
            perdiem = await _lookup_parametro_destino(db, pais_destino or "", "perdiem")
            return (cant * dias * perdiem).quantize(Decimal("0.01"))

    # ─── Honorarios consultores (5.3.1.01) ───────────────────────────────────
    # Fórmula directa: cantidad de consultores × monto mensual × meses contratados.
    # Las "subscripciones" (pasaje/hosp/viático asociados) van por la planilla
    # PL-MISIONES-CONS arriba, no acá.
    if planilla_codigo == "PL-CONSULTORES" and cuenta_codigo == "5.3.1.01":
        cantidad = _d(parametros.get("cantidad"))
        mm = _d(parametros.get("monto_mensual"))
        meses = _d(parametros.get("meses"))
        return (cantidad * mm * meses).quantize(Decimal("0.01"))

    # ─── Gastos de Administración ────────────────────────────────────────────
    # Dos modos: monto fijo (lo más usado) o presupuesto anterior + % incremento.
    # Si vienen los dos, prevalece el monto fijo (más reciente, decisión manual).
    if planilla_codigo == "PL-GASTOS-ADMIN":
        monto_fijo = _d(parametros.get("monto_fijo"))
        if monto_fijo > 0:
            return monto_fijo.quantize(Decimal("0.01"))
        ant = _d(parametros.get("presupuesto_anterior"))
        pct = _d(parametros.get("pct_incremento"))
        return (ant * (Decimal("1") + pct / Decimal("100"))).quantize(Decimal("0.01"))

    # Captura directa (Salarios y Beneficios, Servicios y Licencias, Reuniones):
    # no hay fórmula contra qué validar, el monto lo declara quien carga.
    # Igual pasamos el valor por _d() y quantize() para no guardar basura.
    return hint
