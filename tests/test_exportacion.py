from datetime import date
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest

from dashboard.exportacion import _fecha_corte_mes_anterior, _hoja_wanderlust, construir_excel_completo
from engine.config_contrato import cargar_contrato

CONFIG_PATH = Path(__file__).parent.parent / "config" / "contrato.yaml"


@pytest.fixture
def contrato():
    return cargar_contrato(CONFIG_PATH)


def _df_polizas():
    return pd.DataFrame(
        [
            {"poliza": "P1", "razon_social": "ASISA PARTICULARES", "forma_pago": "M",
             "fecha_efecto": "2026-01-10", "situacion": "A", "fecha_baja": None},
            {"poliza": "P2", "razon_social": "ASISA VIDA TRANQUILIDAD", "forma_pago": "M",
             "fecha_efecto": "2026-06-15", "situacion": "A", "fecha_baja": None},
        ]
    )


def _df_facturacion():
    return pd.DataFrame(
        [
            {"poliza": "P1", "prima_neta": 10.0, "periodo_liquidacion": "2026-01",
             "fecha_desde": "2026-01-10", "fecha_hasta": None},
            {"poliza": "P2", "prima_neta": 20.0, "periodo_liquidacion": "2026-06",
             "fecha_desde": "2026-06-15", "fecha_hasta": None},
        ]
    )


def test_fecha_corte_mes_anterior():
    assert _fecha_corte_mes_anterior(date(2026, 7, 26)).isoformat() == "2026-06-30"
    assert _fecha_corte_mes_anterior(date(2026, 3, 1)).isoformat() == "2026-02-28"


def test_hoja_wanderlust_incluye_total_y_categorias(contrato):
    tabla = _hoja_wanderlust(_df_polizas(), _df_facturacion(), contrato, date(2026, 7, 26))

    categorias = set(tabla["Categoría"])
    assert "Salud Particulares" in categorias
    assert "ASISA Vida" in categorias
    assert "TOTAL 2026" in categorias

    fila_total = tabla[tabla["Categoría"] == "TOTAL 2026"].iloc[0]
    # P1: 10*12*1.0 = 120 ; P2: 20*12*3.5 = 840 -> total 960
    assert fila_total["PAE (€)"] == pytest.approx(960.0)
    assert fila_total["Altas"] == 2
    assert fila_total["Anuladas"] == 0


def test_hoja_wanderlust_sin_objetivo_no_inventa_porcentaje(contrato):
    # config/contrato.yaml no trae wanderlust.objetivo_paes definido todavía
    tabla = _hoja_wanderlust(_df_polizas(), _df_facturacion(), contrato, date(2026, 7, 26))
    fila_objetivo = tabla[tabla["Categoría"] == "Objetivo PAE individual"]
    assert len(fila_objetivo) == 1
    assert pd.isna(fila_objetivo.iloc[0]["PAE (€)"])


def test_hoja_wanderlust_incluye_corte_mes_anterior(contrato):
    tabla = _hoja_wanderlust(_df_polizas(), _df_facturacion(), contrato, date(2026, 7, 26))
    fila_corte = tabla[tabla["Categoría"].str.contains("2026-06-30", na=False)]
    assert len(fila_corte) == 1
    # A 30/06 solo cuenta P1 (fecha_efecto 2026-01-10); P2 es de 15/06... SÍ
    # cae dentro del corte (15/06 <= 30/06), así que ambas cuentan.
    assert fila_corte.iloc[0]["PAE (€)"] == pytest.approx(960.0)


def test_hoja_wanderlust_corte_excluye_polizas_posteriores(contrato):
    df_polizas = pd.DataFrame(
        [
            {"poliza": "P1", "razon_social": "ASISA PARTICULARES", "forma_pago": "M",
             "fecha_efecto": "2026-01-10", "situacion": "A", "fecha_baja": None},
            {"poliza": "P-JULIO", "razon_social": "ASISA PARTICULARES", "forma_pago": "M",
             "fecha_efecto": "2026-07-05", "situacion": "A", "fecha_baja": None},
        ]
    )
    df_facturacion = pd.DataFrame(
        [
            {"poliza": "P1", "prima_neta": 10.0, "periodo_liquidacion": "2026-01",
             "fecha_desde": "2026-01-10", "fecha_hasta": None},
            {"poliza": "P-JULIO", "prima_neta": 50.0, "periodo_liquidacion": "2026-07",
             "fecha_desde": "2026-07-05", "fecha_hasta": None},
        ]
    )
    tabla = _hoja_wanderlust(df_polizas, df_facturacion, contrato, date(2026, 7, 26))

    fila_total = tabla[tabla["Categoría"] == "TOTAL 2026"].iloc[0]
    assert fila_total["PAE (€)"] == pytest.approx(120.0 + 600.0)  # P1 + P-JULIO

    fila_corte = tabla[tabla["Categoría"].str.contains("2026-06-30", na=False)].iloc[0]
    assert fila_corte["PAE (€)"] == pytest.approx(120.0)  # solo P1, P-JULIO queda fuera


