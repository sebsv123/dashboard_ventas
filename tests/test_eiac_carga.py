import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from db.carga import (
    cargar_eiac_polizas,
    cargar_eiac_recibos,
    cargar_polizas,
    cargar_polizas_provisionales_eiac,
)
from db.schema import inicializar_schema
from engine.eiac_integracion import construir_polizas_provisionales_desde_eiac
from ingestion.eiac_xml import parsear_eiac_polizas, parsear_eiac_recibos
from ingestion.polizas import parsear_polizas

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


def test_cargar_polizas_provisionales_eiac_inserta_con_origen(conn):
    df_polizas_eiac = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    df_recibos_eiac = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")
    provisionales, _ = construir_polizas_provisionales_desde_eiac(
        df_polizas_eiac, df_recibos_eiac, pd.DataFrame()
    )

    n = cargar_polizas_provisionales_eiac(conn, provisionales)
    assert n == 3

    fila = pd.read_sql(
        "SELECT * FROM polizas WHERE poliza = ?", conn, params=("9876542",)
    ).iloc[0]
    assert fila["origen"] == "EIAC"
    assert "pendiente de confirmar" in fila["nota_origen"]


def test_cargar_polizas_provisionales_eiac_nunca_sobreescribe_oficial(conn):
    # Primero llega el CSV oficial de ASISA para la póliza 70000001...
    df_oficial = parsear_polizas(FIXTURES / "polizas_sample.csv")
    cargar_polizas(conn, df_oficial)

    razon_social_oficial = pd.read_sql(
        "SELECT razon_social, origen FROM polizas WHERE poliza = ?", conn, params=("70000001",)
    ).iloc[0]
    assert razon_social_oficial["origen"] == "ASISA_CSV"

    # ...luego llega (tarde) una fila EIAC provisional para la MISMA póliza,
    # con datos incompletos (razon_social None) -- no debe pisar la oficial.
    provisional = pd.DataFrame(
        [
            {
                "poliza": "70000001", "cliente_codigo": None, "razon_social": None,
                "producto_base": "SALUD", "producto_codigo": None,
                "fecha_emision": None, "fecha_efecto": None, "fecha_baja": None,
                "forma_pago": "M", "situacion": "A", "provincia_tomador": None,
                "delegacion": None, "nombre_tomador": "X", "origen": "EIAC",
                "nota_origen": "Origen: EIAC, pendiente de confirmar con Pólizas oficial.",
            }
        ]
    )
    n = cargar_polizas_provisionales_eiac(conn, provisional)
    assert n == 0  # INSERT OR IGNORE: ya existía, no se toca

    fila = pd.read_sql(
        "SELECT razon_social, origen, nota_origen FROM polizas WHERE poliza = ?",
        conn, params=("70000001",),
    ).iloc[0]
    assert fila["razon_social"] == "ASISA PARTICULARES"  # dato oficial intacto
    assert fila["origen"] == "ASISA_CSV"  # sigue siendo la oficial
    assert fila["nota_origen"] is None
