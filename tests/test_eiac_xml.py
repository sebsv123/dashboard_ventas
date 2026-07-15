from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from ingestion.eiac_xml import (
    detectar_tipo_eiac,
    parsear_eiac_polizas,
    parsear_eiac_polizas_riesgos,
    parsear_eiac_recibos,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parsear_eiac_polizas_basico():
    df = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    assert len(df) == 3

    fila = df[df["id_poliza"] == "0001234-9876541"].iloc[0]
    assert fila["cliente_codigo"] == "0001234"
    assert fila["numero_poliza"] == "9876541"
    assert fila["situacion_poliza"] == "EV"
    assert fila["clase_poliza"] == "NP"
    assert fila["fecha_efecto_inicial"] == date(2026, 6, 1)
    assert fila["fecha_emision"] == date(2026, 5, 20)
    assert fila["descripcion_riesgo"] == "JUAN PEREZ GARCIA"
    assert fila["descripcion_ramo"] == "Asistencia sanitaria"
    assert fila["codigo_entidad_interno"] == "Asisa"


def test_parsear_eiac_polizas_sin_datos_de_ramo_queda_none():
    # 9876542 no trae CodigoEntidad/DatosRamo en el fixture (simula una
    # póliza real donde ese bloque no viene, o no se ha visto todavía).
    df = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    fila = df[df["id_poliza"] == "0001234-9876542"].iloc[0]
    assert pd.isna(fila["descripcion_ramo"])
    assert pd.isna(fila["codigo_entidad_interno"])


def test_parsear_eiac_polizas_fecha_efecto_futura_no_se_filtra():
    # Caso real: pólizas con FechaEfectoInicial a varios meses vista
    # (septiembre) deben parsear igual que cualquier otra, sin descartarlas
    # ni tratarlas de forma especial — el parser no juzga fechas futuras.
    df = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    fila = df[df["id_poliza"] == "0001234-9876542"].iloc[0]
    assert fila["fecha_efecto_inicial"] == date(2026, 9, 15)
    assert fila["fecha_emision"] == date(2026, 7, 1)


def test_parsear_eiac_polizas_usa_riesgo_numero_orden_1_como_principal():
    # Poliza familiar con 2 asegurados: descripcion_riesgo debe ser el de
    # NumeroOrden=1, no "el primero que aparezca en el XML" a ciegas.
    df = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    fila = df[df["id_poliza"] == "0001234-9876542"].iloc[0]
    assert fila["descripcion_riesgo"] == "MARIA LOPEZ SANCHEZ"


def test_parsear_eiac_polizas_situacion_baja_tambien_se_incluye():
    df = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    fila = df[df["id_poliza"] == "0001234-9876543"].iloc[0]
    assert fila["situacion_poliza"] == "BJ"


def test_parsear_eiac_polizas_fichero_sin_registros_lanza_error(tmp_path):
    vacio = tmp_path / "vacio.xml"
    vacio.write_text(
        '<?xml version="1.0" encoding="ISO-8859-15"?>'
        '<ProcesosEIAC xmlns="http://www.tirea.es/EIAC/ProcesosEIAC"><Objetos/></ProcesosEIAC>'
    )
    with pytest.raises(ValueError, match="EIAC-ENV-POLI"):
        parsear_eiac_polizas(vacio)


def test_parsear_eiac_polizas_riesgos_incluye_todos_los_asegurados():
    df = parsear_eiac_polizas_riesgos(FIXTURES / "eiac_polizas_sample.xml")
    riesgos_9876542 = df[df["id_poliza"] == "0001234-9876542"]
    assert len(riesgos_9876542) == 2
    assert set(riesgos_9876542["descripcion_riesgo"]) == {
        "MARIA LOPEZ SANCHEZ", "PABLO LOPEZ SANCHEZ",
    }
    assert set(riesgos_9876542["numero_orden"]) == {"1", "2"}


def test_parsear_eiac_polizas_riesgos_poliza_de_un_solo_asegurado():
    df = parsear_eiac_polizas_riesgos(FIXTURES / "eiac_polizas_sample.xml")
    riesgos_9876541 = df[df["id_poliza"] == "0001234-9876541"]
    assert len(riesgos_9876541) == 1
    assert riesgos_9876541.iloc[0]["descripcion_riesgo"] == "JUAN PEREZ GARCIA"


def test_parsear_eiac_recibos_deduplica_co_sobre_pe_por_fecha_efecto():
    # Clave de deduplicación: (id_poliza, fecha_efecto_inicial), NO el
    # importe -- en datos reales el importe fluctúa unos céntimos entre
    # intentos del mismo recibo (ver docstring del módulo).
    df = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")

    recibos_junio = df[
        (df["id_poliza"] == "0001234-9876541") & (df["fecha_efecto_inicial"] == date(2026, 6, 1))
    ]
    assert len(recibos_junio) == 1
    assert recibos_junio.iloc[0]["situacion_recibo"] == "CO"
    # El importe que queda es el del intento CO (360.50), no el del PE
    # descartado (360.00) -- aunque no coincidan, gana el CO.
    assert recibos_junio.iloc[0]["prima_total"] == 360.50


def test_parsear_eiac_recibos_no_colapsa_fechas_efecto_distintas_de_la_misma_poliza():
    df = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")
    recibos_poliza = df[df["id_poliza"] == "0001234-9876541"]
    # Junio (deduplicado a 1 fila) + julio (recibo de otro mes) = 2 filas.
    assert len(recibos_poliza) == 2
    assert set(recibos_poliza["fecha_efecto_inicial"]) == {date(2026, 6, 1), date(2026, 7, 1)}


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
        '<ProcesosEIAC xmlns="http://www.tirea.es/EIAC/ProcesosEIAC"><Objetos/></ProcesosEIAC>'
    )
    with pytest.raises(ValueError, match="EIAC-ENV-RECI"):
        parsear_eiac_recibos(vacio)


