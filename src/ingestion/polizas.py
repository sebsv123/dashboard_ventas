"""Parser del fichero Polizas_<NIF>_<MM>_<AAAA>.csv

Es el maestro de pólizas: trae la forma de pago (M/A), la fecha de EFECTO real
(columna "FECHA ALTA" del CSV — ojo, el nombre de columna es engañoso, NO es
la fecha de emisión), la fecha de grabación/emisión real ("FECHA GRAB"),
situación (A=Activa/B=Baja), provincia, y código de producto.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ingestion.utils import leer_csv_asisa, parsear_fecha_es

COLUMNAS_ESPERADAS = {
    "CLIENTE",
    "RAZON SOCIAL",
    "POLIZA",
    "PRODUCTO BASE",
    "PRODUCTO",
    "FECHA GRAB",
    "FECHA ALTA",
    "FECHA BAJA",
    "FORMA PAGO",
    "SITUACION POLIZA",
    "PROVINCIA TOMADOR",
    "DESCRIPCION",
}


def parsear_polizas(path: str | Path) -> pd.DataFrame:
    """Devuelve un DataFrame normalizado, una fila por póliza/asegurado."""
    df = leer_csv_asisa(path)

    faltantes = COLUMNAS_ESPERADAS - set(df.columns)
    if faltantes:
        raise ValueError(
            f"El fichero {path} no tiene las columnas esperadas de Pólizas. "
            f"Faltan: {sorted(faltantes)}. ¿Es realmente un fichero de Pólizas?"
        )

    out = pd.DataFrame(
        {
            "poliza": df["POLIZA"].str.strip(),
            "cliente_codigo": df["CLIENTE"].str.strip(),
            "razon_social": df["RAZON SOCIAL"].str.strip(),
            "producto_base": df["PRODUCTO BASE"].str.strip(),
            "producto_codigo": df["PRODUCTO"].str.strip(),
            # OJO: "FECHA GRAB" es la emisión real; "FECHA ALTA" es en
            # realidad la fecha de EFECTO (confirmado con el portal de ASISA).
            "fecha_emision": df["FECHA GRAB"].apply(parsear_fecha_es),
            "fecha_efecto": df["FECHA ALTA"].apply(parsear_fecha_es),
            "fecha_baja": df["FECHA BAJA"].apply(parsear_fecha_es),
            "forma_pago": df["FORMA PAGO"].str.strip(),  # 'M' mensual, 'A' anual
            "situacion": df["SITUACION POLIZA"].str.strip(),  # 'A' activa, 'B' baja
            "provincia_tomador": df["PROVINCIA TOMADOR"].str.strip(),
            "delegacion": df["DESCRIPCION"].str.strip(),
            "nombre_tomador": (
                df["NOMBRE TOMADOR"].str.strip()
                + " "
                + df["PRIMER APELLIDO TOMADOR"].str.strip()
                + " "
                + df["SEGUNDO APELLIDO TOMADOR"].str.strip()
            ).str.replace(r"\s+", " ", regex=True).str.strip(),
        }
    )
    return out


def poliza_es_prepago_anual(fila: pd.Series) -> bool:
    """True si la póliza se paga de una vez al año (comisión inmediata al emitirse)."""
    return fila["forma_pago"] == "A"
