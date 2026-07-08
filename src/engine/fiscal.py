"""Retenciones IRPF ya practicadas por ASISA — dato real, no una proyección.

Sirve para que Sebastián pueda anticipar su declaración de la renta del año
siguiente con una cifra consultable de lo que YA le han retenido. Este
módulo NO calcula IRPF a pagar ni hace ninguna proyección fiscal — se
limita a sumar/desglosar la columna `irpf` que ya trae cada Factura PDF
confirmada. Cualquier cálculo de cuota, deducciones o estimación de la
declaración está fuera de alcance a propósito.

Nota sobre el signo: en `factura_pdf.irpf` el importe se guarda en
NEGATIVO (es una resta sobre `base_factura` para llegar a
`total_factura`: `base_factura + irpf = total_factura`). Aquí se invierte
el signo para presentar "cuánto se ha retenido" como una cantidad
positiva, que es como tiene sentido leerlo de cara a la renta.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

COLUMNAS_DESGLOSE_MENSUAL = ["periodo", "base_factura", "irpf_retenido", "total_factura"]
COLUMNAS_DESGLOSE_ENTIDAD = ["entidad_nombre", "base_factura", "irpf_retenido", "total_factura"]


@dataclass
class RetencionAnual:
    anio: str
    total_retenido: float
    total_base: float
    total_factura_neto: float
    desglose_mensual: pd.DataFrame
    desglose_entidad: pd.DataFrame


def anios_disponibles(df_factura_pdf: pd.DataFrame) -> list[str]:
    """Años (como texto "AAAA") con al menos una Factura PDF, más reciente primero."""
    if df_factura_pdf.empty:
        return []
    anios = {p.split("-")[0] for p in df_factura_pdf["periodo"].dropna().unique()}
    return sorted(anios, reverse=True)


def _vacio() -> tuple[pd.DataFrame, pd.DataFrame]:
    return (
        pd.DataFrame(columns=COLUMNAS_DESGLOSE_MENSUAL),
        pd.DataFrame(columns=COLUMNAS_DESGLOSE_ENTIDAD),
    )


def calcular_retenciones_anio(df_factura_pdf: pd.DataFrame, anio: str) -> RetencionAnual:
    """Retenciones IRPF del año dado, sumando TODOS los meses y AMBAS entidades.

    `anio` es el año fiscal como texto ("2026"). Si no hay ninguna Factura
    PDF de ese año todavía, devuelve totales en 0 y desgloses vacíos (no es
    un error: simplemente no hay nada que mostrar aún).
    """
    if df_factura_pdf.empty:
        vacio_mensual, vacio_entidad = _vacio()
        return RetencionAnual(anio, 0.0, 0.0, 0.0, vacio_mensual, vacio_entidad)

    df_anio = df_factura_pdf[df_factura_pdf["periodo"].str.startswith(f"{anio}-")].copy()
    if df_anio.empty:
        vacio_mensual, vacio_entidad = _vacio()
        return RetencionAnual(anio, 0.0, 0.0, 0.0, vacio_mensual, vacio_entidad)

    df_anio["irpf_retenido"] = -df_anio["irpf"]

    total_retenido = round(float(df_anio["irpf_retenido"].sum()), 2)
    total_base = round(float(df_anio["base_factura"].sum()), 2)
    total_factura_neto = round(float(df_anio["total_factura"].sum()), 2)

    desglose_mensual = (
        df_anio.groupby("periodo")[["base_factura", "irpf_retenido", "total_factura"]]
        .sum()
        .round(2)
        .reset_index()
        .sort_values("periodo")
    )
    desglose_entidad = (
        df_anio.groupby("entidad_nombre")[["base_factura", "irpf_retenido", "total_factura"]]
        .sum()
        .round(2)
        .reset_index()
    )

    return RetencionAnual(
        anio, total_retenido, total_base, total_factura_neto, desglose_mensual, desglose_entidad
    )
