from pathlib import Path

import pandas as pd
import pytest

from engine.config_contrato import cargar_contrato
from engine.objetivo import calcular_objetivo_anual

CONFIG_PATH = Path(__file__).parent.parent / "config" / "contrato.yaml"


@pytest.fixture
def contrato():
    return cargar_contrato(CONFIG_PATH)


def _df_polizas():
    return pd.DataFrame(
        [
            # Enero: salud mensual + salud anual
            {"poliza": "P1", "forma_pago": "M", "razon_social": "ASISA PARTICULARES"},
            {"poliza": "P2", "forma_pago": "A", "razon_social": "ASISA PARTICULARES"},
            # Febrero: vida
            {"poliza": "P3", "forma_pago": "M", "razon_social": "ASISA VIDA TRANQUILIDAD"},
            # Febrero: otra salud mensual, pero SIN fila en polizas (simula
            # Facturación subida antes que Pólizas para esta póliza)
        ]
    )


def _df_facturacion():
    return pd.DataFrame(
        [
            {"poliza": "P1", "periodo_liquidacion": "2026-01", "prima_neta": 30.0, "fecha_desde": "2026-01-01"},
            {"poliza": "P2", "periodo_liquidacion": "2026-01", "prima_neta": 500.0, "fecha_desde": "2026-01-15"},
            {"poliza": "P3", "periodo_liquidacion": "2026-02", "prima_neta": 12.0, "fecha_desde": "2026-02-01"},
            {"poliza": "P4", "periodo_liquidacion": "2026-02", "prima_neta": 25.0, "fecha_desde": "2026-02-10"},
        ]
    )


def test_calcular_objetivo_anual_mezcla_los_3_tipos_y_suma_bien(contrato):
    resultado = calcular_objetivo_anual(
        _df_polizas(), _df_facturacion(), contrato, anio=2026, mes_hasta=2, objetivo=100000.0
    )
    # Enero: salud mensual 30*12=360, salud anual 500 (sin anualizar) -> 860
    # Febrero: vida 12*12=144, más P4 (25*12=300) que no cruza con Pólizas
    #          (falta_polizas=True para febrero, P4 no suma al total)
    mes_enero = next(m for m in resultado.meses if m.periodo == "2026-01")
    mes_febrero = next(m for m in resultado.meses if m.periodo == "2026-02")

    assert mes_enero.salud_mensual == pytest.approx(360.0)
    assert mes_enero.salud_anual == pytest.approx(500.0)
    assert mes_enero.vida == 0.0
    assert mes_enero.completo is True

    assert mes_febrero.vida == pytest.approx(144.0)
    assert mes_febrero.completo is False  # P4 no tiene fila en Pólizas

    assert resultado.produccion_total == pytest.approx(360.0 + 500.0 + 144.0)
    assert resultado.meses_incompletos == ["2026-02"]


def test_calcular_objetivo_anual_porcentaje_sobre_objetivo(contrato):
    resultado = calcular_objetivo_anual(
        _df_polizas(), _df_facturacion(), contrato, anio=2026, mes_hasta=2, objetivo=1000.0
    )
    # produccion_total = 1004.0 -> 100.4%
    assert resultado.porcentaje == pytest.approx(100.4)


def test_calcular_objetivo_anual_usa_el_objetivo_del_contrato_por_defecto(contrato):
    resultado = calcular_objetivo_anual(
        _df_polizas(), _df_facturacion(), contrato, anio=2026, mes_hasta=1
    )
    assert resultado.objetivo == contrato.objetivo_produccion_anual
    assert resultado.objetivo == 100000.0  # valor actual de config/contrato.yaml


def test_calcular_objetivo_anual_mes_sin_ninguna_facturacion_no_es_cero_silencioso(contrato):
    resultado = calcular_objetivo_anual(
        _df_polizas(), _df_facturacion(), contrato, anio=2026, mes_hasta=3, objetivo=100000.0
    )
    mes_marzo = next(m for m in resultado.meses if m.periodo == "2026-03")
    assert mes_marzo.completo is False
    assert mes_marzo.total == 0.0
    assert "2026-03" in resultado.meses_incompletos


def test_calcular_objetivo_anual_df_vacios_no_revienta(contrato):
    resultado = calcular_objetivo_anual(
        pd.DataFrame(), pd.DataFrame(), contrato, anio=2026, mes_hasta=3, objetivo=50000.0
    )
    assert resultado.produccion_total == 0.0
    assert resultado.meses_incompletos == ["2026-01", "2026-02", "2026-03"]
    assert resultado.porcentaje == 0.0
