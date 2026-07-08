import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from db.carga import cargar_facturacion, cargar_polizas, cargar_factura_pdf, recalcular_resumen_mensual
from db.schema import inicializar_schema
from engine.config_contrato import cargar_contrato
from ingestion.facturacion import parsear_facturacion
from ingestion.polizas import parsear_polizas

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG_PATH = Path(__file__).parent.parent / "config" / "contrato.yaml"


@pytest.fixture
def contrato():
    return cargar_contrato(CONFIG_PATH)


@pytest.fixture
def conn():
    conexion = sqlite3.connect(":memory:")
    inicializar_schema(conexion)
    yield conexion
    conexion.close()


def _cargar_fixtures_basicas(conn):
    cargar_polizas(conn, parsear_polizas(FIXTURES / "polizas_sample.csv"))
    cargar_facturacion(conn, parsear_facturacion(FIXTURES / "facturacion_sample.csv"))


def test_recalcular_resumen_mensual_crea_snapshot_por_periodo(conn, contrato):
    _cargar_fixtures_basicas(conn)

    filas = recalcular_resumen_mensual(conn, contrato)
    assert filas == 1  # las 3 pólizas de la fixture caen todas en 2026-06

    resumen = pd.read_sql("SELECT * FROM resumen_mensual", conn)
    assert len(resumen) == 1
    fila = resumen.iloc[0]
    assert fila["periodo"] == "2026-06"
    assert fila["polizas_nuevas"] == 3
    assert fila["produccion_total"] == pytest.approx(360.0 + 144.0 + 500.0)
    assert fila["rappel_real"] is None
    assert fila["comision_neta_real"] is None


def test_recalcular_resumen_mensual_es_idempotente(conn, contrato):
    _cargar_fixtures_basicas(conn)
    recalcular_resumen_mensual(conn, contrato)
    recalcular_resumen_mensual(conn, contrato)

    resumen = pd.read_sql("SELECT * FROM resumen_mensual", conn)
    assert len(resumen) == 1  # no duplica filas por periodo, solo actualiza


def test_recalcular_resumen_mensual_sin_polizas_no_crea_filas(conn, contrato):
    assert recalcular_resumen_mensual(conn, contrato) == 0
    resumen = pd.read_sql("SELECT * FROM resumen_mensual", conn)
    assert resumen.empty


def test_recalcular_resumen_mensual_incluye_rappel_real_si_hay_factura_pdf(conn, contrato):
    _cargar_fixtures_basicas(conn)

    class FacturaFalsa:
        def __init__(self):
            self.entidad_cif = "A08169294"
            self.entidad_nombre = "ASISA"
            self.numero_factura = "F-2026-06"
            self.fecha_factura = "2026-07-01"
            self.periodo = "2026-06"
            self.rappel = 900.0
            self.totales = {"total_factura": 1500.0}

    cargar_factura_pdf(conn, [FacturaFalsa()])
    recalcular_resumen_mensual(conn, contrato)

    resumen = pd.read_sql("SELECT * FROM resumen_mensual", conn)
    fila = resumen.iloc[0]
    assert fila["rappel_real"] == pytest.approx(900.0)
    assert fila["comision_neta_real"] == pytest.approx(600.0)  # 1500 - 900
