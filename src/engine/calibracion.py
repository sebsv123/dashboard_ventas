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
from engine.insights import primeras_altas_por_periodo, siguiente_periodo
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


def corregir_periodo_liquidacion_vida_mensual(
    df_facturacion: pd.DataFrame, df_polizas: pd.DataFrame, contrato: ContratoConfig
) -> pd.DataFrame:
    """Corrige `periodo_liquidacion` SOLO para recibos de Vida con ciclo
    mensual conocido (`duracion_recibo_meses` ≈ 1), cuando el valor
    literal del CSV de Facturación no coincide con el que le corresponde
    según su propia `fecha_desde`.

    EXCEPCIÓN PUNTUAL, no una reversión de la regla general — "el CSV
    oficial siempre gana" sigue aplicando sin cambios a Salud (y a Vida
    con otro ciclo, p.ej. anual/prepago: `duracion_recibo_meses` != 1, ver
    caso real póliza 64131545 más abajo). Aquí se hace una excepción
    porque, PARA ESTE CASO CONCRETO, el ciclo es lo bastante predecible
    (una ventana mensual exacta desde `fecha_desde`) como para reconstruir
    el periodo correcto con certeza, sin depender de que llegue la
    Liquidación real.

    Caso real que motivó esto (ago-2026): pólizas 64110228/64110254
    (ASISA VIDA TRANQUILIDAD), ventana fecha_desde=2026-06-15/
    fecha_hasta=2026-07-15 etiquetada `periodo_liquidacion="2026-06"` en
    Facturación, mientras que la Liquidación real de ASISA liquida esa
    MISMA ventana en periodo "2026-07" — confirmado también con la ventana
    anterior de las mismas pólizas (fecha_desde=2026-05-15, periodo real
    "2026-06", que aquí YA coincidía con el literal). El patrón: el
    periodo correcto es el mes siguiente al de `fecha_desde`, no el mismo
    mes — Liquidación real NO discrepa con la regla general de ciclo
    16→15 aplicada a `fecha_efecto` de la póliza (esa regla se mantiene
    intacta en todo el resto del proyecto); aquí se corrige un dato
    puntual de Facturación que resultó estar mal etiquetado en el
    fichero de origen para estas ventanas mensuales de Vida.

    NO aplica a la póliza 64131545 (AV ACCIDENTES SENIOR, Vida mensual->
    anual prepago con `duracion_recibo_meses`=12): ahí el literal
    "2026-06" SÍ coincide con la Liquidación real, así que forzar aquí el
    mismo criterio mensual la habría roto — de ahí el guard explícito por
    duración.
    """
    columnas_necesarias = {"poliza", "fecha_desde", "periodo_liquidacion", "duracion_recibo_meses"}
    if df_facturacion.empty or not columnas_necesarias.issubset(df_facturacion.columns):
        return df_facturacion
    if df_polizas.empty or "razon_social" not in df_polizas.columns:
        return df_facturacion

    razon_social_por_poliza = df_polizas.drop_duplicates(subset="poliza", keep="last").set_index("poliza")["razon_social"]
    razon_social = df_facturacion["poliza"].map(razon_social_por_poliza)

    es_vida = razon_social.isin(contrato.comisiones_vida.keys())
    duracion = pd.to_numeric(df_facturacion["duracion_recibo_meses"], errors="coerce")
    es_ciclo_mensual = duracion.round(0) == 1
    fecha_desde_valida = df_facturacion["fecha_desde"].notna()
    aplica = es_vida & es_ciclo_mensual & fecha_desde_valida
    if not aplica.any():
        return df_facturacion

    def _periodo_mes_siguiente(fecha_desde) -> str:
        mes_calendario = f"{fecha_desde.year:04d}-{fecha_desde.month:02d}"
        return siguiente_periodo(mes_calendario)

    periodo_correcto = df_facturacion.loc[aplica, "fecha_desde"].apply(_periodo_mes_siguiente)
    corregido = df_facturacion.copy()
    corregido.loc[aplica, "periodo_liquidacion"] = periodo_correcto
    return corregido


def _fecha_relevante_produccion(fila: pd.Series):
    """Fecha del propio movimiento: `fecha_desde` del recibo (Facturación)
    si está disponible, si no `fecha_efecto` de la póliza (caso de
    `fusion_altas`, que no trae `fecha_desde` -- ver
    `engine.insights.primeras_altas_por_periodo`, que solo devuelve
    poliza/periodo_liquidacion/prima_neta)."""
    fecha_desde = fila.get("fecha_desde")
    if fecha_desde is not None and not pd.isna(fecha_desde):
        return fecha_desde
    return fila.get("fecha_efecto")


