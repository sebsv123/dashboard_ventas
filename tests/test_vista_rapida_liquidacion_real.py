"""Vista rápida (y Rappel) muestran la Liquidación real cuando ya existe
para el periodo, en vez de seguir mostrando el estimado del motor.

Caso real que motivó esto (ago-2026): "Comisión Salud estimada" de agosto
sobreestimaba en +109,11€ porque el motor nunca ve regularizaciones
posteriores a la primera alta de una póliza (extornos/reanualizaciones,
ver `engine.calibracion.comision_bruta_real_periodo`). Mismo principio que
"el CSV oficial siempre gana sobre EIAC": si Liquidación real ya tiene
movimientos de un periodo, esa cifra gana sobre lo que el motor hubiera
predicho.
"""

import sqlite3
from datetime import date
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from db.schema import inicializar_schema

APP_PATH = Path(__file__).parent.parent / "src" / "dashboard" / "app.py"


def _valor(texto: str) -> float:
    return float(texto.replace(" €", "").replace(",", ""))


def _sumar_meses(anio: int, mes: int, n: int) -> tuple[int, int]:
    total = anio * 12 + (mes - 1) + n
    return total // 12, total % 12 + 1


@pytest.mark.usefixtures("monkeypatch")
def test_vista_rapida_usa_comision_real_cuando_ya_hay_liquidacion_del_periodo(tmp_path, monkeypatch):
    hoy = date.today()
    periodo_actual = f"{hoy.year:04d}-{hoy.month:02d}"
    periodo_liquidacion = f"{hoy.month:02d}-{hoy.year:04d}"
    fecha_efecto = f"{periodo_actual}-05"
    anio_siguiente, mes_siguiente = _sumar_meses(hoy.year, hoy.month, 1)
    fin_vida = f"{anio_siguiente:04d}-{mes_siguiente:02d}-05"

    db_path = tmp_path / "liquidacion_real_mes_actual.db"
    conn = sqlite3.connect(db_path)
    inicializar_schema(conn)
    conn.executemany(
        "INSERT INTO polizas (poliza, razon_social, forma_pago, situacion, fecha_efecto) VALUES (?, ?, ?, ?, ?)",
        [
            ("SALUD-REAL", "ASISA PARTICULARES", "A", "A", fecha_efecto),
            ("VIDA-REAL", "ASISA VIDA TRANQUILIDAD", "M", "A", fecha_efecto),
        ],
    )
    conn.executemany(
        "INSERT INTO facturacion (poliza, cartera, prima_neta, prima_total, fecha_desde, fecha_hasta, periodo_liquidacion) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            # Estimado (SALUD): 400 x 25% = 100,00€. Real en Liquidación: 80,00€.
            ("SALUD-REAL", "CARTERA", 400.00, 400.00, fecha_efecto, fecha_efecto, periodo_actual),
            # Estimado (VIDA): 50 x 60% = 30,00€. Real en Liquidación: 25,00€.
            ("VIDA-REAL", "CARTERA", 50.00, 50.00, fecha_efecto, fin_vida, periodo_actual),
        ],
    )
    conn.executemany(
        "INSERT INTO liquidacion (poliza, razon_social, comision, accion, periodo_liquidacion, fecha_desde, es_extorno) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("SALUD-REAL", "ASISA PARTICULARES", 80.00, "PRODUCCION", periodo_liquidacion, fecha_efecto, 0),
            ("VIDA-REAL", "ASISA VIDA TRANQUILIDAD", 25.00, "PRODUCCION", periodo_liquidacion, fecha_efecto, 0),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DASHBOARD_DB_PATH", str(db_path))
    st.cache_resource.clear()
    st.cache_data.clear()
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()

    assert at.exception == []

    # La cifra REAL de Liquidación gana -- ya no aparece como "estimada".
    assert any(m.label == "Comisión Salud real" and _valor(m.value) == pytest.approx(80.00) for m in at.metric)
    assert any(m.label == "Comisión Vida real" and _valor(m.value) == pytest.approx(25.00) for m in at.metric)
    assert any(m.label == "💰 Total NETO real" for m in at.metric)
    # Ninguna métrica de "Mes actual" debe seguir anunciando el estimado
    # del motor para Salud/Vida cuando ya hay Liquidación real de ese
    # periodo cargada.
    assert not any(m.label == "Comisión Salud estimada" for m in at.metric)
    assert not any(m.label == "Comisión Vida estimada" for m in at.metric)
