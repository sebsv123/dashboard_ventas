"""Parser del fichero Liquidacion_<NIF>_<MM>_<AAAA>.csv

Contiene el detalle línea a línea de cada movimiento de comisión real,
incluyendo anticipos anualizados (salud mensual), producción por recibo
(vida y salud anual/prepago) y regularizaciones/extornos.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ingestion.utils import leer_csv_asisa, parsear_decimal_es, parsear_fecha_es

COLUMNAS_ESPERADAS = {
    "RAZON SOCIAL",
    "POLIZA",
    "FECHA DESDE",
    "FECHA HASTA",
    "PRIMA NETA",
    "SIT. RECIBO",
    "COMISION",
    "COMISION %",
    "IND. COM.",
    "ACCION",
    "PER. LIQUIDACION",
}

# Traducción de los códigos internos ASISA a los conceptos que aparecen
# literalmente en la factura PDF — así el motor y la factura hablan igual.
MAPA_CONCEPTO_FACTURA = {
    ("ANUAL", "ANUALIZADA"): "ANTICIPO COMISIONES",
    ("ANUAL", "EXTORNO ANUALIZADA"): "REGULARIZACION ANTICIPO COMISIONES",
    ("POR RECIBO", "PRODUCCION"): "COMISION DE RECIBOS MEDIADOR COBRADOS",
    ("POR RECIBO", "MANTENIMIENTO"): "COMISION DE RECIBOS MEDIADOR COBRADOS",
}


def parsear_liquidacion(path: str | Path) -> pd.DataFrame:
    """Devuelve un DataFrame normalizado, una fila por línea de liquidación."""
    df = leer_csv_asisa(path)

    faltantes = COLUMNAS_ESPERADAS - set(df.columns)
    if faltantes:
        raise ValueError(
            f"El fichero {path} no tiene las columnas esperadas de Liquidación. "
            f"Faltan: {sorted(faltantes)}. ¿Es realmente un fichero de Liquidación?"
        )

    out = pd.DataFrame(
        {
            "poliza": df["POLIZA"].str.strip(),
            "razon_social": df["RAZON SOCIAL"].str.strip(),
            "fecha_desde": df["FECHA DESDE"].apply(parsear_fecha_es),
            "fecha_hasta": df["FECHA HASTA"].apply(parsear_fecha_es),
            "prima_neta": df["PRIMA NETA"].apply(parsear_decimal_es),
            "situacion_recibo": df["SIT. RECIBO"].str.strip(),
            "comision": df["COMISION"].apply(parsear_decimal_es),
            "comision_pct": df["COMISION %"].apply(parsear_decimal_es),
            "indicador_comision": df["IND. COM."].str.strip(),
            "accion": df["ACCION"].str.strip(),
            "periodo_liquidacion": df["PER. LIQUIDACION"].str.strip(),
        }
    )

    out["concepto_factura"] = out.apply(
        lambda r: MAPA_CONCEPTO_FACTURA.get(
            (r["indicador_comision"], r["accion"]), r["accion"]
        ),
        axis=1,
    )

    # Dinero real: todo lo que NO sea la reversión contable de un anticipo
    # anualizado que aún no se ha "des-anualizado" cuenta como movimiento real.
    # (ver notas del motor de reconciliación para el detalle fino de esto).
    out["es_extorno"] = out["accion"].str.contains("EXTORNO", na=False)

    return out


def total_por_entidad(df_liquidacion: pd.DataFrame) -> pd.Series:
    """Suma la comisión bruta agrupada por entidad (RAZON SOCIAL)."""
    return df_liquidacion.groupby("razon_social")["comision"].sum()
