from datetime import date
from pathlib import Path

import pytest

from engine.config_contrato import cargar_contrato
from engine.proyeccion import proyectar_cierre_mes

CONFIG_PATH = Path(__file__).parent.parent / "config" / "contrato.yaml"


@pytest.fixture
def contrato():
    return cargar_contrato(CONFIG_PATH)


def test_proyeccion_ritmo_bajo_no_llega_al_objetivo(contrato):
    # 300€ acumulados en 15 de 30 días -> ritmo 20€/día -> proyección 600€
    # (objetivo del tramo es 2500€, así que se queda muy corto)
    proyeccion = proyectar_cierre_mes(
        produccion_acumulada_hasta_hoy=300.0,
        dia_actual_del_mes=15,
        dias_totales_del_mes=30,
        contrato=contrato,
        fecha_referencia=date(2026, 4, 15),
    )
    assert proyeccion.produccion_proyectada == pytest.approx(600.0)
    assert proyeccion.rappel_proyectado.importe == 300.0  # tope mínimo


def test_proyeccion_ritmo_medio_alcanza_justo_el_objetivo(contrato):
    # 1250€ en 15 de 30 días -> ritmo 83,33€/día -> proyección 2500€ (= objetivo)
    proyeccion = proyectar_cierre_mes(
        produccion_acumulada_hasta_hoy=1250.0,
        dia_actual_del_mes=15,
        dias_totales_del_mes=30,
        contrato=contrato,
        fecha_referencia=date(2026, 4, 15),
    )
    assert proyeccion.produccion_proyectada == pytest.approx(2500.0)
    assert proyeccion.rappel_proyectado.importe == pytest.approx(900.0)
    assert proyeccion.rappel_proyectado.confianza == "media"


def test_proyeccion_ritmo_por_encima_del_objetivo_topa_en_maximo(contrato):
    # 2000€ en 10 de 30 días -> ritmo 200€/día -> proyección 6000€ (>> objetivo)
    proyeccion = proyectar_cierre_mes(
        produccion_acumulada_hasta_hoy=2000.0,
        dia_actual_del_mes=10,
        dias_totales_del_mes=30,
        contrato=contrato,
        fecha_referencia=date(2026, 4, 15),
    )
    assert proyeccion.produccion_proyectada == pytest.approx(6000.0)
    assert proyeccion.rappel_proyectado.importe == 1200.0  # tope máximo
    assert "superar" in proyeccion.mensaje.lower()


def test_proyeccion_ultimo_dia_coincide_con_lo_acumulado(contrato):
    proyeccion = proyectar_cierre_mes(
        produccion_acumulada_hasta_hoy=1800.0,
        dia_actual_del_mes=30,
        dias_totales_del_mes=30,
        contrato=contrato,
        fecha_referencia=date(2026, 4, 30),
    )
    assert proyeccion.produccion_proyectada == pytest.approx(1800.0)


def test_proyeccion_dia_actual_invalido_lanza_error(contrato):
    with pytest.raises(ValueError):
        proyectar_cierre_mes(
            produccion_acumulada_hasta_hoy=100.0,
            dia_actual_del_mes=0,
            dias_totales_del_mes=30,
            contrato=contrato,
            fecha_referencia=date(2026, 4, 15),
        )


def test_proyeccion_dia_actual_mayor_que_dias_totales_lanza_error(contrato):
    with pytest.raises(ValueError):
        proyectar_cierre_mes(
            produccion_acumulada_hasta_hoy=100.0,
            dia_actual_del_mes=31,
            dias_totales_del_mes=28,
            contrato=contrato,
            fecha_referencia=date(2026, 4, 15),
        )
