from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from ingestion.eiac_xml import (
    detectar_tipo_eiac,
    parsear_eiac_polizas,
    parsear_eiac_recibos,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parsear_eiac_polizas_basico():
    df = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    assert len(df) == 3

    fila = df[df["id_poliza"] == "0001234-9876541"].iloc[0]
    assert fila["cliente_codigo"] == "0001234"
    assert fila["numero_poliza"] == "9876541"
    assert fila["situacion_poliza"] == "EF"
    assert fila["clase_poliza"] == "SALUD"
    assert fila["fecha_efecto_inicial"] == date(2026, 6, 1)
    assert fila["fecha_emision"] == date(2026, 5, 20)
    assert fila["descripcion_riesgo"] == "JUAN PEREZ GARCIA"


def test_parsear_eiac_polizas_fecha_efecto_futura_no_se_filtra():
    # Caso real: pólizas con FechaEfectoInicial a varios meses vista
    # (septiembre) deben parsear igual que cualquier otra, sin descartarlas
    # ni tratarlas de forma especial — el parser no juzga fechas futuras.
    df = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    fila = df[df["id_poliza"] == "0001234-9876542"].iloc[0]
    assert fila["fecha_efecto_inicial"] == date(2026, 9, 15)
    assert fila["fecha_emision"] == date(2026, 7, 1)


def test_parsear_eiac_polizas_situacion_baja_tambien_se_incluye():
    df = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    fila = df[df["id_poliza"] == "0001234-9876543"].iloc[0]
    assert fila["situacion_poliza"] == "BJ"


def test_parsear_eiac_polizas_fichero_sin_registros_lanza_error(tmp_path):
    vacio = tmp_path / "vacio.xml"
    vacio.write_text(
        '<?xml version="1.0" encoding="ISO-8859-15"?>'
        '<EIAC xmlns="http://www.tirea.es/EIAC/ProcesosEIAC"><Polizas/></EIAC>'
    )
    with pytest.raises(ValueError, match="EIAC-ENV-POLI"):
        parsear_eiac_polizas(vacio)


def test_parsear_eiac_recibos_deduplica_co_sobre_pe():
    df = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")

    recibos_360 = df[(df["id_poliza"] == "0001234-9876541") & (df["prima_total"] == 360.0)]
    assert len(recibos_360) == 1
    assert recibos_360.iloc[0]["situacion_recibo"] == "CO"


def test_parsear_eiac_recibos_no_colapsa_importes_distintos_de_la_misma_poliza():
    df = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")
    recibos_poliza = df[df["id_poliza"] == "0001234-9876541"]
    # 360.00 (deduplicado a 1 fila) + 30.00 (recibo mensual distinto) = 2 filas.
    assert len(recibos_poliza) == 2
    assert set(recibos_poliza["prima_total"]) == {360.0, 30.0}


def test_parsear_eiac_recibos_pendiente_sin_confirmar_se_mantiene_pe():
    df = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")
    fila = df[df["id_poliza"] == "0001234-9876542"].iloc[0]
    assert fila["situacion_recibo"] == "PE"
    assert fila["prima_total"] == 300.0


def test_parsear_eiac_recibos_pista_forma_pago():
    df = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")
    domiciliado = df[df["clase_forma_pago"] == "CC"].iloc[0]
    tarjeta = df[df["clase_forma_pago"] == "TA"].iloc[0]
    assert domiciliado["pista_forma_pago"] == "posible_mensual"
    assert tarjeta["pista_forma_pago"] == "posible_prepago_anual"


def test_parsear_eiac_recibos_fichero_sin_registros_lanza_error(tmp_path):
    vacio = tmp_path / "vacio.xml"
    vacio.write_text(
        '<?xml version="1.0" encoding="ISO-8859-15"?>'
        '<EIAC xmlns="http://www.tirea.es/EIAC/ProcesosEIAC"><Recibos/></EIAC>'
    )
    with pytest.raises(ValueError, match="EIAC-ENV-RECI"):
        parsear_eiac_recibos(vacio)


def test_deduplicar_recibos_prioridad_no_depende_del_orden():
    # Aunque el CO viniera ANTES del PE en el fichero, el resultado debe ser
    # el mismo: CO gana siempre sobre PE para el mismo poliza+importe.
    import pandas as pd
    from ingestion.eiac_xml import _deduplicar_recibos

    df = pd.DataFrame(
        [
            {"id_poliza": "X", "prima_total": 100.0, "situacion_recibo": "CO"},
            {"id_poliza": "X", "prima_total": 100.0, "situacion_recibo": "PE"},
        ]
    )
    resultado = _deduplicar_recibos(df)
    assert len(resultado) == 1
    assert resultado.iloc[0]["situacion_recibo"] == "CO"


@pytest.mark.parametrize(
    "nombre,esperado",
    [
        ("EIAC-ENV-POLI-20260714.xml", "polizas"),
        ("EIAC-ENV-RECI-20260714.xml", "recibos"),
        ("eiac-env-poli-minusculas.xml", "polizas"),
    ],
)
def test_detectar_tipo_eiac(nombre, esperado):
    assert detectar_tipo_eiac(nombre) == esperado


def test_detectar_tipo_eiac_desconocido_lanza_error():
    with pytest.raises(ValueError, match="No se reconoce"):
        detectar_tipo_eiac("otro_fichero.xml")
