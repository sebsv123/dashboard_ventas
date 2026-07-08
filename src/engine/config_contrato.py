"""Carga del config/contrato.yaml con validación de tipos vía pydantic.

Si algún día editas el YAML a mano y te equivocas (p.ej. un porcentaje como
texto en vez de número, o un tramo de rappel sin objetivo), esto falla al
arrancar con un mensaje claro, en vez de calcular algo mal en silencio.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ComisionAnual(BaseModel):
    produccion: float = Field(ge=0, le=1)
    mantenimiento: float = Field(ge=0, le=1)


class ComisionSalud(BaseModel):
    primer_anio: ComisionAnual
    segundo_anio_en_adelante: ComisionAnual


class TramoRappel(BaseModel):
    desde_mes: int
    hasta_mes: int
    objetivo_prima_anualizada_mes: float = Field(gt=0)


class RappelInicial(BaseModel):
    tramos: list[TramoRappel]
    minimo: float
    maximo: float
    base_100pct: float


class NivelMix(BaseModel):
    nivel: int
    importe: float
    salud: int
    dental: int
    accidentes_hospitalizacion: int
    decesos: int


class ContratoConfig(BaseModel):
    vigencia: dict
    fiscal: dict
    entidades: dict
    comisiones_salud: dict[str, ComisionSalud]
    comisiones_vida: dict
    codigos_producto: dict
    rappel: dict
    periodo_devengo: dict
    alertas: dict

    @property
    def retencion_irpf(self) -> float:
        return self.fiscal["retencion_irpf"]

    @property
    def inicio_contrato(self):
        from datetime import date
        return date.fromisoformat(self.vigencia["inicio_contrato"])

    @property
    def rappel_inicial(self) -> RappelInicial:
        return RappelInicial(**self.rappel["inicial"])

    @property
    def rappel_niveles_mix(self) -> list[NivelMix]:
        return [NivelMix(**n) for n in self.rappel["variable_mix"]["niveles"]]

    @property
    def dias_margen_alerta(self) -> int:
        return self.alertas["dias_margen_antes_de_alertar"]

    @property
    def dias_antelacion_cambio_tarifa(self) -> int:
        return self.alertas["dias_antelacion_cambio_tarifa"]


@lru_cache(maxsize=1)
def cargar_contrato(path: str | Path = "config/contrato.yaml") -> ContratoConfig:
    with open(path, encoding="utf-8") as fh:
        datos = yaml.safe_load(fh)
    return ContratoConfig(**datos)