def _excluir_por_anulacion(fila: pd.Series, periodo: str) -> bool:
    """Pólizas no activas (`situacion != "A"`) se excluyen de producción/
    comisión/rappel del periodo de su propia anulación EN ADELANTE, pero
    NO de los periodos anteriores a `fecha_baja` -- la comisión ganada
    antes de anularse fue real y ASISA la sigue pagando (caso real: póliza
    64171931, 24,69€ de un recibo 10/06-19/06/2026, anulada el 19/06/2026).

    El PERIODO que se está estimando (no `fecha_efecto`) es la referencia
    principal: `fecha_efecto` es fija (fecha de alta original) y puede
    quedar MUY por detrás del periodo que se está procesando cuando el
    recibo llega tarde a Facturación -- comparar solo `fecha_efecto` contra
    `fecha_baja` sin mirar el periodo dejaba colar producción de meses
    posteriores a la anulación (bug real: pólizas 63930658/63933124,
    ANULADAS el mismo día de su alta en enero 2026, cuyo único recibo llegó
    a Facturación con `periodo_liquidacion="2026-02"` -- un mes DESPUÉS de
    anuladas; como `fecha_efecto` también era de enero, la comparación
    directa fecha_efecto>fecha_baja daba False y las contaba igual).

    Solo cuando el periodo coincide con el mes de la propia anulación se
    usa la fecha del movimiento concreto (`fecha_desde` del recibo si está,
    si no `fecha_efecto`) para decidir si cayó antes o después dentro de
    ESE mismo mes -- el caso real 64171931 de arriba.

    Sin `fecha_baja` conocida (anulada pero sin fecha, o columna ausente
    por compatibilidad con llamadas/tests antiguos) se mantiene el
    criterio conservador anterior: excluir directamente.
    """
    situacion = fila.get("situacion")
    if situacion in (None, "A"):
        return False
    fecha_baja = fila.get("fecha_baja")
    if fecha_baja is None or pd.isna(fecha_baja):
        return True
    # pd.Timestamp() normaliza tanto Timestamp/date ya parseados como texto
    # ISO crudo (p.ej. eiac_polizas.fecha_anulacion, que no siempre llega
    # parseado como fecha) -- sin esto, un fecha_baja en texto rompía aquí
    # con AttributeError al pedir .year sobre un str.
    fecha_baja = pd.Timestamp(fecha_baja)
    periodo_baja = f"{fecha_baja.year:04d}-{fecha_baja.month:02d}"
    if periodo < periodo_baja:
        return False
    if periodo > periodo_baja:
        return True
    fecha_relevante = _fecha_relevante_produccion(fila)
    if fecha_relevante is None or pd.isna(fecha_relevante):
        return True
    return pd.Timestamp(fecha_relevante) > pd.Timestamp(fecha_baja)


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
    comisión/rappel del periodo de su propia anulación EN ADELANTE, pero
    SIGUEN CONTANDO en los periodos ANTERIORES a `fecha_baja` — una
    póliza anulada no debe seguir sumando producción futura, pero la
    comisión que ya ganó antes de anularse fue real y ASISA la paga igual
    (ver `_excluir_por_anulacion`). Bug real encontrado en julio 2026: la
    póliza 64171931 (ASISA Travel and You) llegó por EIAC con
    `ClasePoliza=AN`/`SituacionPoliza=EX` (anulada, `FechaAnulacion` real)
    DESPUÉS de su alta original — el motor no filtraba por `situacion` en
    ningún punto de esta cadena. El primer fix (ago-2026) excluía la
    póliza entera sin mirar fechas, lo cual escondía sin querer 24,69€ de
    comisión real de un recibo cobrado ANTES de la anulación (10/06 a
    19/06/2026, Liquidación real de junio) — corregido comparando la
    fecha del propio movimiento (`fecha_desde` del recibo si está, si no
    `fecha_efecto`) contra `fecha_baja`.
    """
    anio, mes = (int(x) for x in periodo.split("-"))
    fecha_ref = date(anio, mes, 1)

    comision_salud = 0.0
    comision_vida = 0.0
    produccion_salud = 0.0
    produccion_vida = 0.0
    exclusiones_anualizacion_salud: list[EstadoAnualizacionSalud] = []
    for _, fila in fusion_altas.iterrows():
        if _excluir_por_anulacion(fila, periodo):
            continue  # anulada/baja: excluida desde su periodo de anulación en adelante
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
            if _excluir_por_anulacion(fila, periodo):
                continue  # anulada/baja: excluida desde su periodo de anulación en adelante
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
