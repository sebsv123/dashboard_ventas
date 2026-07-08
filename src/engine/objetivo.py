"""Objetivo anual de producción — acumulado del año natural en curso.

Suma los 3 tipos de nueva producción (salud mensual anualizada, salud
prepago anual íntegra, vida anualizada), mes a mes, usando SIEMPRE
periodo_liquidacion como fuente de verdad del periodo real (vía
`engine.insights.primeras_altas_por_periodo`) — nunca el mes calendario
de fecha_efecto (ver esa función para el porqué).

Cada mes se marca `completo=False` cuando falta Facturación de ese
periodo (no hay ningún recibo con ese periodo_liquidacion) o cuando hay
altas de Facturación que no cruzan con ninguna póliza (falta subir/
actualizar Pólizas) — así un mes sin datos nunca se confunde con un mes
de producción 0€ real.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from engine.config_contrato import ContratoConfig
from engine.insights import primeras_altas_por_periodo


@dataclass
class DesgloseMesObjetivo:
    periodo: str
    completo: bool
    salud_mensual: float = 0.0
    salud_anual: float = 0.0
    vida: float = 0.0

    @property
    def total(self) -> float:
        return round(self.salud_mensual + self.salud_anual + self.vida, 2)


@dataclass
class ObjetivoAnual:
    anio: str
    objetivo: float
    meses: list[DesgloseMesObjetivo] = field(default_factory=list)

    @property
    def produccion_total(self) -> float:
        return round(sum(m.total for m in self.meses), 2)

    @property
    def porcentaje(self) -> float:
        if not self.objetivo:
            return 0.0
        return round(self.produccion_total / self.objetivo * 100, 1)

    @property
    def meses_incompletos(self) -> list[str]:
        return [m.periodo for m in self.meses if not m.completo]


def _desglose_mes(
    df_polizas: pd.DataFrame, df_facturacion: pd.DataFrame, contrato: ContratoConfig, periodo: str
) -> DesgloseMesObjetivo:
    hay_facturacion_del_periodo = bool(
        not df_facturacion.empty and (df_facturacion["periodo_liquidacion"] == periodo).any()
    )

    altas = primeras_altas_por_periodo(df_facturacion)
    altas = altas[altas["periodo_liquidacion"] == periodo]

    if altas.empty or df_polizas.empty:
        return DesgloseMesObjetivo(periodo, completo=hay_facturacion_del_periodo)

    fusion = altas.merge(
        df_polizas[["poliza", "forma_pago", "razon_social"]], on="poliza", how="left"
    )
    faltan_polizas = bool(fusion["forma_pago"].isna().any())

    desglose = DesgloseMesObjetivo(
        periodo, completo=hay_facturacion_del_periodo and not faltan_polizas
    )
    for _, fila in fusion.dropna(subset=["forma_pago"]).iterrows():
        prima = fila["prima_neta"] if fila["forma_pago"] == "A" else fila["prima_neta"] * 12
        if fila["razon_social"] in contrato.comisiones_vida:
            desglose.vida += prima
        elif fila["forma_pago"] == "A":
            desglose.salud_anual += prima
        else:
            desglose.salud_mensual += prima

    desglose.salud_mensual = round(desglose.salud_mensual, 2)
    desglose.salud_anual = round(desglose.salud_anual, 2)
    desglose.vida = round(desglose.vida, 2)
    return desglose


def calcular_objetivo_anual(
    df_polizas: pd.DataFrame,
    df_facturacion: pd.DataFrame,
    contrato: ContratoConfig,
    anio: int,
    mes_hasta: int,
    objetivo: float | None = None,
) -> ObjetivoAnual:
    """Acumulado de producción del año `anio`, de enero a `mes_hasta` incluido.

    `mes_hasta` normalmente es el mes actual (no tiene sentido pedir meses
    futuros, que por definición no tendrán datos). `objetivo` por defecto
    viene de `contrato.objetivo_produccion_anual` (config/contrato.yaml),
    pero se puede pasar otro para permitir cambiarlo desde la UI sin tocar
    el YAML.
    """
    objetivo_final = objetivo if objetivo is not None else contrato.objetivo_produccion_anual
    meses = [
        _desglose_mes(df_polizas, df_facturacion, contrato, f"{anio:04d}-{mes:02d}")
        for mes in range(1, mes_hasta + 1)
    ]
    return ObjetivoAnual(anio=str(anio), objetivo=objetivo_final, meses=meses)
