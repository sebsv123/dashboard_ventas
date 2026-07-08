from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from engine.comisiones import fecha_cambio_a_mantenimiento
from engine.config_contrato import cargar_contrato
from engine.insights import (
    alertas_cambio_tarifa,
    construir_produccion_polizas,
    evolucion_mensual,
    hay_suficiente_historico,
    ranking_productos,
    ranking_provincias,
    variacion_mes_actual_vs_anterior,
)
from ingestion.facturacion import parsear_facturacion
from ingestion.polizas import parsear_polizas

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG_PATH = Path(__file__).parent.parent / "config" / "contrato.yaml"


@pytest.fixture
def contrato():
    return cargar_contrato(CONFIG_PATH)


@pytest.fixture
def df_polizas():
    return parsear_polizas(FIXTURES / "polizas_sample.csv")


@pytest.fixture
def df_facturacion():
    return parsear_facturacion(FIXTURES / "facturacion_sample.csv")


@pytest.fixture
def df_produccion(df_polizas, df_facturacion, contrato):
    return construir_produccion_polizas(df_polizas, df_facturacion, contrato)


def test_construir_produccion_polizas_anualiza_y_clasifica(df_produccion):
    assert set(df_produccion["periodo"]) == {"2026-06"}

    salud = df_produccion[df_produccion["tipo"] == "salud"].set_index("poliza")
    vida = df_produccion[df_produccion["tipo"] == "vida"].set_index("poliza")

    assert salud.loc["70000001", "prima_anual"] == pytest.approx(360.0)  # mensual x12
    assert salud.loc["70000003", "prima_anual"] == pytest.approx(500.0)  # anual, sin x12
    assert vida.loc["70000002", "prima_anual"] == pytest.approx(144.0)  # mensual x12


def test_hay_suficiente_historico_con_un_solo_mes(df_produccion):
    assert hay_suficiente_historico(df_produccion) is False


def test_evolucion_mensual_agrupa_por_periodo_y_tipo(df_produccion):
    agg = evolucion_mensual(df_produccion)
    fila_salud = agg[agg["tipo"] == "salud"].iloc[0]
    fila_vida = agg[agg["tipo"] == "vida"].iloc[0]
    assert fila_salud["polizas_nuevas"] == 2
    assert fila_salud["prima_anual_total"] == pytest.approx(860.0)
    assert fila_vida["polizas_nuevas"] == 1
    assert fila_vida["prima_anual_total"] == pytest.approx(144.0)


def test_ranking_productos_ordena_por_prima_desc(df_produccion):
    ranking = ranking_productos(df_produccion)
    assert ranking.iloc[0]["razon_social"] == "ASISA PARTICULARES"
    assert ranking.iloc[0]["prima_anual_total"] == pytest.approx(860.0)


def test_ranking_provincias_ordena_por_prima_desc(df_produccion):
    ranking = ranking_provincias(df_produccion)
    assert list(ranking["provincia_tomador"])[0] == "Sevilla"


def test_variacion_mes_sin_datos_del_mes_anterior(df_produccion):
    variacion = variacion_mes_actual_vs_anterior(df_produccion, date(2026, 6, 15))
    assert variacion.tendencia == "sin_datos"


def test_variacion_mes_bajada(df_produccion):
    # No hay producción de julio en la fixture -> caída del 100% vs junio
    variacion = variacion_mes_actual_vs_anterior(df_produccion, date(2026, 7, 15))
    assert variacion.tendencia == "bajada"
    assert variacion.variacion_pct == pytest.approx(-100.0)


def test_variacion_mes_subida():
    df = pd.DataFrame({"periodo": ["2026-05", "2026-06"], "prima_anual": [500.0, 800.0]})
    variacion = variacion_mes_actual_vs_anterior(df, date(2026, 6, 15))
    assert variacion.tendencia == "subida"
    assert variacion.variacion_pct == pytest.approx(60.0)


def test_alertas_cambio_tarifa_solo_salud_mensual_activa_proxima_a_cumplir_anio(
    df_polizas, contrato
):
    # 70000001: salud mensual, efecto 2026-06-01 -> a 2027-04-15 le faltan 47 días
    # para cumplir el año (dentro de la ventana de 60 días de aviso).
    alertas = alertas_cambio_tarifa(df_polizas, contrato, date(2027, 4, 15))
    assert len(alertas) == 1
    assert alertas[0].poliza == "70000001"
    assert 0 < alertas[0].dias_para_cambio <= 60


def test_alertas_cambio_tarifa_vacio_fuera_de_ventana(df_polizas, contrato):
    alertas = alertas_cambio_tarifa(df_polizas, contrato, date(2026, 7, 15))
    assert alertas == []


def test_alertas_cambio_tarifa_usa_fecha_cambio_a_mantenimiento_compartida(
    df_polizas, contrato
):
    # La fecha de "cumple 1 año" del aviso debe ser exactamente la misma que
    # calcula engine.comisiones.fecha_cambio_a_mantenimiento — no una cuenta
    # de días independiente que pueda desincronizarse.
    alertas = alertas_cambio_tarifa(df_polizas, contrato, date(2027, 4, 15))
    esperado = fecha_cambio_a_mantenimiento(date(2026, 6, 1))
    assert f"Cumple 1 año el {esperado.isoformat()}" in alertas[0].nota
    assert alertas[0].dias_para_cambio == (esperado - date(2027, 4, 15)).days


def test_alertas_cambio_tarifa_respeta_el_umbral_del_yaml(df_polizas, tmp_path):
    # El umbral de aviso (60 días) debe venir de config/contrato.yaml, no de
    # una constante en el código: si se reduce en el YAML, el motor debe
    # dejar de avisar de una póliza que antes sí entraba en la ventana.
    texto_original = CONFIG_PATH.read_text(encoding="utf-8")
    assert "dias_antelacion_cambio_tarifa: 60" in texto_original
    texto_modificado = texto_original.replace(
        "dias_antelacion_cambio_tarifa: 60", "dias_antelacion_cambio_tarifa: 10"
    )
    config_temporal = tmp_path / "contrato_umbral_bajo.yaml"
    config_temporal.write_text(texto_modificado, encoding="utf-8")

    contrato_umbral_bajo = cargar_contrato(config_temporal)
    assert contrato_umbral_bajo.dias_antelacion_cambio_tarifa == 10

    # 70000001 está a 47 días de cumplir el año: entra con el umbral de 60
    # por defecto, pero no con uno de 10.
    alertas = alertas_cambio_tarifa(df_polizas, contrato_umbral_bajo, date(2027, 4, 15))
    assert alertas == []
