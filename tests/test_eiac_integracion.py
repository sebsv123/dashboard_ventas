from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from engine.config_contrato import cargar_contrato
from engine.eiac_integracion import (
    RAZON_SOCIAL_SALUD_POR_DEFECTO,
    RAZON_SOCIAL_TRAVEL,
    RAZON_SOCIAL_VIDA_POR_DEFECTO,
    construir_facturacion_desde_eiac,
    construir_polizas_provisionales_desde_eiac,
    integrar_eiac,
)
from engine.insights import primeras_altas_por_periodo, resumen_produccion_periodo
from ingestion.eiac_xml import (
    NumeroPolizaExtraido,
    extraer_numero_poliza_asisa,
    parsear_eiac_polizas,
    parsear_eiac_recibos,
)

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG_PATH = Path(__file__).parent.parent / "config" / "contrato.yaml"


@pytest.fixture
def contrato():
    return cargar_contrato(CONFIG_PATH)


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
    facturacion_eiac, no_reconocidos = construir_facturacion_desde_eiac(df_recibos, pd.DataFrame())

    assert no_reconocidos == []
    assert set(facturacion_eiac["poliza"]) == {"9876541", "9876542"}
    fila_junio = facturacion_eiac[
        (facturacion_eiac["poliza"] == "9876541") & (facturacion_eiac["prima_total"] == 360.50)
    ].iloc[0]
    assert fila_junio["periodo_liquidacion"] == "2026-06"  # mes calendario de FechaEfectoInicial
    assert fila_junio["prima_neta"] == 300.0


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
    facturacion_eiac, no_reconocidos = construir_facturacion_desde_eiac(df_recibos, pd.DataFrame())
    assert facturacion_eiac.empty
    assert len(no_reconocidos) == 1
    assert no_reconocidos[0].id_poliza_eiac == "24848-ABC"


def test_construir_facturacion_desde_eiac_vacio_no_revienta():
    facturacion_eiac, no_reconocidos = construir_facturacion_desde_eiac(pd.DataFrame(), pd.DataFrame())
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
    facturacion_eiac, _ = construir_facturacion_desde_eiac(df_recibos, df_facturacion_oficial)

    combinado = pd.concat([df_facturacion_oficial, facturacion_eiac], ignore_index=True)
    # No debe lanzar TypeError.
    resultado = primeras_altas_por_periodo(combinado)
    assert "70000001" in set(resultado["poliza"])
    assert "9876541" in set(resultado["poliza"])


def test_construir_facturacion_desde_eiac_csv_oficial_gana_caso_real_63946797():
    """Caso real que motivó esta regla (Sebastián, verificación manual del
    Objetivo anual): 63946797 tiene un recibo EIAC de 97,78€ con fecha de
    efecto 25/02/2026, pero el CSV oficial YA tiene esta póliza (recibo
    recurrente de 49,59€ desde abril). El CSV oficial gana siempre — EIAC
    se descarta por completo para esta póliza, no solo el importe menor.
    """
    df_eiac_recibos = pd.DataFrame(
        [
            {
                "id_poliza": "24300-63946797", "prima_total": 97.98, "prima_neta": 97.78,
                "situacion_recibo": "CO", "fecha_efecto_inicial": date(2026, 2, 25),
                "clase_forma_pago": "CC", "pista_forma_pago": "posible_mensual",
            }
        ]
    )
    df_facturacion_existente = pd.DataFrame(
        [
            {
                "poliza": "63946797", "periodo_liquidacion": "2026-04",
                "prima_neta": 49.59, "fecha_desde": pd.Timestamp("2026-03-25"),
            }
        ]
    )
    facturacion_eiac, no_reconocidos = construir_facturacion_desde_eiac(
        df_eiac_recibos, df_facturacion_existente
    )
    assert facturacion_eiac.empty
    assert no_reconocidos == []