def test_deduplicar_recibos_prioridad_no_depende_del_orden():
    # Aunque el CO viniera ANTES del PE en el fichero, el resultado debe ser
    # el mismo: CO gana siempre sobre PE para el mismo poliza+fecha_efecto.
    from ingestion.eiac_xml import _deduplicar_recibos

    df = pd.DataFrame(
        [
            {"id_poliza": "X", "fecha_efecto_inicial": date(2026, 1, 1), "situacion_recibo": "CO"},
            {"id_poliza": "X", "fecha_efecto_inicial": date(2026, 1, 1), "situacion_recibo": "PE"},
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


# --- caso real 64171931 (ASISA Travel and You): RamoEntidad + anulación ----

def test_parsear_eiac_polizas_ramo_entidad_travel_caso_real_64171931():
    df = parsear_eiac_polizas(FIXTURES / "eiac_polizas_64171931_alta.xml")
    fila = df[df["id_poliza"] == "23165-64171931"].iloc[0]
    assert fila["ramo_entidad"] == "RAVI"
    assert fila["descripcion_ramo"] == "Asistencia en viaje"
    assert fila["situacion_poliza"] == "EV"
    assert fila["clase_poliza"] == "NP"
    assert pd.isna(fila["fecha_anulacion"])


def test_parsear_eiac_polizas_anulacion_caso_real_64171931():
    # Fichero POSTERIOR real: ClasePoliza=AN, SituacionPoliza=EX, y
    # DatosAnulacion/FechaAnulacion -- confirmado que <DatosAnulacion> es
    # hermano de <Fechas>, no anidado dentro (ver fixture/docstring).
    df = parsear_eiac_polizas(FIXTURES / "eiac_polizas_64171931_anulacion.xml")
    fila = df[df["id_poliza"] == "23165-64171931"].iloc[0]
    assert fila["situacion_poliza"] == "EX"
    assert fila["clase_poliza"] == "AN"
    assert fila["fecha_anulacion"] == date(2026, 6, 19)
    assert fila["motivo_anulacion"] == "NI"
    assert fila["ramo_entidad"] == "RAVI"
