from pathlib import Path

import pandas as pd
import pytest

from engine.config_contrato import cargar_contrato
from engine.wanderlust import calcular_pae_anual

CONFIG_PATH = Path(__file__).parent.parent / "config" / "contrato.yaml"


@pytest.fixture
def contrato():
    return cargar_contrato(CONFIG_PATH)


def _poliza(poliza, razon_social, forma_pago, fecha_efecto, situacion="A", fecha_baja=None):
    return {
        "poliza": poliza,
        "razon_social": razon_social,
        "forma_pago": forma_pago,
        "fecha_efecto": fecha_efecto,
        "situacion": situacion,
        "fecha_baja": fecha_baja,
    }


def _recibo(poliza, prima_neta, periodo_liquidacion, fecha_desde, fecha_hasta=None):
    return {
        "poliza": poliza,
        "prima_neta": prima_neta,
        "periodo_liquidacion": periodo_liquidacion,
        "fecha_desde": fecha_desde,
        "fecha_hasta": fecha_hasta,
    }


# --- un caso por cada categoría de producto -----------------------------------

def test_multiplicador_correcto_por_categoria(contrato):
    df_polizas = pd.DataFrame(
        [
            _poliza("P-PART", "ASISA PARTICULARES", "M", "2026-01-10"),
            _poliza("P-RED", "ASISA RED SANITARIA", "M", "2026-01-10"),
            _poliza("P-INT", "ASISA INTEGRAL", "M", "2026-01-10"),
            _poliza("P-PYM", "ASISA PYMES", "M", "2026-01-10"),
            _poliza("P-DEN", "DENTAL", "A", "2026-01-10"),
            _poliza("P-HOS", "HOSPITALIZACION", "A", "2026-01-10"),
            _poliza("P-ACC", "ACCIDENTES", "A", "2026-01-10"),
            _poliza("P-VID", "ASISA VIDA TRANQUILIDAD", "M", "2026-01-10"),
            _poliza("P-DEC", "DECESOS", "A", "2026-01-10"),
            _poliza("P-MAS", "MASCOTAS", "A", "2026-01-10"),
            _poliza("P-TRA", "ASISA TRAVEL AND YOU", "A", "2026-01-10"),
        ]
    )
    df_facturacion = pd.DataFrame(
        [
            _recibo("P-PART", 10.0, "2026-01", "2026-01-10"),
            _recibo("P-RED", 10.0, "2026-01", "2026-01-10"),
            _recibo("P-INT", 10.0, "2026-01", "2026-01-10"),
            _recibo("P-PYM", 10.0, "2026-01", "2026-01-10"),
            _recibo("P-DEN", 100.0, "2026-01", "2026-01-10"),
            _recibo("P-HOS", 100.0, "2026-01", "2026-01-10"),
            _recibo("P-ACC", 100.0, "2026-01", "2026-01-10"),
            _recibo("P-VID", 10.0, "2026-01", "2026-01-10"),
            _recibo("P-DEC", 100.0, "2026-01", "2026-01-10"),
            _recibo("P-MAS", 100.0, "2026-01", "2026-01-10"),
            _recibo("P-TRA", 100.0, "2026-01", "2026-01-10"),
        ]
    )

    resultado = calcular_pae_anual(df_polizas, df_facturacion, contrato, anio=2026)
    por_cat = {c: d.pae for c, d in resultado.por_categoria.items()}

    # mensual -> anualizada x12, luego x multiplicador
    assert por_cat["Salud Particulares"] == pytest.approx((10.0 * 12) * 1.0 * 2)  # PART + RED
    assert por_cat["Salud colectivos privados"] == pytest.approx((10.0 * 12) * 0.7 * 2)  # INT + PYM
    assert por_cat["ASISA Dental"] == pytest.approx(100.0 * 1.5)  # anual, tal cual
    assert por_cat["ASISA Hospitalización"] == pytest.approx(100.0 * 4.0)
    assert por_cat["ASISA Accidentes"] == pytest.approx(100.0 * 4.0)
    assert por_cat["ASISA Vida"] == pytest.approx((10.0 * 12) * 3.5)
    assert por_cat["ASISA Decesos"] == pytest.approx(100.0 * 3.5)
    assert por_cat["ASISA Mascotas"] == pytest.approx(100.0 * 1.2)
    assert por_cat["ASISA Travel"] == pytest.approx(100.0 * 0.5)


def test_producto_desconocido_no_suma_y_se_cuenta(contrato):
    df_polizas = pd.DataFrame([_poliza("P1", "ASISA PRODUCTO INEXISTENTE", "A", "2026-03-01")])
    df_facturacion = pd.DataFrame([_recibo("P1", 100.0, "2026-03", "2026-03-01")])

    resultado = calcular_pae_anual(df_polizas, df_facturacion, contrato, anio=2026)
    assert resultado.pae_total == 0.0
    assert resultado.sin_categoria == 1


# --- anulación resta PAE -------------------------------------------------------

def test_anulacion_resta_pae_completo(contrato):
    df_polizas = pd.DataFrame(
        [
            _poliza("P1", "ASISA PARTICULARES", "A", "2026-02-01", situacion="A"),
            _poliza("P2", "ASISA PARTICULARES", "A", "2026-02-15", situacion="B"),
        ]
    )
    df_facturacion = pd.DataFrame(
        [
            _recibo("P1", 500.0, "2026-02", "2026-02-01"),
            _recibo("P2", 300.0, "2026-02", "2026-02-15"),
        ]
    )
    resultado = calcular_pae_anual(df_polizas, df_facturacion, contrato, anio=2026)
    desglose = resultado.por_categoria["Salud Particulares"]

    # 500*1.0 (alta) - 300*1.0 (anulada) = 200
    assert desglose.pae == pytest.approx(200.0)
    assert desglose.polizas_alta == 1
    assert desglose.polizas_baja == 1
    assert resultado.pae_total == pytest.approx(200.0)


