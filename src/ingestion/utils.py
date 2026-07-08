"""Utilidades compartidas por todos los parsers de ficheros de ASISA.

Los ficheros CSV que exporta el portal de ASISA tienen particularidades que
hay que tratar siempre igual:
  - Encoding latin-1 (no UTF-8).
  - Separador ';'.
  - Decimales con coma española ("1.234,56").
  - Fechas en formato dd/mm/aaaa.
  - Fecha "01/01/1900" se usa como valor nulo (p.ej. FECHA BAJA sin baja).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd

ENCODING = "latin-1"
SEPARATOR = ";"
FECHA_NULA = date(1900, 1, 1)


def leer_csv_asisa(path: str | Path) -> pd.DataFrame:
    """Lee un CSV exportado del portal de ASISA con el encoding/separador correctos."""
    return pd.read_csv(path, sep=SEPARATOR, encoding=ENCODING, dtype=str).fillna("")


def parsear_decimal_es(valor: str | float | int | None) -> float:
    """Convierte '1.234,56' o '-101,72' (formato español) a float.

    Ya viene como float si pandas lo infirió; se deja pasar tal cual.
    """
    if valor is None or valor == "":
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip()
    if texto == "":
        return 0.0
    texto = texto.replace(".", "").replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return 0.0


def parsear_fecha_es(valor: str | None) -> date | None:
    """Convierte 'dd/mm/aaaa' a date. Devuelve None si está vacío o es la fecha nula 01/01/1900."""
    if not valor or not str(valor).strip():
        return None
    try:
        fecha = datetime.strptime(str(valor).strip(), "%d/%m/%Y").date()
    except ValueError:
        return None
    if fecha == FECHA_NULA:
        return None
    return fecha


def periodo_a_texto(fecha: date) -> str:
    """Formatea una fecha como 'AAAA-MM', igual que la columna PER. LIQUIDACION de ASISA."""
    return f"{fecha.year:04d}-{fecha.month:02d}"
