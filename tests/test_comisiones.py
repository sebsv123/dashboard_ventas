from pathlib import Path

import pytest

from engine.comisiones import (
    TIPO_SALUD_ANUAL,
    TIPO_SALUD_MENSUAL,
    TIPO_VIDA,
    aplicar_retencion,
    clasificar_poliza,
    estimar_comision_poliza,
)
from engine.config_contrato import cargar_contrato
from ingestion.polizas import parsear_polizas

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG_PATH = Path(__file__).parent.parent / "config" / "contrato.yaml"


@pytest.fixture
def contrato():
    return cargar_contrato(CONFIG_PATH)


@pytest.fixture
def df_polizas():
    return parsear_polizas(FIXTURES / "polizas_sample.csv")


def test_clasificacion_por_tipo(contrato, df_polizas):
    mensual = df_polizas[df_polizas["poliza"] == "70000001"].iloc[0]
    vida = df_polizas[df_polizas["poliza"] == "70000002"].iloc[0]
    anual = df_polizas[df_polizas["poliza"] == "70000003"].iloc[0]

    assert clasificar_poliza(mensual, contrato) == TIPO_SALUD_MENSUAL
    assert clasificar_poliza(vida, contrato) == TIPO_VIDA
    assert clasificar_poliza(anual, contrato) == TIPO_SALUD_ANUAL


def test_estimacion_salud_anual_confianza_alta(contrato, df_polizas):
    anual = df_polizas[df_polizas["poliza"] == "70000003"].iloc[0]
    estimacion = estimar_comision_poliza(anual, contrato, prima_anual=500.0)
    # ASISA PARTICULARES primer año = 25%
    assert estimacion.comision_bruta_estimada == pytest.approx(125.0)
    assert estimacion.confianza == "alta"


def test_estimacion_vida_confianza_alta(contrato, df_polizas):
    vida = df_polizas[df_polizas["poliza"] == "70000002"].iloc[0]
    estimacion = estimar_comision_poliza(vida, contrato, prima_anual=144.0, prima_recibo_mensual=12.0)
    # ASISA VIDA TRANQUILIDAD producción = 60%
    assert estimacion.comision_bruta_estimada == pytest.approx(7.2)
    assert estimacion.confianza == "alta"


def test_estimacion_salud_mensual_confianza_media(contrato, df_polizas):
    mensual = df_polizas[df_polizas["poliza"] == "70000001"].iloc[0]
    estimacion = estimar_comision_poliza(mensual, contrato, prima_anual=360.0)
    assert estimacion.confianza == "media"  # mecanismo no cerrado al 100%


def test_retencion_irpf(contrato):
    assert aplicar_retencion(1000.0, contrato) == 850.0