def test_vencimiento_natural_no_resta_caso_real_64171931(contrato):
    """Caso real: ASISA Travel and You, viaje de 9 días, situacion="B" en
    Pólizas pero FechaAnulacion (fecha_baja) coincide EXACTAMENTE con la
    fecha_hasta del recibo (fin de cobertura contratada) -- no es una
    cancelación anticipada, es el vencimiento normal del producto. Debe
    contar como PAE ganado, no restarse."""
    df_polizas = pd.DataFrame(
        [
            _poliza(
                "64171931", "ASISA TRAVEL AND YOU", "A", "2026-06-10",
                situacion="B", fecha_baja="2026-06-19",
            ),
        ]
    )
    df_facturacion = pd.DataFrame(
        [
            _recibo("64171931", 100.0, "2026-06", "2026-06-10", fecha_hasta="2026-06-19"),
        ]
    )
    resultado = calcular_pae_anual(df_polizas, df_facturacion, contrato, anio=2026)
    desglose = resultado.por_categoria["ASISA Travel"]

    assert desglose.pae == pytest.approx(100.0 * 0.5)
    assert desglose.polizas_alta == 1
    assert desglose.polizas_baja == 0
    assert resultado.pae_total == pytest.approx(50.0)


def test_cancelacion_anticipada_real_si_resta_pae(contrato):
    """Cancelación anticipada genuina: fecha_baja ANTES de agotar la
    cobertura contratada (fecha_hasta) -- a diferencia del vencimiento
    natural de arriba, esto sí debe restar la prima anual completa."""
    df_polizas = pd.DataFrame(
        [
            _poliza(
                "P-CANC", "ASISA PARTICULARES", "M", "2026-03-01",
                situacion="B", fecha_baja="2026-04-01",
            ),
        ]
    )
    df_facturacion = pd.DataFrame(
        [
            # cobertura contratada hasta fin de año, pero se anula en abril
            _recibo("P-CANC", 50.0, "2026-03", "2026-03-01", fecha_hasta="2026-12-31"),
        ]
    )
    resultado = calcular_pae_anual(df_polizas, df_facturacion, contrato, anio=2026)
    desglose = resultado.por_categoria["Salud Particulares"]

    # 50*12*1.0 = 600, restado por completo
    assert desglose.pae == pytest.approx(-600.0)
    assert desglose.polizas_alta == 0
    assert desglose.polizas_baja == 1
    assert resultado.pae_total == pytest.approx(-600.0)


# --- NO se usa periodo_liquidacion ---------------------------------------------

def test_no_usa_periodo_liquidacion_solo_fecha_efecto(contrato):
    """Poliza con fecha_efecto en 2025 pero periodo_liquidacion "2026-01"
    (ciclo 16->15: efecto 30/12/2025 cae en el periodo de enero) NO debe
    contar en el PAE de 2026 -- si el cálculo mirase periodo_liquidacion en
    vez de fecha_efecto, esta prueba fallaría."""
    df_polizas = pd.DataFrame(
        [
            # fecha_efecto en 2025, pero su recibo cae en periodo_liquidacion "2026-01"
            _poliza("P-2025", "ASISA PARTICULARES", "M", "2025-12-30"),
            # fecha_efecto en 2026, pero con un periodo_liquidacion "2025-12"
            # (caso inverso, para probar que tampoco excluye por error)
            _poliza("P-2026", "ASISA PARTICULARES", "M", "2026-01-02"),
        ]
    )
    df_facturacion = pd.DataFrame(
        [
            _recibo("P-2025", 10.0, "2026-01", "2025-12-30"),
            _recibo("P-2026", 20.0, "2025-12", "2026-01-02"),
        ]
    )
    resultado = calcular_pae_anual(df_polizas, df_facturacion, contrato, anio=2026)
    desglose = resultado.por_categoria["Salud Particulares"]

    # Solo P-2026 cuenta (fecha_efecto 2026), aunque su periodo_liquidacion
    # sea "2025-12". P-2025 no cuenta pese a periodo_liquidacion "2026-01".
    assert desglose.pae == pytest.approx(20.0 * 12 * 1.0)
    assert desglose.polizas_alta == 1


# --- objetivo / porcentaje ------------------------------------------------------

def test_sin_objetivo_no_inventa_porcentaje(contrato):
    df_polizas = pd.DataFrame([_poliza("P1", "ASISA PARTICULARES", "A", "2026-01-01")])
    df_facturacion = pd.DataFrame([_recibo("P1", 100.0, "2026-01", "2026-01-01")])
    resultado = calcular_pae_anual(df_polizas, df_facturacion, contrato, anio=2026, objetivo=None)
    assert resultado.objetivo is None
    assert resultado.porcentaje is None


def test_usa_objetivo_del_contrato_por_defecto(contrato):
    resultado = calcular_pae_anual(pd.DataFrame(), pd.DataFrame(), contrato, anio=2026)
    assert resultado.objetivo == contrato.wanderlust_objetivo_paes
    assert resultado.objetivo is None  # valor actual de config/contrato.yaml (no definido)


def test_df_vacios_no_revienta(contrato):
    resultado = calcular_pae_anual(pd.DataFrame(), pd.DataFrame(), contrato, anio=2026, objetivo=5000.0)
    assert resultado.pae_total == 0.0
    assert resultado.por_categoria == {}
    assert resultado.porcentaje == 0.0
