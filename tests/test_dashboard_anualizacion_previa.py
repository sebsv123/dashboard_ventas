"""Vista rápida aplica la exclusión de anualización previa con el motor real."""

import sqlite3
import calendar
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
def test_vista_rapida_excluye_anualizacion_previa_y_mantiene_vida(tmp_path, monkeypatch):
    hoy = date.today()
    periodo_actual = f"{hoy.year:04d}-{hoy.month:02d}"
    anio_anualizacion, mes_anualizacion = _sumar_meses(hoy.year, hoy.month, -2)
    periodo_anualizacion = f"{anio_anualizacion:04d}-{mes_anualizacion:02d}"
    anio_vida, mes_vida = _sumar_meses(hoy.year, hoy.month, -3)
    periodo_vida = f"{anio_vida:04d}-{mes_vida:02d}"
    inicio_actual = f"{periodo_actual}-01"
    fin_actual = f"{periodo_actual}-{calendar.monthrange(hoy.year, hoy.month)[1]:02d}"
    inicio_anualizacion = f"{periodo_anualizacion}-01"
    inicio_vida = f"{periodo_vida}-01"
    fin_vida = f"{periodo_vida}-{calendar.monthrange(anio_vida, mes_vida)[1]:02d}"

    db_path = tmp_path / "anualizacion_previa.db"
    conn = sqlite3.connect(db_path)
    inicializar_schema(conn)
    conn.executemany(
        "INSERT INTO polizas (poliza, razon_social, forma_pago, situacion, fecha_efecto) VALUES (?, ?, ?, ?, ?)",
        [
            ("SALUD-LEGITIMA", "ASISA PARTICULARES", "M", "A", inicio_actual),
            ("SALUD-YA-ANUALIZADA", "ASISA PARTICULARES", "M", "A", inicio_anualizacion),
            ("VIDA-RECURRENTE", "ASISA VIDA TRANQUILIDAD", "M", "A", inicio_vida),
        ],
    )
    conn.executemany(
        "INSERT INTO facturacion (poliza, cartera, prima_neta, prima_total, fecha_desde, fecha_hasta, periodo_liquidacion) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("SALUD-LEGITIMA", "CARTERA", 263.00, 263.00, inicio_actual, fin_actual, periodo_actual),
            ("SALUD-YA-ANUALIZADA", "CARTERA", 143.20, 143.20, inicio_actual, fin_actual, periodo_actual),
            ("VIDA-RECURRENTE", "CARTERA", 159.5166666667, 159.5166666667, inicio_vida, fin_vida, periodo_vida),
            ("VIDA-RECURRENTE", "CARTERA", 159.5166666667, 159.5166666667, inicio_actual, fin_actual, periodo_actual),
        ],
    )
    conn.execute(
        "INSERT INTO liquidacion (poliza, comision, accion, periodo_liquidacion, fecha_desde, es_extorno) VALUES (?, ?, ?, ?, ?, ?)",
        ("SALUD-YA-ANUALIZADA", 429.60, "ANUALIZADA", periodo_anualizacion, inicio_anualizacion, 0),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DASHBOARD_DB_PATH", str(db_path))
    st.cache_resource.clear()
    st.cache_data.clear()
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()

    assert at.exception == []
    assert any(m.label == "Producción nueva Salud" and _valor(m.value) == pytest.approx(3156.0) for m in at.metric)
    assert any(m.label == "Comisión Salud estimada" and _valor(m.value) == pytest.approx(789.0) for m in at.metric)
    assert any(m.label == "Comisión Vida estimada" and _valor(m.value) == pytest.approx(95.71) for m in at.metric)
    assert any(m.label == "Rappel estimado" and _valor(m.value) == pytest.approx(1136.16) for m in at.metric)
    assert any("anualización previa vigente" in aviso.value for aviso in at.warning)
