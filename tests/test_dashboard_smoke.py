"""Pruebas de humo del dashboard completo con datos PARCIALES.

Reproducen el bug real reportado: subir solo Facturación (sin Pólizas
todavía) hacía reventar la pestaña Pólizas con StreamlitAPIException
porque un multiselect tenía default=["A"] fijo. Estas pruebas ejecutan
el script de Streamlit entero (las 5 pestañas se renderizan en la misma
pasada) contra bases de datos con un único fichero cargado, y confirman
que ninguna combinación revienta — a lo sumo debe mostrar menos
información de la ideal, nunca una excepción.
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date
from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

from db.carga import cargar_facturacion, cargar_factura_pdf, cargar_liquidacion, cargar_polizas
from db.schema import inicializar_schema
from ingestion.facturacion import parsear_facturacion
from ingestion.liquidacion import parsear_liquidacion
from ingestion.polizas import parsear_polizas

APP_PATH = Path(__file__).parent.parent / "src" / "dashboard" / "app.py"
FIXTURES = Path(__file__).parent / "fixtures"


class _FacturaPdfFalsa:
    """Sustituto mínimo de FacturaEntidad para no depender de un PDF real."""

    def __init__(self):
        self.entidad_cif = "A08169294"
        self.entidad_nombre = "ASISA"
        self.numero_factura = "F-2026-06"
        self.fecha_factura = "2026-07-01"
        self.periodo = "2026-06"
        self.rappel = 900.0
        self.totales = {"total_liquidacion": 1000.0, "total_factura": 1500.0, "irpf": 100.0, "base_factura": 1600.0}


def _nueva_db(tmp_path, nombre) -> Path:
    db_path = tmp_path / nombre
    conn = sqlite3.connect(db_path)
    inicializar_schema(conn)
    return db_path, conn


def _correr_app(db_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DB_PATH", str(db_path))
    # st.cache_resource/st.cache_data son cachés GLOBALES de proceso,
    # indexadas por el código fuente de la función cacheada, no por sus
    # argumentos ni por qué AppTest las ejecutó. get_conn()/cargar_datos()
    # no reciben la ruta de BD como argumento (leen el global DB_PATH), así
    # que sin este clear() un test posterior heredaría la conexión/los
    # DataFrames del test anterior aunque apunte a una BD distinta.
    st.cache_resource.clear()
    st.cache_data.clear()
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    return at


def test_dashboard_no_revienta_con_solo_facturacion(tmp_path, monkeypatch):
    # El escenario real que reportó el bug: Facturación subida, Pólizas no.
    db_path, conn = _nueva_db(tmp_path, "solo_facturacion.db")
    cargar_facturacion(conn, parsear_facturacion(FIXTURES / "facturacion_sample.csv"))
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []


def test_dashboard_no_revienta_con_solo_polizas(tmp_path, monkeypatch):
    db_path, conn = _nueva_db(tmp_path, "solo_polizas.db")
    cargar_polizas(conn, parsear_polizas(FIXTURES / "polizas_sample.csv"))
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []


def test_dashboard_no_revienta_con_solo_liquidacion(tmp_path, monkeypatch):
    db_path, conn = _nueva_db(tmp_path, "solo_liquidacion.db")
    cargar_liquidacion(conn, parsear_liquidacion(FIXTURES / "liquidacion_sample.csv"))
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []


def test_dashboard_no_revienta_con_solo_factura_pdf(tmp_path, monkeypatch):
    db_path, conn = _nueva_db(tmp_path, "solo_factura_pdf.db")
    cargar_factura_pdf(conn, [_FacturaPdfFalsa()])
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []


def test_rappel_cuenta_poliza_de_fin_de_mes_en_su_periodo_real(tmp_path, monkeypatch):
    """Caso real confirmado: pólizas con fecha_efecto a fin de mes cuyo
    PRIMER recibo de Facturación (el que fija el periodo real de devengo
    16->15 de ASISA) cae en el mes SIGUIENTE al calendario de fecha_efecto.
    La pestaña Rappel debe contarlas en el mes real (periodo_liquidacion),
    no en el mes calendario de fecha_efecto — igual que las pólizas reales
    64201679/64174100 (fecha_efecto 30/06/2026, periodo_liquidacion
    "2026-07") que motivaron este fix.

    Las fechas se generan relativas a `date.today()` (no hardcodeadas a
    2026) para que esta prueba no caduque cuando pase julio de 2026: la
    pestaña Rappel filtra por el mes calendario de HOY, así que el caso de
    prueba debe construirse siempre alrededor de "hoy", sea cuando sea.
    """
    hoy = date.today()
    periodo_actual = f"{hoy.year:04d}-{hoy.month:02d}"
    if hoy.month == 1:
        anio_ant, mes_ant = hoy.year - 1, 12
    else:
        anio_ant, mes_ant = hoy.year, hoy.month - 1
    ultimo_dia_mes_ant = calendar.monthrange(anio_ant, mes_ant)[1]
    fecha_efecto_fin_mes_ant = f"{ultimo_dia_mes_ant:02d}/{mes_ant:02d}/{anio_ant:04d}"

    polizas_csv = tmp_path / "polizas_fin_de_mes.csv"
    polizas_csv.write_text(
        "AGENTE;ORDEN NIF;NOMBRE AGENTE;CLIENTE;RAZON SOCIAL;POLIZA;ORDEN;PRODUCTO BASE;"
        "PRODUCTO;FECHA GRAB;FECHA ALTA;FECHA BAJA;FORMA PAGO;SITUACION POLIZA;"
        "INDICADOR DE FACTURACION;NIF TOMADOR;NOMBRE TOMADOR;PRIMER APELLIDO TOMADOR;"
        "SEGUNDO APELLIDO TOMADOR;DIRECCION TOMADOR;C  POSTAL TOMADOR;POBLACION TOMADOR;"
        "PROVINCIA TOMADOR;TELEFONO TOMADOR;F  NACIMIENTO TOMADOR;NIF ASEGURADO;"
        "NOMBRE ASEGURADO;PRIMER APELLIDO ASEGURADO;SEGUNDO APELLIDO ASEGURADO;"
        "DIRECCION ASEGURADO;C  POSTAL ASEGURADO;POBLACION ASEGURADO;PROVINCIA ASEGURADO;"
        "TELEFONO ASEGURADO;F  NACIMIENTOASEGURADO;DELEGACION;DESCRIPCION;PER  LIQUIDACION;"
        "SUBAGENTE\n"
        f"00000000X;0;AGENTE PRUEBA;90099;ASISA PARTICULARES;64201679;0;"
        f"ASISTENCIA SANITARIA;101049;{fecha_efecto_fin_mes_ant};{fecha_efecto_fin_mes_ant};"
        f"01/01/1900;M;A;S;X0000099A;NOMBRE;APELLIDO1;APELLIDO2;Calle Real 1;28000;MADRID;"
        f"Madrid;+34600000099;01/01/1990;X0000099A;NOMBRE;APELLIDO1;APELLIDO2;Calle Real 1;"
        f"28000;MADRID;Madrid;+34600000099;01/01/1990;2800;MADRID;{mes_ant:04d};\n",
        encoding="utf-8",
    )

    facturacion_csv = tmp_path / "facturacion_fin_de_mes.csv"
    facturacion_csv.write_text(
        "CLIENTE;CARTERA;OPERACION;POLIZA;NOMBRE CLIENTE;FECHA DESDE;FECHA HASTA;"
        "PRIMA NETA;PRIMA TOTAL;PER. LIQUIDACION\n"
        f"90099;ASISTENCIA SANITARIA;CARTERA;64201679;ASISA PARTICULARES;"
        f"{fecha_efecto_fin_mes_ant};{fecha_efecto_fin_mes_ant};40,00;40,10;{periodo_actual}\n",
        encoding="utf-8",
    )

    db_path, conn = _nueva_db(tmp_path, "fin_de_mes.db")
    cargar_polizas(conn, parsear_polizas(polizas_csv))
    cargar_facturacion(conn, parsear_facturacion(facturacion_csv))
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []

    metricas = {m.label: m.value for m in at.metric}
    # 40€ de prima mensual anualizada = 480€ — debe aparecer como
    # producción del mes EN CURSO (periodo_liquidacion), aunque fecha_efecto
    # caiga en el mes calendario anterior.
    assert metricas["Producción estimada del mes"] == "480.00 €"


def test_dashboard_no_revienta_con_polizas_y_liquidacion_sin_facturacion(tmp_path, monkeypatch):
    # Pólizas + Liquidación pero SIN Facturación del mes: combinación real
    # (p.ej. Sebastián sube Liquidación mensual antes que la Facturación
    # semanal del mismo periodo).
    db_path, conn = _nueva_db(tmp_path, "polizas_liquidacion_sin_facturacion.db")
    cargar_polizas(conn, parsear_polizas(FIXTURES / "polizas_sample.csv"))
    cargar_liquidacion(conn, parsear_liquidacion(FIXTURES / "liquidacion_sample.csv"))
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []
