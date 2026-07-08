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
DIAS_ANTELACION_CAMBIO_TARIFA = 60

COLUMNAS_PRODUCCION = [
    "poliza", "razon_social", "provincia_tomador", "forma_pago",
    "fecha_efecto", "periodo", "tipo", "prima_anual",
]


def construir_produccion_polizas(
    df_polizas: pd.DataFrame, df_facturacion: pd.DataFrame, contrato: ContratoConfig
) -> pd.DataFrame:
    """Une pólizas con su recibo más reciente y anualiza la prima.

    Devuelve una fila por póliza con periodo ("AAAA-MM" de fecha_efecto),
    tipo ('vida'|'salud') y prima_anual estimada — la misma aproximación
    que ya usa la pestaña Rappel (prima del recibo más reciente, anualizada
    si la póliza es mensual).
    """
    if df_polizas.empty:
        return pd.DataFrame(columns=COLUMNAS_PRODUCCION)

    df = df_polizas.dropna(subset=["fecha_efecto"]).copy()
    if df.empty:
        return pd.DataFrame(columns=COLUMNAS_PRODUCCION)

    df["periodo"] = pd.to_datetime(df["fecha_efecto"]).dt.strftime("%Y-%m")
    df["tipo"] = df["razon_social"].apply(
        lambda r: "vida" if r in contrato.comisiones_vida else "salud"
    )

    primas = []
    for _, fila in df.iterrows():
        recibos = df_facturacion[df_facturacion["poliza"] == fila["poliza"]]
        if recibos.empty:
            primas.append(0.0)
            continue
        prima = recibos.iloc[-1]["prima_neta"]
        primas.append(prima if fila["forma_pago"] == "A" else prima * 12)
    df["prima_anual"] = primas

    return df[COLUMNAS_PRODUCCION]


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
    """Pólizas de salud mensual a menos de 60 días de pasar a % mantenimiento.

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
        if 0 < dias_para_cambio <= DIAS_ANTELACION_CAMBIO_TARIFA:
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