def test_construir_facturacion_desde_eiac_no_afecta_polizas_solo_en_eiac():
    # El filtro de "CSV oficial gana" es por póliza, no global: una póliza
    # que NO está en el CSV oficial debe seguir apareciendo vía EIAC.
    df_eiac_recibos = pd.DataFrame(
        [
            {
                "id_poliza": "24300-99999999", "prima_total": 50.0, "prima_neta": 45.0,
                "situacion_recibo": "CO", "fecha_efecto_inicial": date(2026, 7, 1),
                "clase_forma_pago": "CC", "pista_forma_pago": "posible_mensual",
            }
        ]
    )
    df_facturacion_existente = pd.DataFrame(
        [{"poliza": "63946797", "periodo_liquidacion": "2026-04", "prima_neta": 49.59,
          "fecha_desde": pd.Timestamp("2026-03-25")}]
    )
    facturacion_eiac, _ = construir_facturacion_desde_eiac(df_eiac_recibos, df_facturacion_existente)
    assert set(facturacion_eiac["poliza"]) == {"99999999"}


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
    assert fila["situacion"] == "A"  # EIAC "EV" (en vigor) -> ASISA "A"
    assert fila["fecha_efecto"] == date(2026, 9, 15)  # varios meses vista, no se filtra

    fila_baja = provisionales[provisionales["poliza"] == "9876543"].iloc[0]
    assert fila_baja["situacion"] == "B"  # EIAC "BJ" -> ASISA "B"


def test_construir_polizas_provisionales_no_usa_clase_poliza_como_producto():
    # ClasePoliza es un código de TRANSACCIÓN (NP/SU/AN), NO el ramo del
    # producto (confirmado con 8 ficheros reales) -- producto_base debe
    # quedar sin confirmar, nunca inferido de ahí.
    df_polizas_eiac = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    provisionales, _ = construir_polizas_provisionales_desde_eiac(
        df_polizas_eiac, pd.DataFrame(), pd.DataFrame()
    )
    fila = provisionales[provisionales["poliza"] == "9876541"].iloc[0]
    assert fila["producto_base"] is None


def test_construir_polizas_provisionales_asume_particulares_cuando_confirma_salud():
    # 9876541 SÍ trae DescripcionRamo="Asistencia sanitaria" y
    # CodigoEntidad/CodigoInterno="Asisa" (ver fixture) -> se asume ASISA
    # PARTICULARES en vez de dejar la comisión en 0€, marcado como asumido.
    df_polizas_eiac = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    provisionales, _ = construir_polizas_provisionales_desde_eiac(
        df_polizas_eiac, pd.DataFrame(), pd.DataFrame()
    )
    fila = provisionales[provisionales["poliza"] == "9876541"].iloc[0]
    assert fila["razon_social"] == RAZON_SOCIAL_SALUD_POR_DEFECTO
    assert bool(fila["razon_social_asumida"]) is True
    assert "Producto exacto no confirmado" in fila["nota_origen"]
    assert RAZON_SOCIAL_SALUD_POR_DEFECTO in fila["nota_origen"]


def test_construir_polizas_provisionales_sin_senal_de_ramo_no_asume_nada():
    # 9876542 no trae DescripcionRamo ni CodigoEntidad (ver fixture) ->
    # razon_social se queda en None, nada asumido.
    df_polizas_eiac = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    provisionales, _ = construir_polizas_provisionales_desde_eiac(
        df_polizas_eiac, pd.DataFrame(), pd.DataFrame()
    )
    fila = provisionales[provisionales["poliza"] == "9876542"].iloc[0]
    assert pd.isna(fila["razon_social"])
    assert bool(fila["razon_social_asumida"]) is False
    assert "no se asume ningún % de comisión" in fila["nota_origen"]


def test_construir_polizas_provisionales_vacio_no_revienta():
    provisionales, no_reconocidos = construir_polizas_provisionales_desde_eiac(
        pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    )
    assert provisionales.empty
    assert no_reconocidos == []


# --- integrar_eiac (punto de entrada único) ---------------------------------

