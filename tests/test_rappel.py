from datetime import date
from pathlib import Path

import pytest

from engine.config_contrato import cargar_contrato
from engine.rappel import calcular_rappel_inicial, calcular_rappel_mix, meses_desde_inicio

CONFIG_PATH = Path(__file__).parent.parent / "config" / "contrato.yaml"


@pytest.fixture
def contrato():
    return cargar_contrato(CONFIG_PATH)


def test_meses_desde_inicio():
    inicio = date(2025, 12, 1)
    assert meses_desde_inicio(date(2025, 12, 15), inicio) == 1
    assert meses_desde_inicio(date(2026, 1, 10), inicio) == 2
    assert meses_desde_inicio(date(2026, 4, 1), inicio) == 5


def test_rappel_mes_formacion_da_900_plano(contrato):
    # Validado con la factura real de enero 2026: RAPPEL MES 2 = 900,00€
    resultado = calcular_rappel_inicial(
        contrato, fecha_referencia=date(2026, 1, 15), produccion_mes_salud=0.0
    )
    assert resultado.es_mes_formacion is True
    assert resultado.importe == 900.0
    assert resultado.confianza == "alta"


def test_rappel_produccion_alta_topa_en_1200(contrato):
    # Validado con abril/mayo/junio 2026 reales: producción alta -> tope 1.200€
    resultado = calcular_rappel_inicial(
        contrato, fecha_referencia=date(2026, 4, 15), produccion_mes_salud=6000.0
    )
    assert resultado.importe == 1200.0
    assert resultado.confianza == "alta"


def test_rappel_produccion_cero_topa_en_minimo(contrato):
    resultado = calcular_rappel_inicial(
        contrato, fecha_referencia=date(2026, 4, 15), produccion_mes_salud=0.0
    )
    assert resultado.importe == 300.0


def test_rappel_tramo_intermedio_marca_confianza_media(contrato):
    # A propósito, un valor de producción que NO toca ni el min ni el max:
    # debe marcarse como confianza "media", nunca como hecho confirmado.
    resultado = calcular_rappel_inicial(
        contrato, fecha_referencia=date(2026, 4, 15), produccion_mes_salud=1500.0
    )
    assert 300 < resultado.importe < 1200
    assert resultado.confianza == "media"


def test_rappel_mix_requiere_las_4_columnas_simultaneamente(contrato):
    # Cumple salud y dental de nivel 1 pero NO accidentes/decesos -> nivel 0
    resultado = calcular_rappel_mix(
        contrato,
        conteos_altas={"salud": 6, "dental": 6, "accidentes_hospitalizacion": 0, "decesos": 0},
    )
    assert resultado.nivel_alcanzado == 0
    assert resultado.importe == 0.0


def test_rappel_mix_nivel_1_completo(contrato):
    resultado = calcular_rappel_mix(
        contrato,
        conteos_altas={"salud": 6, "dental": 6, "accidentes_hospitalizacion": 1, "decesos": 2},
    )
    assert resultado.nivel_alcanzado == 1
    assert resultado.importe == 100.0
