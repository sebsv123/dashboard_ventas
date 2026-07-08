import pandas as pd
import pytest

from engine.fiscal import anios_disponibles, calcular_retenciones_anio


def _df_factura_pdf():
    # Datos sintéticos: 2 meses (mayo, junio 2026) x 2 entidades (Salud, Vida).
    return pd.DataFrame(
        [
            {
                "entidad_cif": "A08169294",
                "entidad_nombre": "ASISA Salud",
                "periodo": "2026-05",
                "base_factura": 2072.94,
                "irpf": -310.94,
                "total_factura": 1762.00,
            },
            {
                "entidad_cif": "A87425070",
                "entidad_nombre": "ASISA Vida",
                "periodo": "2026-05",
                "base_factura": 7.13,
                "irpf": -1.07,
                "total_factura": 6.06,
            },
            {
                "entidad_cif": "A08169294",
                "entidad_nombre": "ASISA Salud",
                "periodo": "2026-06",
                "base_factura": 2450.46,
                "irpf": -367.57,
                "total_factura": 2082.89,
            },
            {
                "entidad_cif": "A87425070",
                "entidad_nombre": "ASISA Vida",
                "periodo": "2026-06",
                "base_factura": 128.77,
                "irpf": -19.32,
                "total_factura": 109.45,
            },
        ]
    )


def test_calcular_retenciones_anio_suma_total_ambas_entidades_y_meses():
    resultado = calcular_retenciones_anio(_df_factura_pdf(), "2026")
    assert resultado.total_retenido == pytest.approx(310.94 + 1.07 + 367.57 + 19.32)
    assert resultado.total_base == pytest.approx(2072.94 + 7.13 + 2450.46 + 128.77)
    assert resultado.total_factura_neto == pytest.approx(1762.00 + 6.06 + 2082.89 + 109.45)


def test_calcular_retenciones_anio_desglose_mensual_tiene_los_2_meses():
    resultado = calcular_retenciones_anio(_df_factura_pdf(), "2026")
    assert list(resultado.desglose_mensual["periodo"]) == ["2026-05", "2026-06"]
    fila_mayo = resultado.desglose_mensual[resultado.desglose_mensual["periodo"] == "2026-05"].iloc[0]
    assert fila_mayo["irpf_retenido"] == pytest.approx(310.94 + 1.07)


def test_calcular_retenciones_anio_desglose_por_entidad():
    resultado = calcular_retenciones_anio(_df_factura_pdf(), "2026")
    assert set(resultado.desglose_entidad["entidad_nombre"]) == {"ASISA Salud", "ASISA Vida"}
    fila_salud = resultado.desglose_entidad[
        resultado.desglose_entidad["entidad_nombre"] == "ASISA Salud"
    ].iloc[0]
    assert fila_salud["irpf_retenido"] == pytest.approx(310.94 + 367.57)


def test_calcular_retenciones_anio_sin_datos_del_anio_da_ceros():
    resultado = calcular_retenciones_anio(_df_factura_pdf(), "2025")
    assert resultado.total_retenido == 0.0
    assert resultado.desglose_mensual.empty
    assert resultado.desglose_entidad.empty


def test_calcular_retenciones_anio_df_vacio_no_revienta():
    resultado = calcular_retenciones_anio(pd.DataFrame(), "2026")
    assert resultado.total_retenido == 0.0
    assert resultado.desglose_mensual.empty


def test_anios_disponibles_ordena_descendente():
    df = pd.DataFrame({"periodo": ["2024-12", "2026-05", "2025-01"]})
    assert anios_disponibles(df) == ["2026", "2025", "2024"]


def test_anios_disponibles_vacio():
    assert anios_disponibles(pd.DataFrame()) == []
