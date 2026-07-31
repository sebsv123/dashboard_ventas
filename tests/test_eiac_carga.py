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
    assert n1 == 3  # junio (deduplicado) + julio + 9876542

    # Reimportar el mismo fichero no crea filas nuevas -- el UPSERT
    # actualiza in situ (mismos valores, misma prioridad CO/PE), pero el
    # recuento de filas de la tabla no cambia.
    n2 = cargar_eiac_recibos(conn, df)
    assert n2 == 3  # 3 filas "tocadas" por el upsert, 0 filas nuevas
    total = pd.read_sql("SELECT COUNT(*) AS n FROM eiac_recibos", conn).iloc[0]["n"]
    assert total == 3


def test_cargar_eiac_recibos_guarda_situacion_co_no_pe(conn):
    df = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")
    cargar_eiac_recibos(conn, df)

    fila = pd.read_sql(
        "SELECT * FROM eiac_recibos WHERE id_poliza = ? AND fecha_efecto_inicial = ?",
        conn, params=("0001234-9876541", "2026-06-01"),
    ).iloc[0]
    assert fila["situacion_recibo"] == "CO"
    assert fila["prima_total"] == 360.50  # el importe del intento CO, no el del PE descartado


def test_cargar_eiac_recibos_un_pe_tardio_no_degrada_un_co_ya_guardado(conn):
    # Simula lo que pasa de verdad con los ficheros reales: se sube un
    # lote con el CO ya confirmado, y días después llega OTRO lote con un
    # reintento PE residual del mismo recibo (mismo id_poliza+fecha) --
    # el CO ya guardado NO debe degradarse a PE.
    co_ya_guardado = pd.DataFrame(
        [
            {
                "id_poliza": "P1", "prima_total": 100.0, "prima_neta": 90.0,
                "situacion_recibo": "CO", "fecha_efecto_inicial": pd.Timestamp("2026-06-01"),
                "clase_forma_pago": "CC", "pista_forma_pago": "posible_mensual",
            }
        ]
    )
    cargar_eiac_recibos(conn, co_ya_guardado)

    pe_tardio = pd.DataFrame(
        [
            {
                "id_poliza": "P1", "prima_total": 999.0, "prima_neta": 900.0,
                "situacion_recibo": "PE", "fecha_efecto_inicial": pd.Timestamp("2026-06-01"),
                "clase_forma_pago": "CC", "pista_forma_pago": "posible_mensual",
            }
        ]
    )
    n = cargar_eiac_recibos(conn, pe_tardio)
    assert n == 0  # bloqueado por la cláusula WHERE del upsert

    fila = pd.read_sql("SELECT * FROM eiac_recibos WHERE id_poliza = ?", conn, params=("P1",)).iloc[0]
    assert fila["situacion_recibo"] == "CO"
    assert fila["prima_total"] == 100.0


def test_cargar_polizas_provisionales_eiac_inserta_con_origen(conn):
    df_polizas_eiac = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    df_recibos_eiac = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")
    provisionales, _ = construir_polizas_provisionales_desde_eiac(
        df_polizas_eiac, df_recibos_eiac, pd.DataFrame()
    )

    resultado = cargar_polizas_provisionales_eiac(conn, provisionales)
    assert resultado.nuevas == 3
    assert resultado.actualizadas == 0
    assert resultado.sin_cambios == 0
    assert resultado.total_provisionales == 3

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
    resultado = cargar_polizas_provisionales_eiac(conn, provisional)
    assert resultado.ignoradas_oficiales == 1
    assert resultado.total_provisionales == 0

    fila = pd.read_sql(
        "SELECT razon_social, origen, nota_origen FROM polizas WHERE poliza = ?",
        conn, params=("70000001",),
    ).iloc[0]
    assert fila["razon_social"] == "ASISA PARTICULARES"  # dato oficial intacto
    assert fila["origen"] == "ASISA_CSV"  # sigue siendo la oficial
    assert fila["nota_origen"] is None


def test_carga_provisional_eiac_clasifica_nuevas_sin_cambios_y_actualizadas(conn):
    filas = pd.DataFrame([
        {
            "poliza": f"EIAC-{n}", "cliente_codigo": str(n), "razon_social": "ASISA PARTICULARES",
            "producto_base": "SALUD", "producto_codigo": "101049", "fecha_emision": pd.NaT,
            "fecha_efecto": pd.Timestamp("2026-07-01"), "fecha_baja": None, "forma_pago": "M",
            "situacion": "A", "provincia_tomador": None, "delegacion": "MADRID",
            "nombre_tomador": None, "origen": "EIAC", "nota_origen": "Provisional",
        }
        for n in range(1, 5)
    ])
    primera = cargar_polizas_provisionales_eiac(conn, filas)
    assert (primera.nuevas, primera.actualizadas, primera.sin_cambios, primera.total_provisionales) == (4, 0, 0, 4)
    segunda = cargar_polizas_provisionales_eiac(conn, filas.copy())
    assert (segunda.nuevas, segunda.actualizadas, segunda.sin_cambios, segunda.total_provisionales) == (0, 0, 4, 4)

    modificada = filas.copy()
    modificada.loc[0, "delegacion"] = "SEVILLA"
    tercera = cargar_polizas_provisionales_eiac(conn, modificada)
    assert (tercera.nuevas, tercera.actualizadas, tercera.sin_cambios, tercera.total_provisionales) == (0, 1, 3, 4)
