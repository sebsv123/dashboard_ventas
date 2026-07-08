from pathlib import Path

from ingestion.facturacion import parsear_facturacion
from ingestion.liquidacion import parsear_liquidacion
from ingestion.polizas import parsear_polizas

FIXTURES = Path(__file__).parent / "fixtures"


def test_parsear_facturacion_basico():
    df = parsear_facturacion(FIXTURES / "facturacion_sample.csv")
    assert len(df) == 3
    assert df.loc[0, "prima_neta"] == 30.00
    # la póliza 70000003 tiene un recibo de ~12 meses (prepago anual)
    fila_anual = df[df["poliza"] == "70000003"].iloc[0]
    assert fila_anual["duracion_recibo_meses"] > 11


def test_parsear_polizas_forma_pago():
    df = parsear_polizas(FIXTURES / "polizas_sample.csv")
    assert len(df) == 3
    mensual = df[df["poliza"] == "70000001"].iloc[0]
    anual = df[df["poliza"] == "70000003"].iloc[0]
    assert mensual["forma_pago"] == "M"
    assert anual["forma_pago"] == "A"
    # fecha_efecto (columna FECHA ALTA) != fecha_emision (columna FECHA GRAB)
    assert anual["fecha_emision"] < anual["fecha_efecto"]


def test_parsear_liquidacion_basico():
    df = parsear_liquidacion(FIXTURES / "liquidacion_sample.csv")
    assert len(df) == 2
    fila_vida = df[df["razon_social"] == "ASISA VIDA TRANQUILIDAD"].iloc[0]
    assert fila_vida["comision"] == 7.20
    assert fila_vida["comision_pct"] == 60.0
