import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from db.carga import cargar_eiac_polizas, cargar_eiac_recibos
from db.schema import inicializar_schema
from ingestion.eiac_xml import parsear_eiac_polizas, parsear_eiac_recibos

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn():
    conexion = sqlite3.connect(":memory:")
    inicializar_schema(conexion)
    yield conexion
    conexion.close()


def test_cargar_eiac_polizas_inserta_y_es_idempotente(conn):
    df = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    n1 = cargar_eiac_polizas(conn, df)
    assert n1 == 3

    # Reimportar el mismo fichero no duplica filas (upsert por id_poliza).
    n2 = cargar_eiac_polizas(conn, df)
    assert n2 == 3
    total = pd.read_sql("SELECT COUNT(*) AS n FROM eiac_polizas", conn).iloc[0]["n"]
    assert total == 3


def test_cargar_eiac_recibos_inserta_y_no_duplica(conn):
    df = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")
    n1 = cargar_eiac_recibos(conn, df)
    assert n1 == 3  # 360.00 (deduplicado) + 30.00 + 300.00

    n2 = cargar_eiac_recibos(conn, df)
    assert n2 == 0  # ya estaban, INSERT OR IGNORE no cuenta nada nuevo
    total = pd.read_sql("SELECT COUNT(*) AS n FROM eiac_recibos", conn).iloc[0]["n"]
    assert total == 3


def test_cargar_eiac_recibos_guarda_situacion_co_no_pe(conn):
    df = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")
    cargar_eiac_recibos(conn, df)

    fila = pd.read_sql(
        "SELECT * FROM eiac_recibos WHERE id_poliza = ? AND prima_total = ?",
        conn, params=("0001234-9876541", 360.0),
    ).iloc[0]
    assert fila["situacion_recibo"] == "CO"