def test_integrar_eiac_combina_no_reconocidos_de_ambas_fuentes():
    df_polizas_eiac = pd.DataFrame(
        [{"id_poliza": "24848-BADPOLI", "situacion_poliza": "EV", "clase_poliza": "NP",
          "fecha_efecto_inicial": date(2026, 6, 1), "fecha_emision": date(2026, 5, 1),
          "descripcion_riesgo": "X", "cliente_codigo": "24848"}]
    )
    df_recibos_eiac = pd.DataFrame(
        [{"id_poliza": "24848-BADPOLI", "prima_total": 100.0, "prima_neta": 90.0,
          "situacion_recibo": "CO", "fecha_efecto_inicial": date(2026, 6, 1),
          "clase_forma_pago": "CC", "pista_forma_pago": "posible_mensual"}]
    )
    resultado = integrar_eiac(df_polizas_eiac, df_recibos_eiac, pd.DataFrame(), pd.DataFrame())
    assert resultado.facturacion_eiac.empty
    assert resultado.polizas_provisionales.empty
    # El mismo IdPoliza mal formado aparece en ambas fuentes -> se reporta una sola vez.
    assert len(resultado.no_reconocidos) == 1
    assert resultado.no_reconocidos[0].id_poliza_eiac == "24848-BADPOLI"


def test_integrar_eiac_extremo_a_extremo_con_fixtures():
    df_polizas_eiac = parsear_eiac_polizas(FIXTURES / "eiac_polizas_sample.xml")
    df_recibos_eiac = parsear_eiac_recibos(FIXTURES / "eiac_recibos_sample.xml")

    resultado = integrar_eiac(df_polizas_eiac, df_recibos_eiac, pd.DataFrame(), pd.DataFrame())

    assert set(resultado.facturacion_eiac["poliza"]) == {"9876541", "9876542"}
    assert set(resultado.polizas_provisionales["poliza"]) == {"9876541", "9876542", "9876543"}
    assert resultado.no_reconocidos == []


def test_integrar_eiac_csv_oficial_gana_periodo_e_importe_caso_real_63946797(contrato):
    """Caso real de extremo a extremo: 63946797 ya existe en Pólizas Y
    Facturación oficiales — integrar_eiac no debe generar NADA para ella
    (ni fila de facturación EIAC ni póliza provisional), y el importe que
    debe prevalecer al calcular altas es el del CSV oficial (49,59€), no
    el de EIAC (97,78€).
    """
    df_eiac_polizas = pd.DataFrame(
        [{"id_poliza": "24300-63946797", "situacion_poliza": "EV", "clase_poliza": "NP",
          "fecha_efecto_inicial": date(2026, 2, 25), "fecha_emision": date(2026, 2, 25),
          "descripcion_riesgo": None, "cliente_codigo": "24300",
          "descripcion_ramo": "Asistencia sanitaria", "codigo_entidad_interno": "Asisa"}]
    )
    df_eiac_recibos = pd.DataFrame(
        [{"id_poliza": "24300-63946797", "prima_total": 97.98, "prima_neta": 97.78,
          "situacion_recibo": "CO", "fecha_efecto_inicial": date(2026, 2, 25),
          "clase_forma_pago": "CC", "pista_forma_pago": "posible_mensual"}]
    )
    df_polizas_existente = pd.DataFrame(
        [{"poliza": "63946797", "forma_pago": "M", "razon_social": "ASISA PARTICULARES"}]
    )
    df_facturacion_existente = pd.DataFrame(
        [{"poliza": "63946797", "periodo_liquidacion": "2026-04",
          "prima_neta": 49.59, "fecha_desde": pd.Timestamp("2026-03-25")}]
    )

    resultado = integrar_eiac(
        df_eiac_polizas, df_eiac_recibos, df_polizas_existente, df_facturacion_existente
    )
    assert resultado.facturacion_eiac.empty
    assert resultado.polizas_provisionales.empty

    combinado = pd.concat([df_facturacion_existente, resultado.facturacion_eiac], ignore_index=True)
    altas = primeras_altas_por_periodo(combinado)
    fila = altas[altas["poliza"] == "63946797"].iloc[0]
    assert fila["prima_neta"] == pytest.approx(49.59)
    assert fila["periodo_liquidacion"] == "2026-04"


