"""Insights históricos: evolución de producción, rankings y alertas de tarifa.

A diferencia de la mayoría de pestañas del dashboard (que respetan el
selector de periodo), Insights siempre mira TODO el histórico que haya en
la base de datos — igual que la pestaña Alertas. Nada de estadística
avanzada: agregaciones simples y explicables, a propósito (ver
BRIEF_FASE2.md, "fuera de alcance").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from engine.comisiones import fecha_cambio_a_mantenimiento
from engine.config_contrato import ContratoConfig

MESES_MINIMOS_PARA_TENDENCIA = 2

COLUMNAS_PRODUCCION = [
    "poliza", "razon_social", "provincia_tomador", "forma_pago",
    "fecha_efecto", "periodo", "tipo", "prima_anual",
]


def primeras_altas_por_periodo(df_facturacion: pd.DataFrame) -> pd.DataFrame:
    """El primer recibo de Facturación de cada póliza.

    FUENTE DE VERDAD DE "PERIODO" EN TODO EL PROYECTO: el periodo real de
    devengo de una póliza es el ciclo 16→15 que ASISA ya calcula y expone
    en la columna "PER. LIQUIDACION" de Facturación/Liquidación
    (`periodo_liquidacion`) — NUNCA el mes calendario de la fecha de
    efecto de Pólizas. Pólizas es una foto completa de cartera en el
    momento de cada exportación (no tiene noción de periodo); Facturación
    sí. Confirmado con datos reales: dos pólizas con fecha_efecto
    30/06/2026 aparecen en Facturación de JULIO con
    periodo_liquidacion="2026-07", porque su ventana de devengo real
    (16-junio a 15-julio) cae en julio, no en junio.

    Cualquier cálculo de "qué pólizas son nuevas altas de un periodo X"
    debe partir de aquí — filtrar Facturación por periodo_liquidacion y
    LUEGO cruzar con Pólizas por número de póliza para obtener
    forma_pago/razon_social/fecha_efecto — nunca al revés (nunca filtrar
    primero Pólizas por el mes calendario de fecha_efecto).

    Solo se cuenta el PRIMER recibo de cada póliza: las de salud mensual
    reaparecen en Facturación cada mes con un recibo nuevo, pero solo el
    primero es la "nueva alta" — los siguientes son la misma póliza
    siendo facturada de nuevo, no producción nueva.
    """
    columnas = ["poliza", "periodo_liquidacion", "prima_neta"]
    if df_facturacion.empty:
        return pd.DataFrame(columns=columnas)
    ordenado = df_facturacion.sort_values(["poliza", "periodo_liquidacion", "fecha_desde"])
    primeras = ordenado.groupby("poliza", as_index=False).first()
    return primeras[columnas]


def construir_produccion_polizas(
    df_polizas: pd.DataFrame, df_facturacion: pd.DataFrame, contrato: ContratoConfig
) -> pd.DataFrame:
    """Una fila por póliza con su periodo real de alta, tipo y prima anualizada.

    El periodo ("AAAA-MM") viene de `primeras_altas_por_periodo` — el ciclo
    real de devengo de ASISA vía Facturación, no el mes calendario de
    fecha_efecto (ver el docstring de esa función). Una póliza sin ningún
    recibo en Facturación todavía no puede tener periodo real asignado, así
    que no aparece aquí hasta que se suba su primer recibo.
    """
    if df_polizas.empty or df_facturacion.empty:
        return pd.DataFrame(columns=COLUMNAS_PRODUCCION)

    primeras_altas = primeras_altas_por_periodo(df_facturacion)
    fusion = primeras_altas.merge(
        df_polizas[["poliza", "razon_social", "provincia_tomador", "forma_pago", "fecha_efecto"]],
        on="poliza",
        how="inner",
    )
    if fusion.empty:
        return pd.DataFrame(columns=COLUMNAS_PRODUCCION)

    fusion = fusion.rename(columns={"periodo_liquidacion": "periodo"})
    fusion["tipo"] = fusion["razon_social"].apply(
        lambda r: "vida" if r in contrato.comisiones_vida else "salud"
    )
    fusion["prima_anual"] = fusion.apply(
        lambda fila: fila["prima_neta"] if fila["forma_pago"] == "A" else fila["prima_neta"] * 12,
        axis=1,
    )
    return fusion[COLUMNAS_PRODUCCION]


def siguiente_periodo(periodo: str) -> str:
    """"2026-07" -> "2026-08"; "2026-12" -> "2027-01"."""
    anio, mes = (int(x) for x in periodo.split("-"))
    if mes == 12:
        return f"{anio + 1:04d}-01"
    return f"{anio:04d}-{mes + 1:02d}"


@dataclass
class ResumenPeriodoRapido:
    periodo: str
    tiene_datos: bool  # False si no hay NINGÚN recibo de Facturación de este periodo
    produccion_salud: float
    polizas_detectadas: int


def resumen_produccion_periodo(
    df_polizas: pd.DataFrame, df_facturacion: pd.DataFrame, contrato: ContratoConfig, periodo: str
) -> ResumenPeriodoRapido:
    """Producción de Salud (nuevas altas) ya detectada para un periodo real.

    `tiene_datos=False` distingue "no hay ningún dato todavía de este
    periodo" (no se ha subido Facturación con ese periodo_liquidacion) de
    "hay datos y la producción es 0" (que sería un hecho real, no ausencia
    de datos) — para no repetir la confusión de mostrar un 0€ desnudo
    donde en realidad falta subir el fichero.
    """
    altas = primeras_altas_por_periodo(df_facturacion)
    altas = altas[altas["periodo_liquidacion"] == periodo]
    if altas.empty:
        return ResumenPeriodoRapido(periodo, False, 0.0, 0)

    if df_polizas.empty:
        return ResumenPeriodoRapido(periodo, True, 0.0, 0)

    fusion = altas.merge(
        df_polizas[["poliza", "forma_pago", "razon_social"]], on="poliza", how="inner"
    )
    altas_salud = fusion[~fusion["razon_social"].isin(contrato.comisiones_vida.keys())]
    produccion_salud = 0.0
    for _, fila in altas_salud.iterrows():
        produccion_salud += fila["prima_neta"] if fila["forma_pago"] == "A" else fila["prima_neta"] * 12

    return ResumenPeriodoRapido(periodo, True, round(produccion_salud, 2), len(fusion))


def hay_suficiente_historico(df_produccion: pd.DataFrame) -> bool:
    return df_produccion["periodo"].nunique() >= MESES_MINIMOS_PARA_TENDENCIA


def evolucion_mensual(df_produccion: pd.DataFrame) -> pd.DataFrame:
    """Nº de pólizas nuevas y prima anualizada total, por periodo y tipo."""
    if df_produccion.empty:
        return pd.DataFrame(columns=["periodo", "tipo", "polizas_nuevas", "prima_anual_total"])
    return (
        df_produccion.groupby(["periodo", "tipo"])
        .agg(polizas_nuevas=("poliza", "count"), prima_anual_total=("prima_anual", "sum"))
        .reset_index()
        .sort_values("periodo")
    )


def ranking_productos(df_produccion: pd.DataFrame) -> pd.DataFrame:
    if df_produccion.empty:
        return pd.DataFrame(columns=["razon_social", "polizas", "prima_anual_total"])
    return (
        df_produccion.groupby("razon_social")
        .agg(polizas=("poliza", "count"), prima_anual_total=("prima_anual", "sum"))
        .reset_index()
        .sort_values("prima_anual_total", ascending=False)
    )


def ranking_provincias(df_produccion: pd.DataFrame) -> pd.DataFrame:
    if df_produccion.empty:
        return pd.DataFrame(columns=["provincia_tomador", "polizas", "prima_anual_total"])
    return (
        df_produccion.groupby("provincia_tomador")
        .agg(polizas=("poliza", "count"), prima_anual_total=("prima_anual", "sum"))
        .reset_index()
        .sort_values("prima_anual_total", ascending=False)
    )


@dataclass
class VariacionMensual:
    periodo_actual: str
    periodo_anterior: str
    produccion_actual: float
    produccion_anterior: float
    variacion_pct: float | None
    tendencia: str  # "subida" | "bajada" | "sin_datos"


def variacion_mes_actual_vs_anterior(
    df_produccion: pd.DataFrame, fecha_referencia: date
) -> VariacionMensual:
    """Compara la producción del mes de fecha_referencia contra el mes anterior."""
    periodo_actual = f"{fecha_referencia.year:04d}-{fecha_referencia.month:02d}"
    if fecha_referencia.month == 1:
        anio_ant, mes_ant = fecha_referencia.year - 1, 12
    else:
        anio_ant, mes_ant = fecha_referencia.year, fecha_referencia.month - 1
    periodo_anterior = f"{anio_ant:04d}-{mes_ant:02d}"

    produccion_actual = float(
        df_produccion.loc[df_produccion["periodo"] == periodo_actual, "prima_anual"].sum()
    )
    produccion_anterior = float(
        df_produccion.loc[df_produccion["periodo"] == periodo_anterior, "prima_anual"].sum()
    )

    if produccion_anterior <= 0:
        return VariacionMensual(
            periodo_actual, periodo_anterior, produccion_actual, produccion_anterior,
            variacion_pct=None, tendencia="sin_datos",
        )

    variacion_pct = round((produccion_actual - produccion_anterior) / produccion_anterior * 100, 1)
    tendencia = "subida" if variacion_pct >= 0 else "bajada"
    return VariacionMensual(
        periodo_actual, periodo_anterior, produccion_actual, produccion_anterior,
        variacion_pct, tendencia,
    )


@dataclass
class AlertaCambioTarifa:
    poliza: str
    razon_social: str
    fecha_efecto: date
    dias_para_cambio: int
    nota: str = ""


def alertas_cambio_tarifa(
    df_polizas: pd.DataFrame, contrato: ContratoConfig, fecha_referencia: date
) -> list[AlertaCambioTarifa]:
    """Pólizas de salud mensual a menos de `contrato.dias_antelacion_cambio_tarifa`
    días de pasar a % mantenimiento.

    Información accionable real: saber que la comisión de una póliza va a
    bajar pronto (de % producción a % mantenimiento del año 2 en adelante).
    """
    if df_polizas.empty:
        return []

    candidatas = df_polizas[
        (df_polizas["situacion"] == "A")
        & (df_polizas["forma_pago"] == "M")
        & (~df_polizas["razon_social"].isin(contrato.comisiones_vida.keys()))
    ]

    alertas = []
    for _, fila in candidatas.iterrows():
        fecha_efecto_ts = pd.Timestamp(fila["fecha_efecto"]) if pd.notna(fila["fecha_efecto"]) else None
        if fecha_efecto_ts is None or pd.isna(fecha_efecto_ts):
            continue
        fecha_efecto = fecha_efecto_ts.date()
        # Reutiliza el mismo criterio que engine.comisiones para "cumplir 12
        # meses" — no reinventar la aritmética de fechas con otra lógica que
        # podría no coincidir (p.ej. una cuenta de 365 días fijos diverge en
        # años bisiestos o fechas de efecto a fin de mes).
        aniversario = fecha_cambio_a_mantenimiento(fecha_efecto)
        dias_para_cambio = (aniversario - fecha_referencia).days
        if 0 < dias_para_cambio <= contrato.dias_antelacion_cambio_tarifa:
            alertas.append(
                AlertaCambioTarifa(
                    poliza=fila["poliza"],
                    razon_social=fila["razon_social"],
                    fecha_efecto=fecha_efecto,
                    dias_para_cambio=dias_para_cambio,
                    nota=(
                        f"Cumple 1 año el {aniversario.isoformat()}: pasa de % "
                        "producción a % mantenimiento (estimación, confirmar en Liquidación)."
                    ),
                )
            )
    return sorted(alertas, key=lambda a: a.dias_para_cambio)
