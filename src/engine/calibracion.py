"""Calibración del motor de estimación: estimado vs. real, mes a mes.

Objetivo: para cada periodo donde YA tenemos tanto Facturación+Pólizas
completas como Liquidación/Factura PDF real, recalcula lo que el motor de
ESTIMACIÓN (el mismo que usan las pestañas Rappel y Vista rápida —
`estimar_comision_poliza` + `calcular_rappel_inicial`, ignorando por
completo el dato real) habría predicho, y lo compara contra la Factura PDF
real de ese mismo periodo. Sirve para que Sebastián sepa cuánto fiarse del
número en pantalla mientras el mes está en curso (p.ej. "+/-15% de sesgo").

IMPORTANTE: un periodo con Factura PDF real pero SIN Facturación/Pólizas
completas (caso real: marzo 2026, que solo tiene Liquidación) se EXCLUYE
de la comparación con una nota explícita — no es un fallo del motor, es
ausencia de datos de entrada con los que estimar nada.

La cifra "estimado" se calcula igual que en pantalla: suma de
`estimar_comision_poliza` de cada primera alta del periodo (salud + vida)
más el rappel estimado (`calcular_rappel_inicial`, solo producción de
salud), todo ello neto de retención IRPF (`aplicar_retencion`) para ser
comparable con el "Total factura" real, que ya viene neto de IRPF.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from engine.comisiones import aplicar_retencion, estimar_comision_poliza
from engine.config_contrato import ContratoConfig
from engine.insights import primeras_altas_por_periodo
from engine.rappel import ResultadoRappelInicial, calcular_rappel_inicial
from engine.reconciliacion import DiferenciaEstimadoVsReal, comparar_estimado_vs_real


@dataclass
class PeriodoExcluidoCalibracion:
    periodo: str
    motivo: str


@dataclass
class ResultadoCalibracion:
    periodos: list[DiferenciaEstimadoVsReal] = field(default_factory=list)
    excluidos: list[PeriodoExcluidoCalibracion] = field(default_factory=list)

    @property
    def sesgo_medio_eur(self) -> float | None:
        """Diferencia media (estimado - real). Positivo = el motor se pasa
        de media; negativo = el motor se queda corto de media."""
        if not self.periodos:
            return None
        return round(sum(p.diferencia for p in self.periodos) / len(self.periodos), 2)

    @property
    def sesgo_medio_pct(self) -> float | None:
        pcts = [p.diferencia_pct for p in self.periodos if p.diferencia_pct is not None]
        if not pcts:
            return None
        return round(sum(pcts) / len(pcts), 1)


@dataclass
class EstimacionPeriodo:
    """Estimación completa de un periodo: comisión bruta + rappel + neto.

    Es LA función que usan Calibración, Vista rápida y Rappel para "lo que
    el motor estimaría" de un periodo — cualquier cambio en la fórmula
    (qué cuenta como producción, cómo se calcula la comisión o el rappel,
    la retención aplicada) debe pasar por aquí, no duplicarse en cada
    pestaña.
    """

    periodo: str
    produccion_salud: float
    comision_bruta: float
    rappel: ResultadoRappelInicial
    total_bruto: float
    total_neto: float


def estimar_comision_y_rappel_periodo(
    fusion_altas: pd.DataFrame, contrato: ContratoConfig, periodo: str
) -> EstimacionPeriodo:
    """Estima comisión bruta + rappel + total neto de un periodo.

    `fusion_altas` es el resultado de cruzar las primeras altas del
    periodo (`engine.insights.primeras_altas_por_periodo`, filtrado por
    `periodo_liquidacion`) con Pólizas por número de póliza — debe traer
    ya las columnas de Pólizas (fecha_efecto, forma_pago, razon_social,
    poliza) además de `prima_neta` de Facturación.
    """
    anio, mes = (int(x) for x in periodo.split("-"))
    fecha_ref = date(anio, mes, 1)

    comision_bruta_total = 0.0
    produccion_salud = 0.0
    for _, fila in fusion_altas.iterrows():
        prima_anual = fila["prima_neta"] if fila["forma_pago"] == "A" else fila["prima_neta"] * 12
        prima_recibo_mensual = fila["prima_neta"] if fila["forma_pago"] != "A" else None
        estimacion = estimar_comision_poliza(
            fila, contrato, prima_anual=prima_anual,
            prima_recibo_mensual=prima_recibo_mensual, fecha_referencia=fecha_ref,
        )
        comision_bruta_total += estimacion.comision_bruta_estimada
        if fila["razon_social"] not in contrato.comisiones_vida:
            produccion_salud += prima_anual

    rappel = calcular_rappel_inicial(contrato, fecha_referencia=fecha_ref, produccion_mes_salud=produccion_salud)
    total_bruto = comision_bruta_total + rappel.importe
    total_neto = aplicar_retencion(total_bruto, contrato)

    return EstimacionPeriodo(
        periodo=periodo,
        produccion_salud=round(produccion_salud, 2),
        comision_bruta=round(comision_bruta_total, 2),
        rappel=rappel,
        total_bruto=round(total_bruto, 2),
        total_neto=total_neto,
    )


def calcular_calibracion(
    df_polizas: pd.DataFrame,
    df_facturacion: pd.DataFrame,
    df_factura_pdf: pd.DataFrame,
    contrato: ContratoConfig,
) -> ResultadoCalibracion:
    """Compara, periodo a periodo, el estimado del motor contra el real de
    Factura PDF — solo para los periodos donde ambas fuentes están completas.
    """
    if df_factura_pdf.empty:
        return ResultadoCalibracion()

    periodos_reales = sorted(df_factura_pdf["periodo"].dropna().unique())
    estimaciones: dict[str, float] = {}
    reales: dict[str, float] = {}
    excluidos: list[PeriodoExcluidoCalibracion] = []

    for periodo in periodos_reales:
        real_total = round(
            float(df_factura_pdf.loc[df_factura_pdf["periodo"] == periodo, "total_factura"].sum()), 2
        )

        hay_facturacion = not df_facturacion.empty and (df_facturacion["periodo_liquidacion"] == periodo).any()
        if not hay_facturacion:
            excluidos.append(
                PeriodoExcluidoCalibracion(
                    periodo,
                    "Sin Facturación de este periodo todavía — no se puede recalcular el "
                    "estimado. No es un fallo del motor, es ausencia de datos de entrada.",
                )
            )
            continue

        if df_polizas.empty:
            excluidos.append(
                PeriodoExcluidoCalibracion(
                    periodo,
                    "No hay ninguna Póliza cargada — no se puede clasificar ninguna alta "
                    "de este periodo. No es un fallo del motor, es ausencia de datos de entrada.",
                )
            )
            continue

        altas = primeras_altas_por_periodo(df_facturacion)
        altas = altas[altas["periodo_liquidacion"] == periodo]
        fusion = altas.merge(df_polizas, on="poliza", how="left")
        faltan_polizas = bool(fusion["forma_pago"].isna().any())
        if faltan_polizas:
            excluidos.append(
                PeriodoExcluidoCalibracion(
                    periodo,
                    "Hay altas de Facturación de este periodo que no cruzan con ninguna "
                    "póliza — faltan subir/actualizar Pólizas. No es un fallo del motor, "
                    "es ausencia de datos de entrada.",
                )
            )
            continue

        estimaciones[periodo] = estimar_comision_y_rappel_periodo(fusion, contrato, periodo).total_neto
        reales[periodo] = real_total

    diferencias = comparar_estimado_vs_real(estimaciones, reales)
    return ResultadoCalibracion(periodos=diferencias, excluidos=excluidos)
