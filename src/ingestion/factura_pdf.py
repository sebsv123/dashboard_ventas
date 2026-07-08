"""Parser de las facturas PDF que emite ASISA (una por entidad y mes).

El PDF trae texto embebido (no es un escaneo), así que extraemos con
pdfplumber y parseamos con reglas fijas sobre las etiquetas que ASISA usa
siempre igual: "TOTAL LIQUIDACIÓN", "SUBVENCIONES", "RAPPEL POR CUMPLIMIENTO
DE OBJETIVOS", "IRPF (15,00%)", "TOTAL FACTURA", etc.

Cada PDF puede traer 1 o 2 páginas (una por entidad: ASISA salud / ASISA
Vida), cada una con su propio "Nº factura" y CIF.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

from ingestion.utils import parsear_decimal_es

CIF_ASISA_SALUD = "A08169294"
CIF_ASISA_VIDA = "A87425070"

RE_NUM_FACTURA = re.compile(r"Nº factura:\s*(\S+)\s*/\s*(\d{2}/\d{2}/\d{4})")
RE_CIF = re.compile(r"CIF:\s*([A-Z0-9]+)")
RE_PERIODO = re.compile(r"correspondientes al mes de\s*(\d{1,2})/(\d{4})")

# Líneas de desglose "LIQUIDACIONES" con 3 números al final: pólizas, prima, importe
RE_LINEA_LIQUIDACION = re.compile(
    r"^(ANTICIPO COMISIONES|COMISIÓN DE RECIBOS MEDIADOR COBRADOS|"
    r"REGULARIZACIÓN ANTICIPO COMISIONES)\s+(\d+)\s+([\d.,]+)\s+(-?[\d.,]+)$"
)
RE_RAPPEL = re.compile(r"RAPPEL POR CUMPLIMIENTO DE OBJETIVOS\s+(-?[\d.,]+)")
RE_RAPPEL_FORMACION = re.compile(r"RAPPEL MES\s*\d*\s+(-?[\d.,]+)")

RE_TOTALES = {
    "total_liquidacion": re.compile(r"TOTAL LIQUIDACI[OÓ]N\s+(-?[\d.,]+)"),
    "gratificaciones": re.compile(r"GRATIFICACIONES\s+(-?[\d.,]+)"),
    "subvenciones": re.compile(r"SUBVENCIONES\s+(-?[\d.,]+)"),
    "regularizaciones": re.compile(r"REGULARIZACIONES\s+(-?[\d.,]+)"),
    "comisiones_vida": re.compile(r"COMISIONES VIDA\s+(-?[\d.,]+)"),
    "base_factura": re.compile(r"BASE FACTURA\s+(-?[\d.,]+)"),
    "irpf": re.compile(r"IRPF\s*\(\s*[\d,]+\s*%\s*\)\s+(-?[\d.,]+)"),
    "otros_conceptos": re.compile(r"OTROS CONCEPTOS\s+(-?[\d.,]+)"),
    "saldo_anterior": re.compile(r"SALDO ANTERIOR\s+(-?[\d.,]+)"),
    "total_factura": re.compile(r"TOTAL FACTURA\s+(-?[\d.,]+)"),
}


@dataclass
class LineaLiquidacion:
    concepto: str
    polizas: int
    prima_neta: float
    importe: float


@dataclass
class FacturaEntidad:
    entidad_cif: str
    entidad_nombre: str
    numero_factura: str
    fecha_factura: str
    periodo: str  # "AAAA-MM"
    lineas: list[LineaLiquidacion] = field(default_factory=list)
    rappel: float = 0.0
    totales: dict[str, float] = field(default_factory=dict)

    @property
    def es_salud(self) -> bool:
        return self.entidad_cif == CIF_ASISA_SALUD

    @property
    def total_factura_neto(self) -> float:
        return self.totales.get("total_factura", 0.0)


def _entidad_nombre(cif: str) -> str:
    if cif == CIF_ASISA_SALUD:
        return "ASISA, Asistencia Sanitaria Interprovincial de Seguros, S.A."
    if cif == CIF_ASISA_VIDA:
        return "ASISA VIDA SEGUROS, S.A.U."
    return "Desconocida"


def _parsear_pagina(texto: str) -> FacturaEntidad | None:
    m_factura = RE_NUM_FACTURA.search(texto)
    m_cif = RE_CIF.search(texto)
    m_periodo = RE_PERIODO.search(texto)
    if not (m_factura and m_cif and m_periodo):
        return None

    mes, anio = m_periodo.groups()
    periodo = f"{int(anio):04d}-{int(mes):02d}"

    factura = FacturaEntidad(
        entidad_cif=m_cif.group(1),
        entidad_nombre=_entidad_nombre(m_cif.group(1)),
        numero_factura=m_factura.group(1),
        fecha_factura=m_factura.group(2),
        periodo=periodo,
    )

    for linea_texto in texto.splitlines():
        linea_texto = linea_texto.strip()
        m = RE_LINEA_LIQUIDACION.match(linea_texto)
        if m:
            concepto, polizas, prima, importe = m.groups()
            factura.lineas.append(
                LineaLiquidacion(
                    concepto=concepto,
                    polizas=int(polizas),
                    prima_neta=parsear_decimal_es(prima),
                    importe=parsear_decimal_es(importe),
                )
            )
            continue
        m = RE_RAPPEL.search(linea_texto)
        if m:
            factura.rappel = parsear_decimal_es(m.group(1))
            continue
        m = RE_RAPPEL_FORMACION.search(linea_texto)
        if m:
            factura.rappel = parsear_decimal_es(m.group(1))
            continue

    for clave, patron in RE_TOTALES.items():
        m = patron.search(texto)
        if m:
            factura.totales[clave] = parsear_decimal_es(m.group(1))

    return factura


def parsear_factura_pdf(path: str | Path) -> list[FacturaEntidad]:
    """Devuelve una FacturaEntidad por cada página/entidad del PDF (1 o 2)."""
    facturas: list[FacturaEntidad] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            texto = page.extract_text() or ""
            factura = _parsear_pagina(texto)
            if factura:
                facturas.append(factura)
    if not facturas:
        raise ValueError(f"No se pudo extraer ninguna factura reconocible de {path}")
    return facturas