# --- caso real 64171931 (ASISA Travel and You): detección Travel + anulación

def test_construir_polizas_provisionales_detecta_travel_por_ramo_entidad_caso_real():
    # RamoEntidad="RAVI" (caso real 64171931) debe usar "ASISA TRAVEL AND
    # YOU" (20%/0%) en vez del default de Salud (25%/20% de Particulares) --
    # aunque CodigoEntidad/CodigoInterno="Asisa" también dispararía
    # _es_salud_por_ramo, la señal de Travel es más específica y se
    # comprueba primero.
    df_polizas_eiac = parsear_eiac_polizas(FIXTURES / "eiac_polizas_64171931_alta.xml")
    provisionales, no_reconocidos = construir_polizas_provisionales_desde_eiac(
        df_polizas_eiac, pd.DataFrame(), pd.DataFrame()
    )
    assert no_reconocidos == []
    fila = provisionales[provisionales["poliza"] == "64171931"].iloc[0]
    assert fila["razon_social"] == RAZON_SOCIAL_TRAVEL
    assert bool(fila["razon_social_asumida"]) is True
    assert "RamoEntidad='RAVI'" in fila["nota_origen"]
    assert fila["situacion"] == "A"


def test_construir_polizas_provisionales_anulacion_actualiza_situacion_caso_real():
    # El mismo IdPoliza llega en un fichero POSTERIOR con
    # ClasePoliza=AN/SituacionPoliza=EX -- debe mapearse a situacion="B",
    # no quedarse con el código EIAC crudo "EX".
    df_polizas_eiac_anulada = parsear_eiac_polizas(FIXTURES / "eiac_polizas_64171931_anulacion.xml")
    provisionales, _ = construir_polizas_provisionales_desde_eiac(
        df_polizas_eiac_anulada, pd.DataFrame(), pd.DataFrame()
    )
    fila = provisionales[provisionales["poliza"] == "64171931"].iloc[0]
    assert fila["situacion"] == "B"
    # Sigue detectando Travel correctamente aunque venga del fichero de anulación.
    assert fila["razon_social"] == RAZON_SOCIAL_TRAVEL


# --- caso real 22594-64358396 (Elias David Gonzalez Pacheco, ASISA VIDA):
# detección de Vida por RamoEntidad

def test_construir_polizas_provisionales_asume_tranquilidad_cuando_confirma_vida_caso_real():
    # RamoEntidad="VIDA" (caso real 22594-64358396, entidad ASISA VIDA,
    # efecto 2026-08-05, prima_neta ~38,26€/mes) debe usar "ASISA VIDA
    # TRANQUILIDAD" (60%/20%, el producto de Vida más habitual en la
    # cartera) en vez de dejar razon_social en None.
    df_eiac_polizas = pd.DataFrame(
        [{"id_poliza": "22594-64358396", "situacion_poliza": "EV", "clase_poliza": "NP",
          "fecha_efecto_inicial": date(2026, 8, 5), "fecha_emision": date(2026, 7, 20),
          "descripcion_riesgo": "Elias David Gonzalez Pacheco", "cliente_codigo": "22594",
          "ramo_entidad": "VIDA", "descripcion_ramo": "Vida", "codigo_entidad_interno": "Asisa"}]
    )
    provisionales, no_reconocidos = construir_polizas_provisionales_desde_eiac(
        df_eiac_polizas, pd.DataFrame(), pd.DataFrame()
    )
    assert no_reconocidos == []
    fila = provisionales[provisionales["poliza"] == "64358396"].iloc[0]
    assert fila["razon_social"] == RAZON_SOCIAL_VIDA_POR_DEFECTO
    assert bool(fila["razon_social_asumida"]) is True
    assert "Producto exacto no confirmado" in fila["nota_origen"]
    assert RAZON_SOCIAL_VIDA_POR_DEFECTO in fila["nota_origen"]
    assert "RamoEntidad='VIDA'" in fila["nota_origen"]


