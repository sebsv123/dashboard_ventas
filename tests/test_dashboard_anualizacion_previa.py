"""Vista rápida aplica la exclusión de anualización previa con el motor real."""

import sqlite3
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from db.schema import inicializar_schema


APP_PATH = Path(__file__).parent.parent / "src" / "dashboard" / "app.py"


def _valor(texto: str) -> float:
    return float(texto.replace(" €", "").replace(",", ""))


@pytest.mark.usefixtures("monkeypatch")
def test_vista_rapida_excluye_anualizacion_previa_y_mantiene_vida(tmp_path, monkeypatch):
    # La fecha de ejecución del proyecto es agosto de 2026; este escenario
    # sintético reproduce el periodo mostrado por Vista rápida en esa fecha.
    db_path = tmp_path / "anualizacion_previa.db"
    conn = sqlite3.connect(db_path)
    inicializar_schema(conn)
    conn.executemany(
        "INSERT INTO polizas (poliza, razon_social, forma_pago, situacion, fecha_efecto) VALUES (?, ?, ?, ?, ?)",
        [
            ("SALUD-LEGITIMA", "ASISA PARTICULARES", "M", "A", "2026-08-01"),
            ("SALUD-YA-ANUALIZADA", "ASISA PARTICULARES", "M", "A", "2026-06-01"),
            ("VIDA-RECURRENTE", "ASISA VIDA TRANQUILIDAD", "M", "A", "2026-05-01"),
        ],
    )
    conn.executemany(
        "INSERT INTO facturacion (poliza, cartera, prima_neta, prima_total, fecha_desde, fecha_hasta, periodo_liquidacion) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("SALUD-LEGITIMA", "CARTERA", 263.00, 263.00, "2026-08-01", "2026-08-31", "2026-08"),
            ("SALUD-YA-ANUALIZADA", "CARTERA", 143.20, 143.20, "2026-08-01", "2026-08-31", "2026-08"),
            ("VIDA-RECURRENTE", "CARTERA", 159.5166666667, 159.5166666667, "2026-05-01", "2026-05-31", "2026-05"),
            ("VIDA-RECURRENTE", "CARTERA", 159.5166666667, 159.5166666667, "2026-08-01", "2026-08-31", "2026-08"),
        ],
    )
    conn.execute(
        "INSERT INTO liquidacion (poliza, comision, accion, periodo_liquidacion, fecha_desde, es_extorno) VALUES (?, ?, ?, ?, ?, ?)",
        ("SALUD-YA-ANUALIZADA", 429.60, "ANUALIZADA", "2026-06", "2026-06-01", 0),
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