def test_hoja_wanderlust_corte_excluye_poliza_eiac_posterior(contrato):
    polizas = pd.DataFrame([{
        "poliza": "FUTURA-EIAC", "razon_social": "ASISA PARTICULARES",
        "forma_pago": "A", "fecha_efecto": "2026-09-01",
        "situacion": "A", "fecha_baja": None,
    }])
    eiac_polizas = pd.DataFrame([{
        "numero_poliza": "FUTURA-EIAC", "ramo_entidad": "RASA",
        "fecha_efecto_inicial": "2026-09-01",
        "prima_neta_anualizada_poli": 100.0,
    }])

    tabla = _hoja_wanderlust(
        polizas, pd.DataFrame(), contrato, date(2026, 9, 25),
        df_eiac_polizas=eiac_polizas,
    )

    total = tabla.loc[tabla["Categoría"] == "TOTAL 2026"].iloc[0]
    corte = tabla.loc[
        tabla["Categoría"] == "PAE acumulado a 2026-08-31 (mes anterior completo)"
    ].iloc[0]
    assert total["PAE (€)"] == pytest.approx(100.0)
    assert corte["PAE (€)"] == pytest.approx(0.0)
    assert corte["Altas"] == 0
    assert corte["Anuladas"] == 0


@pytest.mark.parametrize(
    "fecha_baja, pae_al_corte",
    [("2026-08-31", -100.0), ("2026-09-10", 100.0)],
)
def test_hoja_wanderlust_corte_aplica_anulacion_cuando_ocurre(
    contrato, fecha_baja, pae_al_corte
):
    polizas = pd.DataFrame([{
        "poliza": "BAJA", "razon_social": "ASISA PARTICULARES",
        "forma_pago": "A", "fecha_efecto": "2026-06-01",
        "situacion": "B", "fecha_baja": fecha_baja,
    }])
    facturacion = pd.DataFrame([{
        "poliza": "BAJA", "prima_neta": 100.0,
        "periodo_liquidacion": "2026-06", "fecha_desde": "2026-06-01",
        "fecha_hasta": "2026-12-31",
    }])

    tabla = _hoja_wanderlust(polizas, facturacion, contrato, date(2026, 9, 25))

    total = tabla.loc[tabla["Categoría"] == "TOTAL 2026", "PAE (€)"].iloc[0]
    corte = tabla.loc[
        tabla["Categoría"] == "PAE acumulado a 2026-08-31 (mes anterior completo)"
    ].iloc[0]
    assert total == pytest.approx(-100.0)
    assert corte["PAE (€)"] == pytest.approx(pae_al_corte)
    assert corte["Altas"] == (1 if pae_al_corte > 0 else 0)
    assert corte["Anuladas"] == (1 if pae_al_corte < 0 else 0)


def test_hoja_wanderlust_no_corte_en_enero(contrato):
    # En enero, el mes anterior completo (diciembre) es de OTRO año -> no
    # tiene sentido compararlo contra el PAE 2026, así que no se añade fila.
    tabla = _hoja_wanderlust(_df_polizas(), _df_facturacion(), contrato, date(2026, 1, 15))
    assert not tabla["Categoría"].str.contains("mes anterior completo", na=False).any()


def test_construir_excel_completo_incluye_hoja_wanderlust(contrato):
    excel_bytes = construir_excel_completo(
        df_polizas=_df_polizas(),
        df_facturacion=_df_facturacion(),
        df_liquidacion=pd.DataFrame(),
        df_factura_pdf=pd.DataFrame(),
        df_eiac_polizas=pd.DataFrame(),
        df_eiac_recibos=pd.DataFrame(),
        contrato=contrato,
        hoy=date(2026, 7, 26),
    )
    hojas = pd.read_excel(BytesIO(excel_bytes), sheet_name=None)
    assert "Wanderlust PAE" in hojas
    assert "TOTAL 2026" in set(hojas["Wanderlust PAE"]["Categoría"])
