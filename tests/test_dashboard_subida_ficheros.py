"""Tests de la UX de subida de ficheros en la barra lateral.

Cubren las 3 mejoras: múltiples ficheros por uploader, resumen de
"ficheros ya cargados" por tipo y periodo, y vaciado de los uploaders
tras un "Procesar" con éxito (patrón de key incremental en session_state).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from streamlit.testing.v1 import AppTest

from db.schema import inicializar_schema

APP_PATH = Path(__file__).parent.parent / "src" / "dashboard" / "app.py"
FIXTURES = Path(__file__).parent / "fixtures"


def _nueva_db(tmp_path, nombre) -> Path:
    db_path = tmp_path / nombre
    conn = sqlite3.connect(db_path)
    inicializar_schema(conn)
    conn.close()
    return db_path


def _iniciar_app(db_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DB_PATH", str(db_path))
    import streamlit as st

    st.cache_resource.clear()
    st.cache_data.clear()
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    return at


def _uploader(at, prefijo_key):
    return next(f for f in at.file_uploader if f.key.startswith(prefijo_key))


def test_uploaders_aceptan_multiples_ficheros(tmp_path, monkeypatch):
    db_path = _nueva_db(tmp_path, "multi.db")
    at = _iniciar_app(db_path, monkeypatch)
    assert at.exception == []
    for f in at.file_uploader:
        assert f.accept_multiple_files is True


def test_procesar_vacio_muestra_aviso_y_no_revienta(tmp_path, monkeypatch):
    db_path = _nueva_db(tmp_path, "sin_ficheros.db")
    at = _iniciar_app(db_path, monkeypatch)

    at.sidebar.button[0].click().run()
    assert at.exception == []
    assert any("ningún fichero" in w.value for w in at.warning)


def test_subir_varios_facturacion_a_la_vez_y_uploader_se_vacia(tmp_path, monkeypatch):
    db_path = _nueva_db(tmp_path, "varios_facturacion.db")
    at = _iniciar_app(db_path, monkeypatch)

    contenido = (FIXTURES / "facturacion_sample.csv").read_bytes()
    fu = _uploader(at, "facturacion_")
    fu.upload("Facturacion_06_2026.csv", contenido, "text/csv")
    at.run()
    assert at.exception == []
    assert len(fu.value) == 1

    at.sidebar.button[0].click().run()
    assert at.exception == []

    # Mensaje de éxito visible tras el rerun automático del botón.
    assert any("Facturación" in s.value and "Facturacion_06_2026.csv" in s.value for s in at.success)

    # El uploader debe volver a estar vacío (nueva key, valor []).
    fu_tras_procesar = _uploader(at, "facturacion_")
    assert fu_tras_procesar.key != fu.key
    assert fu_tras_procesar.value == []


def test_resumen_ficheros_cargados_muestra_periodos_por_tipo(tmp_path, monkeypatch):
    db_path = _nueva_db(tmp_path, "resumen_cargados.db")
    at = _iniciar_app(db_path, monkeypatch)

    contenido = (FIXTURES / "facturacion_sample.csv").read_bytes()
    _uploader(at, "facturacion_").upload("Facturacion_06_2026.csv", contenido, "text/csv")
    at.run()
    at.sidebar.button[0].click().run()
    assert at.exception == []

    caption_facturacion = next(c for c in at.caption if c.value.startswith("**Facturación:**"))
    assert "06-2026" in caption_facturacion.value


def test_resumen_ficheros_sin_polizas_dice_ninguna_todavia(tmp_path, monkeypatch):
    db_path = _nueva_db(tmp_path, "resumen_sin_polizas.db")
    at = _iniciar_app(db_path, monkeypatch)

    contenido = (FIXTURES / "facturacion_sample.csv").read_bytes()
    _uploader(at, "facturacion_").upload("Facturacion_06_2026.csv", contenido, "text/csv")
    at.run()
    at.sidebar.button[0].click().run()
    assert at.exception == []

    caption_polizas = next(c for c in at.caption if c.value.startswith("**Pólizas:**"))
    assert "ninguna todavía" in caption_polizas.value


def test_resumen_agrupa_periodos_de_liquidacion_en_formato_mm_aaaa(tmp_path, monkeypatch):
    # Caso real confirmado: Facturación usa "AAAA-MM" (p.ej. "2026-06") pero
    # Liquidación, en los ficheros reales de ASISA, usa "MM-AAAA" (p.ej.
    # "02-2026") para la misma columna "PER. LIQUIDACION". El resumen debe
    # agrupar igual de bien en ambos formatos, no solo en el de Facturación.
    db_path = _nueva_db(tmp_path, "formato_mixto.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO liquidacion (poliza, periodo_liquidacion, accion, es_extorno) "
        "VALUES ('1', '02-2026', 'PRODUCCION', 0), ('2', '03-2026', 'PRODUCCION', 0)"
    )
    conn.commit()
    conn.close()

    at = _iniciar_app(db_path, monkeypatch)
    assert at.exception == []

    caption_liquidacion = next(c for c in at.caption if c.value.startswith("**Liquidación:**"))
    assert "02, 03-2026" in caption_liquidacion.value
