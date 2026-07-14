from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from engine.eiac_integracion import (
    construir_facturacion_desde_eiac,
    construir_polizas_provisionales_desde_eiac,
    integrar_eiac,
)
from engine.insights import primeras_altas_por_periodo
from ingestion.eiac_xml import (
    NumeroPolizaExtraido,
    extraer_numero_poliza_asisa,
    parsear_eiac_polizas,
    parsear_eiac_recibos,
)

FIXTURES = Path(__file__).parent / "fixtures"


# --- extraer_numero_poliza_asisa --------------------------------------------

def test_extraer_numero_poliza_asisa_caso_confirmado():
    resultado = extraer_numero_poliza_asisa("24848-64276918")
    assert resultado.reconocido is True
    assert resultado.numero_poliza == "64276918"


def test_extraer_numero_poliza_asisa_usa_el_ultimo_guion():
    # codigo_cliente con guion propio: solo la parte final es el nº de póliza.
    resultado = extraer_numero_poliza_asisa("248-48-64276918")
    assert resultado.reconocido is True
    assert resultado.numero_poliza == "64276918"


def test_extraer_numero_poliza_asisa_no_numerico_no_se_descarta_en_silencio():
    resultado = extraer_numero_poliza_asisa("24848-ABC1234")
    assert resultado.reconocido is False
    assert resultado.numero_poliza == "ABC1234"
    assert "no es puramente numérico" in resultado.motivo


def test_extraer_numero_poliza_asisa_longitud_fuera_de_rango():
    resultado = extraer_numero_poliza_asisa("24848-123")
    assert resultado.reconocido is False
    assert "no es puramente numérico" in resultado.motivo


def test_extraer_numero_poliza_asisa_sin_guion():
    resultado = extraer_numero_poliza_asisa("SINGUION1234567")
    assert resultado.reconocido is False
    assert resultado.numero_poliza is None


# --- construir_facturacion_desde_eiac ---------------------------------------

def test_construir_facturacion_desde_eiac_forma_correcta():
    df_recibos = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")
    facturacion_eiac, no_reconocidos = construir_facturacion_desde_eiac(df_recibos)

    assert no_reconocidos == []
    assert set(facturacion_eiac["poliza"]) == {"9876541", "9876542"}
    fila_360 = facturacion_eiac[facturacion_eiac["prima_total"] == 360.0].iloc[0]
    assert fila_360["poliza"] == "9876541"
    assert fila_360["periodo_liquidacion"] == "2026-06"  # mes calendario de FechaEfectoInicial
    assert fila_360["prima_neta"] == 300.0


def test_construir_facturacion_desde_eiac_registra_no_reconocidos():
    df_recibos = pd.DataFrame(
        [
            {
                "id_poliza": "24848-ABC", "prima_total": 100.0, "prima_neta": 90.0,
                "situacion_recibo": "CO", "fecha_efecto_inicial": date(2026, 6, 1),
                "clase_forma_pago": "CC", "pista_forma_pago": "posible_mensual",
            }
        ]
    )
    facturacion_eiac, no_reconocidos = construir_facturacion_desde_eiac(df_recibos)
    assert facturacion_eiac.empty
    assert len(no_reconocidos) == 1
    assert no_reconocidos[0].id_poliza_eiac == "24848-ABC"


def test_construir_facturacion_desde_eiac_vacio_no_revienta():
    facturacion_eiac, no_reconocidos = construir_facturacion_desde_eiac(pd.DataFrame())
    assert facturacion_eiac.empty
    assert no_reconocidos == []


def test_facturacion_eiac_se_puede_concatenar_con_facturacion_oficial_y_ordenar():
    # Regresión: df_facturacion (leído con pd.read_sql(parse_dates=...)) usa
    # pd.Timestamp en fecha_desde; si facturacion_eiac usara datetime.date
    # sin más, pd.concat mezcla los dos tipos en la misma columna 'object' y
    # sort_values (dentro de primeras_altas_por_periodo) revienta con
    # TypeError: 'values' is not ordered.
    df_facturacion_oficial = pd.DataFrame(
        [
            {
                "poliza": "70000001", "periodo_liquidacion": "2026-06",
                "prima_neta": 30.0, "fecha_desde": pd.Timestamp("2026-06-01"),
            }
        ]
    )
    df_recibos = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")
    facturacion_eiac, _ = construir_facturacion_desde_eiac(df_recibos)

    combinado = pd.concat([df_facturacion_oficial, facturacion_eiac], ignore_index=True)
    # No debe lanzar TypeError.
    resultado = primeras_altas_por_periodo(combinado)
    assert "70000001" in set(resultado["poliza"])
    assert "9876541" in set(resultado["poliza"])


# --- construir_polizas_provisionales_desde_eiac -----------------------------

