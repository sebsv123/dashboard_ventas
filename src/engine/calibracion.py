"""Calibración del motor de estimación: estimado vs. real, mes a mes.

Objetivo: para cada periodo donde YA tenemos tanto Facturación+Pólizas
completas como Liquidación/Factura PDF real, recalcula lo que el motor de
ESTIMACIÓN (el mismo que usan las pestañas Rappel y Vista rápida —
`estimar_comision_poliza` + `calcular_rappel_inicial`, ignorando por
completo el dato real) habría predicho, y lo compara contra la Factura PDF
real de ese mismo periodo. Sirve para que Sebastián sepa cuánto fiarse del
número en pantalla mientras el mes está en curso (p.ej. "+/-15% de sesgo").

IMPORTANTE (bug real, ago-2026): `df_polizas`/`df_facturacion` deben ser
las versiones "_con_eiac" (CSV oficial + provisionales de EIAC todavía sin
confirmar) — las MISMAS que usan Vista rápida y Rappel, nunca el CSV
oficial a secas. El objetivo de Calibración es medir la fiabilidad del
número que el agente REALMENTE ve en pantalla, no una versión distinta que
no se muestra en ningún sitio. Pasar el CSV oficial a secas subestimó el
"estimado" de julio 2026 en +1.250€ frente a Vista rápida (faltaba toda la
producción EIAC-only de ese mes) — ver `tests/test_calibracion.py::
test_calibracion_usa_las_mismas_fuentes_de_datos_que_vista_rapida`.

IMPORTANTE: un periodo con Factura PDF real pero SIN Facturación/Pólizas
completas (caso real: marzo 2026, que solo tiene Liquidación) se EXCLUYE
de la comparación con una nota explícita — no es un fallo del motor, es
ausencia de datos de entrada con los que estimar nada.

La cifra "estimado" se calcula igual que en pantalla: suma de
`estimar_comision_poliza` de cada primera alta del periodo (Salud) más
TODOS los recibos Vida del periodo (Vida se devenga en cada recibo
cobrado, no solo en el primero — ver `estimar_comision_y_rappel_periodo`)
más el rappel estimado (`calcular_rappel_inicial`, cuya base Vida depende de
configuración), todo ello neto de retención IRPF (`aplicar_retencion`) para ser
comparable con el "Total factura" real, que ya viene neto de IRPF.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from engine.comisiones import (
    EstadoAnualizacionSalud,
    aplicar_retencion,
    estimar_comision_poliza,
    obtener_estado_anualizacion_salud,
    refinar_confianza_producto_asumido,
)
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
    produccion_vida: float
    comision_salud: float
    comision_vida: float
    comision_bruta: float
    rappel: ResultadoRappelInicial
    total_bruto: float
    total_neto: float
    exclusiones_anualizacion_salud: list[EstadoAnualizacionSalud] = field(default_factory=list)


def estimar_comision_y_rappel_periodo(
    fusion_altas: pd.DataFrame,
    contrato: ContratoConfig,
    periodo: str,
    fusion_recibos_periodo: pd.DataFrame | None = None,
    df_liquidacion: pd.DataFrame | None = None,
) -> EstimacionPeriodo:
    """Estima comisión bruta + rappel + total neto de un periodo.

    `fusion_altas` es el resultado de cruzar las primeras altas del
    periodo (`engine.insights.primeras_altas_por_periodo`, filtrado por
    `periodo_liquidacion`) con Pólizas por número de póliza — debe traer
    ya las columnas de Pólizas (fecha_efecto, forma_pago, razon_social,
    poliza) además de `prima_neta` de Facturación. Si trae también
    `razon_social_asumida` (pólizas provisionales de EIAC con producto
    asumido por defecto, ver `engine.eiac_integracion`), la confianza de
    esa comisión se baja a "baja" — no afecta al importe, solo a cómo se
    presenta la confianza.

    `fusion_recibos_periodo` es distinto: TODOS los recibos de Facturación
    con `periodo_liquidacion == periodo` (sin deduplicar a "primera
    alta"), también fusionados con Pólizas. Hace falta para Vida: a
    diferencia de Salud (que solo genera comisión NUEVA en la primera
    alta, por el mecanismo de anticipo), Vida devenga comisión en CADA
    recibo cobrado, todos los meses — un recibo recurrente de una póliza
    Vida dada de alta hace tiempo no aparece en `fusion_altas` (no es su
    "primera alta"), así que sin este segundo DataFrame esa comisión
    recurrente se queda sin contar (bug real encontrado en julio 2026:
    tres recibos Vida recurrentes, 49,49€ brutos, ausentes del Total NETO
    hasta este fix). Si no se pasa (`None`), la comisión Vida solo
    incluye la primera alta de cada póliza — mismo comportamiento que
    antes de este fix, para no romper llamadas que todavía no lo pasen.

    PÓLIZAS NO ACTIVAS (situacion != "A") SE EXCLUYEN de producción/
    comisión/rappel, aunque su alta o recibo caiga dentro del periodo —
    una póliza anulada no debe seguir sumando al periodo en que se
    vendió. Bug real encontrado en julio 2026: la póliza 64171931 (ASISA
    Travel and You) llegó por EIAC con `ClasePoliza=AN`/
    `SituacionPoliza=EX` (anulada, `FechaAnulacion` real) DESPUÉS de su
    alta original — el motor no filtraba por `situacion` en ningún punto
    de esta cadena, así que si esa póliza hubiera tenido algún recibo
    (no lo tuvo, por eso el caso real no llegó a inflar ningún número en
    pantalla) habría seguido contando. El filtro se hace aquí, sobre
    `fila["situacion"]` si la columna está presente en `fusion_altas`/
    `fusion_recibos_periodo` (viene ya incluida al fusionar con Pólizas
    completo) — si no está presente (llamadas/tests antiguos sin esa
    columna), se cuenta igual que antes, por compatibilidad.
    """
    anio, mes = (int(x) for x in periodo.split("-"))
    fecha_ref = date(anio, mes, 1)

    comision_salud = 0.0
    comision_vida = 0.0
    produccion_salud = 0.0
    produccion_vida = 0.0
    exclusiones_anualizacion_salud: list[EstadoAnualizacionSalud] = []
    for _, fila in fusion_altas.iterrows():
        if fila.get("situacion") not in (None, "A"):
            continue  # anulada/baja: no cuenta como producción de este periodo
        es_vida = fila["razon_social"] in contrato.comisiones_vida
        prima_anual = fila["prima_neta"] if fila["forma_pago"] == "A" else fila["prima_neta"] * 12
        if not es_vida and fila["forma_pago"] != "A" and df_liquidacion is not None:
            estado = obtener_estado_anualizacion_salud(df_liquidacion, fila["poliza"], periodo)
            if estado.anualizacion_vigente:
                exclusiones_anualizacion_salud.append(estado)
                continue
        # Si se pasa fusion_recibos_periodo, Vida se calcula aparte más
        # abajo con TODOS sus recibos del periodo — no sumar aquí también
        # la primera alta o se contaría dos veces.
        if es_vida:
            produccion_vida += prima_anual
        if es_vida and fusion_recibos_periodo is not None:
            continue
        prima_recibo_mensual = fila["prima_neta"] if fila["forma_pago"] != "A" else None
        estimacion = estimar_comision_poliza(
            fila, contrato, prima_anual=prima_anual,
            prima_recibo_mensual=prima_recibo_mensual, fecha_referencia=fecha_ref,
        )
        estimacion = refinar_confianza_producto_asumido(
            estimacion, fila.get("razon_social_asumida") is True
        )
        if es_vida:
            comision_vida += estimacion.comision_bruta_estimada
        else:
            comision_salud += estimacion.comision_bruta_estimada
            produccion_salud += prima_anual

    if fusion_recibos_periodo is not None and not fusion_recibos_periodo.empty:
        recibos_vida = fusion_recibos_periodo[
            fusion_recibos_periodo["razon_social"].isin(contrato.comisiones_vida.keys())
        ]
        for _, fila in recibos_vida.iterrows():
            if fila.get("situacion") not in (None, "A"):
                continue  # anulada/baja: no cuenta como producción de este periodo
            prima_recibo = fila["prima_neta"]
            estimacion_vida = estimar_comision_poliza(
                fila, contrato, prima_anual=prima_recibo * 12,
                prima_recibo_mensual=prima_recibo, fecha_referencia=fecha_ref,
            )
            comision_vida += estimacion_vida.comision_bruta_estimada

    comision_bruta_total = comision_salud + comision_vida
    rappel = calcular_rappel_inicial(
        contrato, fecha_referencia=fecha_ref, produccion_mes_salud=produccion_salud,
        produccion_mes_vida=produccion_vida,
    )
    total_bruto = comision_bruta_total + rappel.importe
    total_neto = aplicar_retencion(total_bruto, contrato)

    return EstimacionPeriodo(
        periodo=periodo,
        produccion_salud=round(produccion_salud, 2),
        produccion_vida=round(produccion_vida, 2),
        comision_salud=round(comision_salud, 2),
        comision_vida=round(comision_vida, 2),
        comision_bruta=round(comision_bruta_total, 2),
        rappel=rappel,
        total_bruto=round(total_bruto, 2),
        total_neto=total_neto,
        exclusiones_anualizacion_salud=exclusiones_anualizacion_salud,
    )


def calcular_calibracion(
    df_polizas: pd.DataFrame,
    df_facturacion: pd.DataFrame,
    df_factura_pdf: pd.DataFrame,
    contrato: ContratoConfig,
    df_liquidacion: pd.DataFrame | None = None,
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

        recibos_periodo = df_facturacion[df_facturacion["periodo_liquidacion"] == periodo]
        fusion_recibos_periodo = recibos_periodo.merge(df_polizas, on="poliza", how="left")

        estimaciones[periodo] = estimar_comision_y_rappel_periodo(
            fusion, contrato, periodo, fusion_recibos_periodo, df_liquidacion
        ).total_neto
        reales[periodo] = real_total

    diferencias = comparar_estimado_vs_real(estimaciones, reales)
    return ResultadoCalibracion(periodos=diferencias, excluidos=excluidos)
