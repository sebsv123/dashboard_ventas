"""Motor de reconciliación y alertas.

Esta es la pieza que responde a la necesidad real de Sebastián: "detectar
cuándo una póliza que se supone que se debió cobrar no se ha cobrado".

Regla: si una póliza activa tiene fecha de efecto dentro de la ventana ya
observable (es decir, tenemos ficheros de Facturación/Liquidación de ese
mes) y han pasado más de `dias_margen_antes_de_alertar` días desde su efecto
sin aparecer en absoluto en Liquidación, se marca como alerta.

Importante: nunca se afirma "ASISA te debe dinero" como hecho — se marca
como "pendiente de revisar", y el propio Sebastián decide, caso a caso, si
es un error suyo (p.ej. dato mal registrado) o de ASISA. Ese veredicto se
guarda (`resolucion_nota`) para ir construyendo criterio con el tiempo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from engine.config_contrato import ContratoConfig


@dataclass
class AlertaPolizaSinCobrar:
    poliza: str
    nombre_tomador: str
    razon_social: str
    fecha_efecto: date
    dias_desde_efecto: int
    nota: str


def detectar_polizas_sin_cobrar(
    df_polizas: pd.DataFrame,
    df_liquidacion: pd.DataFrame,
    contrato: ContratoConfig,
    fecha_hoy: date,
    primer_mes_con_datos: date,
) -> list[AlertaPolizaSinCobrar]:
    """Devuelve pólizas activas cuya fecha de efecto ya debería haberse
    liquidado (dentro de la ventana de datos que tenemos) y no aparecen.

    `primer_mes_con_datos`: el primer día del mes más antiguo del que
    tenemos Facturación/Liquidación real. Es CRÍTICO no evaluar pólizas
    cuya fecha de efecto caiga antes de esa fecha, porque no tenemos forma
    de confirmar si se cobraron o no (ver caso real 63948141: parecía sin
    cobrar hasta que apareció el fichero de marzo que faltaba).
    """
    polizas_liquidadas = set(df_liquidacion["poliza"].unique())
    margen = timedelta(days=contrato.dias_margen_alerta)

    alertas: list[AlertaPolizaSinCobrar] = []
    for _, poliza in df_polizas.iterrows():
        if poliza["situacion"] != "A":
            continue
        fecha_efecto = poliza["fecha_efecto"]
        if pd.isna(fecha_efecto):
            continue
        # Normaliza: puede venir como pd.Timestamp (leído de SQLite/pandas)
        # o como datetime.date (leído directo del DataFrame de ingesta).
        if hasattr(fecha_efecto, "date"):
            fecha_efecto = fecha_efecto.date()
        if fecha_efecto < primer_mes_con_datos:
            continue  # fuera de la ventana observable — no podemos afirmar nada
        if poliza["poliza"] in polizas_liquidadas:
            continue
        dias_transcurridos = (fecha_hoy - fecha_efecto).days
        if dias_transcurridos < margen.days:
            continue  # aún dentro del margen normal de espera

        alertas.append(
            AlertaPolizaSinCobrar(
                poliza=poliza["poliza"],
                nombre_tomador=poliza["nombre_tomador"],
                razon_social=poliza["razon_social"],
                fecha_efecto=fecha_efecto,
                dias_desde_efecto=dias_transcurridos,
                nota=(
                    f"Póliza activa, efecto hace {dias_transcurridos} días, "
                    f"sin ningún movimiento en Liquidación todavía. Revisar "
                    f"en el portal antes de escalar — puede ser normal si el "
                    f"medio de pago falló o similar."
                ),
            )
        )
    return alertas


@dataclass
class DiferenciaEstimadoVsReal:
    mes: str
    concepto: str
    estimado: float
    real: float
    diferencia: float

    @property
    def diferencia_pct(self) -> float | None:
        if self.real == 0:
            return None
        return round(100 * self.diferencia / self.real, 1)


def comparar_estimado_vs_real(
    estimaciones_mes: dict[str, float], reales_mes: dict[str, float]
) -> list[DiferenciaEstimadoVsReal]:
    """Compara, mes a mes, lo que el motor había estimado contra lo que
    ASISA liquidó realmente. Útil para ir calibrando la fórmula del rappel
    y detectar si el mecanismo de salud mensual se aleja de lo esperado.
    """
    resultado = []
    for mes in sorted(set(estimaciones_mes) | set(reales_mes)):
        est = estimaciones_mes.get(mes, 0.0)
        real = reales_mes.get(mes, 0.0)
        resultado.append(
            DiferenciaEstimadoVsReal(
                mes=mes, concepto="total_mes", estimado=est, real=real, diferencia=round(est - real, 2)
            )
        )
    return resultado
