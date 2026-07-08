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

import sqlite3
from pathlib import Path

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
