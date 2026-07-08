"""Motor de cálculo de rappel — Anexo II del contrato.

Dos componentes independientes:
  1. Rappel inicial (300-1.200 €): proporcional a la producción de nuevas
     altas del mes frente al objetivo del tramo de antigüedad.
  2. Rappel variable por mix de producto (100/200/300 €): requiere cumplir
     LAS 4 columnas de un nivel simultáneamente (confirmado por el agente).

IMPORTANTE — fórmula del rappel inicial calibrada solo parcialmente:
en los extremos (min/max) cuadra con los datos reales, pero en un tramo
intermedio (febrero 2026: producción 1.565,56€ -> rappel real 1.041,20€,
mientras la fórmula simple predice ~563€) no cuadra. La hipótesis de trabajo
es que la "producción" del rappel cuenta TODOS los ramos (incluida ASISA
Vida), tal como dice el contrato ("...en todos los ramos"), pero no está
confirmado con datos. Por eso el resultado de esta función siempre viene
con un nivel de "confianza" explícito — no lo mostramos como un hecho.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from engine.config_contrato import ContratoConfig


def meses_desde_inicio(fecha: date, inicio_contrato: date) -> int:
    """Nº de mes de contrato (mes 1 = mes de inicio) al que pertenece `fecha`."""
    return (fecha.year - inicio_contrato.year) * 12 + (fecha.month - inicio_contrato.month) + 1


@dataclass
class ResultadoRappelInicial:
    importe: float
    objetivo_mes: float
    produccion_mes: float
    porcentaje_objetivo: float
    es_mes_formacion: bool
    confianza: str  # "alta" (tope min/max) | "media" (tramo intermedio, sin calibrar)
    nota: str = ""


def calcular_rappel_inicial(
    contrato: ContratoConfig,
    fecha_referencia: date,
    produccion_mes_salud: float,
    produccion_mes_vida: float = 0.0,
) -> ResultadoRappelInicial:
    """Calcula el rappel inicial estimado de un mes.

    `produccion_mes_salud` y `produccion_mes_vida` son la suma de prima
    anualizada de nuevas altas del mes (todos los ramos, según el contrato).
    """
    inicio = contrato.inicio_contrato
    mes_n = meses_desde_inicio(fecha_referencia, inicio)
    rappel_cfg = contrato.rappel_inicial

    if mes_n <= contrato.vigencia["meses_formacion"]:
        return ResultadoRappelInicial(
            importe=rappel_cfg.base_100pct,
            objetivo_mes=0.0,
            produccion_mes=produccion_mes_salud + produccion_mes_vida,
            porcentaje_objetivo=0.0,
            es_mes_formacion=True,
            confianza="alta",
            nota="Mes de formación: rappel plano sin exigir objetivo.",
        )

    tramo = next(
        (t for t in rappel_cfg.tramos if t.desde_mes <= mes_n <= t.hasta_mes),
        rappel_cfg.tramos[-1],  # si supera el último tramo, se queda en el último
    )
    objetivo = tramo.objetivo_prima_anualizada_mes
    produccion_total = produccion_mes_salud + produccion_mes_vida

    importe_bruto = rappel_cfg.base_100pct * (produccion_total / objetivo)
    importe = max(rappel_cfg.minimo, min(rappel_cfg.maximo, importe_bruto))
    porcentaje = produccion_total / objetivo if objetivo else 0.0

    en_extremo = importe in (rappel_cfg.minimo, rappel_cfg.maximo)
    confianza = "alta" if en_extremo else "media"
    nota = (
        "Estimación en el tope (min/max), validada contra datos reales."
        if en_extremo
        else (
            "Estimación en tramo intermedio — la fórmula no está 100% calibrada "
            "en esta zona todavía (ver docstring del módulo). Verificar contra "
            "la Liquidación real del mes en cuanto llegue."
        )
    )

    return ResultadoRappelInicial(
        importe=round(importe, 2),
        objetivo_mes=objetivo,
        produccion_mes=produccion_total,
        porcentaje_objetivo=round(porcentaje * 100, 1),
        es_mes_formacion=False,
        confianza=confianza,
        nota=nota,
    )


@dataclass
class ResultadoRappelMix:
    nivel_alcanzado: int
    importe: float
    conteos: dict[str, int]
    nota: str = ""


def calcular_rappel_mix(contrato: ContratoConfig, conteos_altas: dict[str, int]) -> ResultadoRappelMix:
    """conteos_altas debe traer las claves: salud, dental, accidentes_hospitalizacion, decesos."""
    niveles = sorted(contrato.rappel_niveles_mix, key=lambda n: n.nivel, reverse=True)
    for nivel in niveles:
        cumple = (
            conteos_altas.get("salud", 0) >= nivel.salud
            and conteos_altas.get("dental", 0) >= nivel.dental
            and conteos_altas.get("accidentes_hospitalizacion", 0) >= nivel.accidentes_hospitalizacion
            and conteos_altas.get("decesos", 0) >= nivel.decesos
        )
        if cumple:
            return ResultadoRappelMix(
                nivel_alcanzado=nivel.nivel, importe=nivel.importe, conteos=conteos_altas
            )
    return ResultadoRappelMix(
        nivel_alcanzado=0,
        importe=0.0,
        conteos=conteos_altas,
        nota="No se alcanza ningún nivel de mix este mes.",
    )