def test_poliza_anulada_con_recibo_no_cuenta_produccion_del_periodo(contrato):
    """Investigación pedida por Sebastián: ¿la producción de junio sigue
    contando una póliza anulada por EIAC como viva?

    La póliza real 64171931 NO tiene ningún recibo en los ficheros RECI
    reales (confirmado a mano) -- así que nunca generó producción, con o
    sin este fix. Este test es la parte SINTÉTICA (marcada explícitamente
    como tal): simula que si hubiera tenido un recibo, antes de este fix
    ese recibo SÍ se habría contado igual que el de cualquier póliza
    activa -- el bug real que había que confirmar/corregir en el punto 2.
    """
    df_polizas_eiac_anulada = parsear_eiac_polizas(FIXTURES / "eiac_polizas_64171931_anulacion.xml")
    provisionales, _ = construir_polizas_provisionales_desde_eiac(
        df_polizas_eiac_anulada, pd.DataFrame(), pd.DataFrame()
    )
    assert provisionales.loc[provisionales["poliza"] == "64171931", "situacion"].iloc[0] == "B"

    # Recibo SINTÉTICO (la póliza real no tuvo ninguno) para poder probar
    # el filtro de producción de extremo a extremo.
    df_facturacion = pd.DataFrame(
        [{"poliza": "64171931", "periodo_liquidacion": "2026-06",
          "prima_neta": 123.43, "fecha_desde": pd.Timestamp("2026-06-10")}]
    )
    resumen_junio = resumen_produccion_periodo(provisionales, df_facturacion, contrato, "2026-06")
    assert resumen_junio.tiene_datos is True
    assert resumen_junio.polizas_detectadas == 0
    assert resumen_junio.produccion_salud == 0.0


def test_produccion_julio_coincide_con_calculo_manual_de_los_5_casos_reales(contrato):
    # 5 casos reales cruzados a mano por Sebastián (conversación):
    #   1) 24300-64174100: 89.70 CC, efecto 30/06/2026  -> mensual, ciclo 16-15 -> julio
    #   2) 24300-64201679: 126.00 CC, efecto 30/06/2026 -> mensual, ciclo 16-15 -> julio
    #   3) 24300-64226440: 148.20 CC, efecto 01/07/2026 -> mensual -> julio
    #   4) 24300-64261922: 49.20 CC, efecto 07/07/2026  -> mensual -> julio (Cristina Romero Alpuente)
    #   5) 24848-64276918: 1409.40 TA, efecto 01/07/2026 -> prepago anual -> julio (Residents)
    # Cálculo manual verificado: 1076.40 + 1512.00 + 1778.40 + 590.40 + 1409.40 = 6366.60€
    # Sin CSV oficial para ninguna de las 5 en este test aislado, así que
    # el filtro "CSV oficial gana" no descarta nada aquí.
    df_recibos = parsear_eiac_recibos(FIXTURES / "eiac_recibos_julio_real.xml")
    df_polizas_eiac = parsear_eiac_polizas(FIXTURES / "eiac_polizas_julio_real.xml")

    resultado = integrar_eiac(df_polizas_eiac, df_recibos, pd.DataFrame(), pd.DataFrame())
    assert resultado.no_reconocidos == []
    assert set(resultado.polizas_provisionales["poliza"]) == {
        "64174100", "64201679", "64226440", "64261922", "64276918",
    }

    # Las 2 pólizas con efecto 30/06 deben caer en julio (ciclo 16->15), no en junio.
    periodos_por_poliza = dict(
        zip(resultado.facturacion_eiac["poliza"], resultado.facturacion_eiac["periodo_liquidacion"])
    )
    assert periodos_por_poliza["64174100"] == "2026-07"
    assert periodos_por_poliza["64201679"] == "2026-07"

    resumen_julio = resumen_produccion_periodo(
        resultado.polizas_provisionales, resultado.facturacion_eiac, contrato, "2026-07"
    )
    assert resumen_julio.polizas_detectadas == 5
    assert resumen_julio.produccion_salud == pytest.approx(6366.60, abs=0.01)