def test_construir_polizas_provisionales_crea_solo_las_que_faltan():
    df_polizas_eiac = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    df_recibos_eiac = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")
    df_polizas_existente = pd.DataFrame([{"poliza": "9876541"}])  # ya confirmada por CSV oficial

    provisionales, no_reconocidos = construir_polizas_provisionales_desde_eiac(
        df_polizas_eiac, df_recibos_eiac, df_polizas_existente
    )
    assert no_reconocidos == []
    # 9876541 ya existe -> no se crea provisional para ella.
    assert "9876541" not in set(provisionales["poliza"])
    # 9876542 y 9876543 no existen -> sí se crean.
    assert set(provisionales["poliza"]) == {"9876542", "9876543"}


def test_construir_polizas_provisionales_marca_origen_y_nota():
    df_polizas_eiac = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    df_recibos_eiac = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")

    provisionales, _ = construir_polizas_provisionales_desde_eiac(
        df_polizas_eiac, df_recibos_eiac, pd.DataFrame()
    )
    fila = provisionales[provisionales["poliza"] == "9876542"].iloc[0]
    assert fila["origen"] == "EIAC"
    assert "pendiente de confirmar" in fila["nota_origen"]
    # 9876542 tiene un recibo con ClaseFormaPago=TA -> pista prepago anual -> forma_pago='A'.
    assert fila["forma_pago"] == "A"
    assert "pista_forma_pago" in fila["nota_origen"]


def test_construir_polizas_provisionales_situacion_y_fecha_efecto_futura():
    df_polizas_eiac = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    provisionales, _ = construir_polizas_provisionales_desde_eiac(
        df_polizas_eiac, pd.DataFrame(), pd.DataFrame()
    )
    fila = provisionales[provisionales["poliza"] == "9876542"].iloc[0]
    assert fila["situacion"] == "A"  # EIAC "EF" -> ASISA "A"
    assert fila["fecha_efecto"] == date(2026, 9, 15)  # varios meses vista, no se filtra

    fila_baja = provisionales[provisionales["poliza"] == "9876543"].iloc[0]
    assert fila_baja["situacion"] == "B"  # EIAC "BJ" -> ASISA "B"


def test_construir_polizas_provisionales_vacio_no_revienta():
    provisionales, no_reconocidos = construir_polizas_provisionales_desde_eiac(
        pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    )
    assert provisionales.empty
    assert no_reconocidos == []


# --- integrar_eiac (punto de entrada único) ---------------------------------

def test_integrar_eiac_combina_no_reconocidos_de_ambas_fuentes():
    df_polizas_eiac = pd.DataFrame(
        [{"id_poliza": "24848-BADPOLI", "situacion_poliza": "EF", "clase_poliza": "SALUD",
          "fecha_efecto_inicial": date(2026, 6, 1), "fecha_emision": date(2026, 5, 1),
          "descripcion_riesgo": "X", "cliente_codigo": "24848"}]
    )
    df_recibos_eiac = pd.DataFrame(
        [{"id_poliza": "24848-BADPOLI", "prima_total": 100.0, "prima_neta": 90.0,
          "situacion_recibo": "CO", "fecha_efecto_inicial": date(2026, 6, 1),
          "clase_forma_pago": "CC", "pista_forma_pago": "posible_mensual"}]
    )
    resultado = integrar_eiac(df_polizas_eiac, df_recibos_eiac, pd.DataFrame())
    assert resultado.facturacion_eiac.empty
    assert resultado.polizas_provisionales.empty
    # El mismo IdPoliza mal formado aparece en ambas fuentes -> se reporta una sola vez.
    assert len(resultado.no_reconocidos) == 1
    assert resultado.no_reconocidos[0].id_poliza_eiac == "24848-BADPOLI"


def test_integrar_eiac_extremo_a_extremo_con_fixtures():
    df_polizas_eiac = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    df_recibos_eiac = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")

    resultado = integrar_eiac(df_polizas_eiac, df_recibos_eiac, pd.DataFrame())

    assert set(resultado.facturacion_eiac["poliza"]) == {"9876541", "9876542"}
    assert set(resultado.polizas_provisionales["poliza"]) == {"9876541", "9876542", "9876543"}
    assert resultado.no_reconocidos == []


@pytest.mark.skip(
    reason=(
        "PENDIENTE: falta el dato real (IdPoliza completo, PrimaTotal/PrimaNeta, "
        "FechaEfectoInicial, ClaseFormaPago, intentos CO/PE) de las 5 pólizas "
        "reales cruzadas a mano por Sebastián (64276918, 64254004, 64254007, "
        "64261922, 64201679/64174100) para construir el fixture EIAC real y "
        "comprobar que la producción de julio calculada por integrar_eiac() + "
        "resumen_produccion_periodo() coincide con los 6.366,60€ verificados a "
        "mano. NO se ha inventado ese dato — cuando esté disponible, sustituir "
        "este test por uno con los valores reales (ver conversación)."
    )
)
def test_produccion_julio_coincide_con_calculo_manual_de_los_5_casos_reales():
    pass
