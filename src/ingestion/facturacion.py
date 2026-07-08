"""Parser del fichero Facturacion_<NIF>_<MM>_<AAAA>.csv

Contiene un recibo por línea (facturación, no cobro real): una póliza que
tiene facturación mensual reaparece cada mes con un recibo nuevo.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ingestion.utils import leer_csv_asisa, parsear_decimal_es, parsear_fecha_es

COLUMNAS_ESPERADAS = {
    "CLIENTE",
    "CARTERA",
    "OPERACION",
    "POLIZA",
    "NOMBRE CLIENTE",
    "FECHA DESDE",
    "FECHA HASTA",
    "PRIMA NETA",
    "PRIMA TOTAL",
    "PER. LIQUIDACION",
}


def parsear_facturacion(path: str | Path) -> pd.DataFrame:
    """Devuelve un DataFrame normalizado con un recibo facturado por fila."""
    df = leer_csv_asisa(path)

    faltantes = COLUMNAS_ESPERADAS - set(df.columns)
    if faltantes:
        raise ValueError(
            f"El fichero {path} no tiene las columnas esperadas de Facturación. "
            f"Faltan: {sorted(faltantes)}. ¿Es realmente un fichero de Facturación?"
        )

    out = pd.DataFrame(
        {
            "poliza": df["POLIZA"].str.strip(),
            "cliente_codigo": df["CLIENTE"].str.strip(),
            "cartera": df["CARTERA"].str.strip(),
            "producto_nombre": df["NOMBRE CLIENTE"].str.strip(),
            "fecha_desde": df["FECHA DESDE"].apply(parsear_fecha_es),
            "fecha_hasta": df["FECHA HASTA"].apply(parsear_fecha_es),
            "prima_neta": df["PRIMA NETA"].apply(parsear_decimal_es),
            "prima_total": df["PRIMA TOTAL"].apply(parsear_decimal_es),
            "periodo_liquidacion": df["PER. LIQUIDACION"].str.strip(),
        }
    )

    # Duración del recibo en meses (aprox.) — útil para distinguir pago
    # mensual (~1 mes) de pago anual/prepago (~12 meses). Se mantiene como
    # señal secundaria; la señal fiable es FORMA PAGO del fichero de Pólizas.
    def duracion_meses(row) -> float | None:
        if row["fecha_desde"] is None or row["fecha_hasta"] is None:
            return None
        dias = (row["fecha_hasta"] - row["fecha_desde"]).days
        return round(dias / 30.4, 1)

    out["duracion_recibo_meses"] = out.apply(duracion_meses, axis=1)
    return out
