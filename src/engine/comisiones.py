"""Motor de clasificación y estimación de comisiones por póliza.

Tres mecánicas distintas, todas validadas contra datos reales de Sebastián
(abril-julio 2026 + reconstrucción enero-marzo):

  1. VIDA (entidad ASISA VIDA): comisión = prima del recibo × % del Anexo I,
     cada recibo cobrado, sin rappel. Confianza: alta.

  2. SALUD ANUAL/PREPAGO (forma_pago='A'): un único recibo anual; comisión
     completa = prima anual × % primer año, en el mes de la fecha de EFECTO
     (columna "FECHA ALTA" del CSV de Pólizas). Confianza: alta.

  3. SALUD MENSUAL (forma_pago='M'): ASISA anticipa la comisión anualizada
     completa en el mes del primer recibo cobrado, y en los 11 meses
     siguientes aparecen movimientos de "regularización" cuyo patrón exacto
     no está cerrado al 100% (ver conversación / README). Para la ESTIMACIÓN
     seguimos el texto literal del contrato (anticipo en el primer recibo);
     los meses posteriores de la misma póliza no deberían generar comisión
     nueva, pero esto se marca siempre como estimación de confianza media,
     nunca como hecho, hasta reconciliar con la Liquidación real.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from engine.config_contrato import ContratoConfig

TIPO_VIDA = "vida"
TIPO_SALUD_ANUAL = "salud_anual"
TIPO_SALUD_MENSUAL = "salud_mensual"


@dataclass
class EstimacionComision:
    poliza: str
    tipo: str
    mes_devengo: str  # "AAAA-MM"
    comision_bruta_estimada: float
    confianza: str  # "alta" | "media"
    nota: str = ""


def clasificar_poliza(fila_poliza: pd.Series, contrato: ContratoConfig) -> str:
    """Determina la mecánica de comisión de una póliza (fila del maestro Pólizas)."""
    if fila_poliza["razon_social"] in contrato.comisiones_vida:
        return TIPO_VIDA
    if fila_poliza["forma_pago"] == "A":
        return TIPO_SALUD_ANUAL
    return TIPO_SALUD_MENSUAL


def _pct_comision_salud(contrato: ContratoConfig, razon_social: str, es_primer_anio: bool) -> float:
    tabla = contrato.comisiones_salud.get(razon_social)
    if tabla is None:
        return 0.0
    bloque = tabla.primer_anio if es_primer_anio else tabla.segundo_anio_en_adelante
    return bloque.produccion


def _meses_transcurridos(fecha_efecto: date, fecha_referencia: date) -> int:
    """Nº de meses completos transcurridos entre fecha_efecto y fecha_referencia."""
    meses = (fecha_referencia.year - fecha_efecto.year) * 12 + (
        fecha_referencia.month - fecha_efecto.month
    )
    if fecha_referencia.day < fecha_efecto.day:
        meses -= 1
    return max(meses, 0)


def _pct_comision_vida(contrato: ContratoConfig, razon_social: str, meses_transcurridos: int) -> float:
    datos = contrato.comisiones_vida.get(razon_social, {})
    if "produccion" in datos:
        # Igual que Salud: a partir del segundo año (12 meses) aplica
        # el % de Mantenimiento, no el de Producción.
        if meses_transcurridos < 12:
            return datos["produccion"]
        return datos.get("mantenimiento", datos["produccion"])
    # Productos con escalado por año (p.ej. AV Accidentes Compromiso 10).
    anio_index = meses_transcurridos // 12
    if anio_index <= 0:
        return datos.get("primer_anio", 0.0)
    if anio_index == 1:
        return datos.get("segundo_anio", datos.get("tercer_anio_en_adelante", 0.0))
    return datos.get("tercer_anio_en_adelante", datos.get("segundo_anio", 0.0))


def estimar_comision_poliza(
    fila_poliza: pd.Series,
    contrato: ContratoConfig,
    prima_anual: float,
    prima_recibo_mensual: float | None = None,
    fecha_referencia: date | None = None,
) -> EstimacionComision:
    """Estima la comisión de una póliza dado su tipo, sin necesidad de Liquidación.

    `fecha_referencia` (por defecto hoy) se usa para determinar si la póliza
    lleva 12 meses o más activa y le toca ya el % de mantenimiento (año 2+)
    en vez del % de producción (primer año).
    """
    tipo = clasificar_poliza(fila_poliza, contrato)
    razon_social = fila_poliza["razon_social"]
    fecha_efecto: date | None = fila_poliza["fecha_efecto"]
    ref = fecha_referencia if fecha_referencia is not None else date.today()
    meses_transcurridos = _meses_transcurridos(fecha_efecto, ref) if fecha_efecto else 0
    es_primer_anio = meses_transcurridos < 12

    if tipo == TIPO_VIDA:
        pct = _pct_comision_vida(contrato, razon_social, meses_transcurridos)
        base = prima_recibo_mensual if prima_recibo_mensual is not None else prima_anual / 12
        return EstimacionComision(
            poliza=fila_poliza["poliza"],
            tipo=tipo,
            mes_devengo=f"{fecha_efecto.year:04d}-{fecha_efecto.month:02d}" if fecha_efecto else "",
            comision_bruta_estimada=round(base * pct, 2),
            confianza="alta",
            nota="Vida: comisión por recibo, sin rappel.",
        )

    if tipo == TIPO_SALUD_ANUAL:
        pct = _pct_comision_salud(contrato, razon_social, es_primer_anio)
        return EstimacionComision(
            poliza=fila_poliza["poliza"],
            tipo=tipo,
            mes_devengo=f"{fecha_efecto.year:04d}-{fecha_efecto.month:02d}" if fecha_efecto else "",
            comision_bruta_estimada=round(prima_anual * pct, 2),
            confianza="alta",
            nota=(
                "Salud prepago anual: comisión íntegra en el mes de efecto."
                if es_primer_anio
                else "Salud prepago anual: póliza en año 2+, % de mantenimiento."
            ),
        )

    # TIPO_SALUD_MENSUAL
    pct = _pct_comision_salud(contrato, razon_social, es_primer_anio)
    return EstimacionComision(
        poliza=fila_poliza["poliza"],
        tipo=tipo,
        mes_devengo=f"{fecha_efecto.year:04d}-{fecha_efecto.month:02d}" if fecha_efecto else "",
        comision_bruta_estimada=round(prima_anual * pct, 2),
        confianza="media",
        nota=(
            "Salud mensual: anticipo estimado en el mes del primer recibo. "
            "Los meses 2-12 de esta misma póliza no deberían sumar comisión "
            "nueva, pero el mecanismo exacto de regularización de ASISA no "
            "está cerrado al 100% — confirmar siempre contra Liquidación real."
        ),
    )


def aplicar_retencion(importe_bruto: float, contrato: ContratoConfig) -> float:
    """Comisión/rappel neto tras aplicar la retención de IRPF configurada."""
    return round(importe_bruto * (1 - contrato.retencion_irpf), 2)
