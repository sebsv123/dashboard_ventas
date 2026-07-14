"""Integración del canal EIAC con el motor de rappel/comisiones.

Correspondencia CONFIRMADA por Sebastián con 5 casos reales cruzados a
mano (64276918, 64254004, 64254007, 64261922, 64201679/64174100): la
parte tras el ÚLTIMO guión de `IdPoliza` coincide con el número de póliza
que usan Facturación/Pólizas/Liquidación de ASISA (ver
`ingestion.eiac_xml.extraer_numero_poliza_asisa`).

Con esa correspondencia, este módulo:
  1. Traduce `eiac_recibos` a un DataFrame con la misma forma que
     Facturación (poliza/periodo_liquidacion/prima_neta/fecha_desde), para
     que `engine.insights.primeras_altas_por_periodo` —y todo lo que
     depende de él: Rappel, Vista rápida, Resumen— incluya también las
     ventas que solo están en EIAC, sin esperar al CSV oficial.
  2. Genera filas PROVISIONALES de `polizas` a partir de `eiac_polizas`,
     solo para números de póliza que todavía no existen en la tabla
     oficial — nunca sobreescribe una póliza ya confirmada por el CSV.

APROXIMACIÓN documentada (no confirmada, a diferencia del mapeo de
número de póliza): EIAC no trae el ciclo real de "periodo_liquidacion"
(16→15) que sí calcula ASISA en Facturación/Liquidación — aquí se usa el
mes calendario de `FechaEfectoInicial` como mejor aproximación
disponible. Cuando llegue el CSV de Facturación oficial de ese mes, su
periodo_liquidacion (el correcto, calculado por ASISA) debe prevalecer;
esto NO se resuelve automáticamente en este módulo — ver el aviso de
duplicados potenciales más abajo.

AVISO sobre renumeración: el caso real 64201679/64174100 indica que, al
menos una vez, el número extraído de `IdPoliza` no coincidió 1:1 con el
número que apareció más tarde en el CSV oficial de Pólizas (posible
renovación/cambio de número). La validación de formato de
`extraer_numero_poliza_asisa` NO detecta este caso (el número extraído
es perfectamente válido) — solo una reconciliación posterior contra el
CSV oficial lo revela. Por eso las pólizas provisionales se marcan
siempre con `origen='EIAC'` y `nota_origen`, y nunca sobreescriben una
fila ya oficial (eso lo garantiza `db.carga.cargar_polizas_provisionales_eiac`
con INSERT OR IGNORE, no este módulo).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ingestion.eiac_xml import NumeroPolizaExtraido, extraer_numero_poliza_asisa

# ClaseFormaPago -> forma_pago provisional. PISTA, no un hecho confirmado
# (ver `ingestion.eiac_xml._pista_forma_pago`) — se usa solo como valor de
# arranque hasta que llegue el CSV oficial de Pólizas.
_PISTA_A_FORMA_PAGO = {
    "posible_mensual": "M",
    "posible_prepago_anual": "A",
}

# SituacionPoliza (EIAC) -> situacion (ASISA). Mapeo NO confirmado con
# datos reales todavía (a diferencia de la correspondencia de número de
# póliza) — códigos inferidos de forma razonable; si el código real no
# está aquí, se deja el código EIAC tal cual en vez de forzar un valor.
_SITUACION_EIAC_A_ASISA = {
    "EF": "A",
    "BJ": "B",
}

COLUMNAS_FACTURACION_EIAC = [
    "poliza", "cliente_codigo", "cartera", "producto_nombre",
    "fecha_desde", "fecha_hasta", "prima_neta", "prima_total",
    "periodo_liquidacion", "duracion_recibo_meses",
]

COLUMNAS_POLIZAS_PROVISIONALES = [
    "poliza", "cliente_codigo", "razon_social", "producto_base", "producto_codigo",
    "fecha_emision", "fecha_efecto", "fecha_baja", "forma_pago", "situacion",
    "provincia_tomador", "delegacion", "nombre_tomador", "origen", "nota_origen",
]


@dataclass
class ResultadoIntegracionEiac:
    facturacion_eiac: pd.DataFrame
    polizas_provisionales: pd.DataFrame
    no_reconocidos: list[NumeroPolizaExtraido] = field(default_factory=list)


def _a_fecha(valor):
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    if hasattr(valor, "date"):
        return valor.date() if pd.notna(valor) else None
    return valor


def _extraer_y_registrar(id_poliza_eiac: str, no_reconocidos: list[NumeroPolizaExtraido]) -> str | None:
    resultado = extraer_numero_poliza_asisa(id_poliza_eiac)
    if not resultado.reconocido:
        no_reconocidos.append(resultado)
        return None
    return resultado.numero_poliza


def construir_facturacion_desde_eiac(
    df_eiac_recibos: pd.DataFrame,
) -> tuple[pd.DataFrame, list[NumeroPolizaExtraido]]:
    """Traduce `eiac_recibos` a la misma forma que Facturación.

    Se incluyen recibos "CO" y "PE" por igual: Facturación de ASISA
    tampoco distingue cobro (eso es cosa de Liquidación), así que se
    mantiene el mismo criterio — un recibo "PE" que nunca llegue a
    cobrarse se corregirá cuando llegue la Liquidación real, igual que
    pasaría con cualquier recibo facturado y luego impagado.
    """
    no_reconocidos: list[NumeroPolizaExtraido] = []
    if df_eiac_recibos.empty:
        return pd.DataFrame(columns=COLUMNAS_FACTURACION_EIAC), no_reconocidos

    filas = []
    for _, r in df_eiac_recibos.iterrows():
        numero_poliza = _extraer_y_registrar(r["id_poliza"], no_reconocidos)
        if numero_poliza is None:
            continue
        fecha_efecto = _a_fecha(r["fecha_efecto_inicial"])
        if fecha_efecto is None:
            continue

        prima_neta = r["prima_neta"] if pd.notna(r["prima_neta"]) else r["prima_total"]
        filas.append(
            {
                "poliza": numero_poliza,
                "cliente_codigo": None,
                "cartera": "EIAC",
                "producto_nombre": None,
                # pd.Timestamp, no `date`: df_facturacion (leído con
                # pd.read_sql(parse_dates=...)) usa Timestamp en
                # fecha_desde/fecha_hasta -- mezclar date y Timestamp en la
                # misma columna rompe sort_values al concatenar ambos
                # DataFrames (TypeError: 'values' is not ordered).
                "fecha_desde": pd.Timestamp(fecha_efecto),
                "fecha_hasta": None,
                "prima_neta": prima_neta,
                "prima_total": r["prima_total"],
                "periodo_liquidacion": f"{fecha_efecto.year:04d}-{fecha_efecto.month:02d}",
                "duracion_recibo_meses": None,
            }
        )
    return pd.DataFrame(filas, columns=COLUMNAS_FACTURACION_EIAC), no_reconocidos


def construir_polizas_provisionales_desde_eiac(
    df_eiac_polizas: pd.DataFrame,
    df_eiac_recibos: pd.DataFrame,
    df_polizas_existente: pd.DataFrame,
) -> tuple[pd.DataFrame, list[NumeroPolizaExtraido]]:
    """Genera filas PROVISIONALES de `polizas` a partir de `eiac_polizas`,
    solo para pólizas que todavía no existen en la tabla oficial.
    """
    no_reconocidos: list[NumeroPolizaExtraido] = []
    if df_eiac_polizas.empty:
        return pd.DataFrame(columns=COLUMNAS_POLIZAS_PROVISIONALES), no_reconocidos

    polizas_existentes = (
        set(df_polizas_existente["poliza"]) if not df_polizas_existente.empty else set()
    )

    # pista_forma_pago por póliza: la primera pista no nula vista en sus recibos.
    pista_por_poliza: dict[str, str] = {}
    if not df_eiac_recibos.empty:
        for _, r in df_eiac_recibos.iterrows():
            numero_poliza = _extraer_y_registrar(r["id_poliza"], no_reconocidos)
            if numero_poliza is None:
                continue
            pista = r.get("pista_forma_pago")
            if numero_poliza not in pista_por_poliza and isinstance(pista, str) and pista:
                pista_por_poliza[numero_poliza] = pista

    filas = []
    for _, p in df_eiac_polizas.iterrows():
        numero_poliza = _extraer_y_registrar(p["id_poliza"], no_reconocidos)
        if numero_poliza is None:
            continue
        if numero_poliza in polizas_existentes:
            continue  # ya confirmada por el CSV oficial: no crear provisional

        situacion_eiac = p.get("situacion_poliza")
        situacion = _SITUACION_EIAC_A_ASISA.get(situacion_eiac, situacion_eiac)

        pista = pista_por_poliza.get(numero_poliza)
        forma_pago = _PISTA_A_FORMA_PAGO.get(pista)

        nota = (
            f"Origen: EIAC (id_poliza={p['id_poliza']}), pendiente de confirmar "
            "con Pólizas oficial."
        )
        if forma_pago:
            nota += f" forma_pago provisional inferida de pista_forma_pago='{pista}'."
        clase_poliza = p.get("clase_poliza")
        if isinstance(clase_poliza, str) and "VIDA" in clase_poliza.upper():
            nota += (
                " ClasePoliza sugiere Vida — razon_social se deja sin confirmar "
                "a propósito; no asumas Salud para el cálculo de rappel sin revisar."
            )

        filas.append(
            {
                "poliza": numero_poliza,
                "cliente_codigo": p.get("cliente_codigo"),
                "razon_social": None,
                "producto_base": clase_poliza,
                "producto_codigo": None,
                "fecha_emision": _a_fecha(p.get("fecha_emision")),
                "fecha_efecto": _a_fecha(p.get("fecha_efecto_inicial")),
                "fecha_baja": None,
                "forma_pago": forma_pago,
                "situacion": situacion,
                "provincia_tomador": None,
                "delegacion": None,
                "nombre_tomador": p.get("descripcion_riesgo"),
                "origen": "EIAC",
                "nota_origen": nota,
            }
        )
    return pd.DataFrame(filas, columns=COLUMNAS_POLIZAS_PROVISIONALES), no_reconocidos


def integrar_eiac(
    df_eiac_polizas: pd.DataFrame,
    df_eiac_recibos: pd.DataFrame,
    df_polizas_existente: pd.DataFrame,
) -> ResultadoIntegracionEiac:
    """Punto de entrada único: combina la traducción de Recibos y la
    generación de Pólizas provisionales, con la lista de `IdPoliza` que no
    se pudieron reconocer (formato inesperado) de ambas fuentes."""
    facturacion_eiac, no_reconocidos_recibos = construir_facturacion_desde_eiac(df_eiac_recibos)
    polizas_provisionales, no_reconocidos_polizas = construir_polizas_provisionales_desde_eiac(
        df_eiac_polizas, df_eiac_recibos, df_polizas_existente
    )

    vistos: set[str] = set()
    no_reconocidos: list[NumeroPolizaExtraido] = []
    for item in no_reconocidos_recibos + no_reconocidos_polizas:
        if item.id_poliza_eiac in vistos:
            continue
        vistos.add(item.id_poliza_eiac)
        no_reconocidos.append(item)

    return ResultadoIntegracionEiac(
        facturacion_eiac=facturacion_eiac,
        polizas_provisionales=polizas_provisionales,
        no_reconocidos=no_reconocidos,
    )
