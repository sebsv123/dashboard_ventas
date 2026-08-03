import ast
from pathlib import Path

import pandas as pd
import pytest

from engine.calibracion import calcular_calibracion, estimar_comision_y_rappel_periodo
from engine.comisiones import aplicar_retencion, estimar_comision_poliza
from engine.config_contrato import cargar_contrato
from engine.rappel import calcular_rappel_inicial
from datetime import date

CONFIG_PATH = Path(__file__).parent.parent / "config" / "contrato.yaml"
APP_SRC_PATH = Path(__file__).parent.parent / "src" / "dashboard" / "app.py"


@pytest.fixture
def contrato():
    return cargar_contrato(CONFIG_PATH)


def _df_polizas():
    return pd.DataFrame(
        [
            # Enero: una salud anual (P1) y una vida mensual (P2)
            {"poliza": "P1", "forma_pago": "A", "razon_social": "ASISA PARTICULARES",
             "fecha_efecto": date(2026, 1, 10), "situacion": "A"},
            {"poliza": "P2", "forma_pago": "M", "razon_social": "ASISA VIDA TRANQUILIDAD",
             "fecha_efecto": date(2026, 1, 12), "situacion": "A"},
            # Febrero: salud mensual (P3)
            {"poliza": "P3", "forma_pago": "M", "razon_social": "ASISA PARTICULARES",
             "fecha_efecto": date(2026, 2, 5), "situacion": "A"},
            # Marzo: salud mensual (P4), pero SU fila de Pólizas no llegará
            # a estar en df_facturacion de marzo (simula mes sin Facturación).
            {"poliza": "P4", "forma_pago": "M", "razon_social": "ASISA PARTICULARES",
             "fecha_efecto": date(2026, 3, 5), "situacion": "A"},
            # Mayo: alta que NO tiene fila de Pólizas (simula Pólizas
            # desactualizada para ese periodo) -> P5 no está en este DataFrame.
        ]
    )


def _df_facturacion():
    return pd.DataFrame(
        [
            {"poliza": "P1", "periodo_liquidacion": "2026-01", "prima_neta": 500.0, "fecha_desde": "2026-01-10"},
            {"poliza": "P2", "periodo_liquidacion": "2026-01", "prima_neta": 12.0, "fecha_desde": "2026-01-12"},
            {"poliza": "P3", "periodo_liquidacion": "2026-02", "prima_neta": 30.0, "fecha_desde": "2026-02-05"},
            # Mayo: P5 no cruza con Pólizas -> mes incompleto, debe excluirse.
            {"poliza": "P5", "periodo_liquidacion": "2026-05", "prima_neta": 20.0, "fecha_desde": "2026-05-01"},
        ]
    )


def _df_factura_pdf():
    return pd.DataFrame(
        [
            {"periodo": "2026-01", "entidad_nombre": "ASISA Salud", "total_factura": 300.0},
            {"periodo": "2026-02", "entidad_nombre": "ASISA Salud", "total_factura": 200.0},
            # Marzo: hay Factura PDF real pero NO Facturación de marzo (ver
            # _df_facturacion) -> debe excluirse, no contar como "fallo".
            {"periodo": "2026-03", "entidad_nombre": "ASISA Salud", "total_factura": 999.0},
            # Mayo: hay Factura PDF real, hay Facturación, pero la póliza P5
            # no cruza con Pólizas -> también debe excluirse.
            {"periodo": "2026-05", "entidad_nombre": "ASISA Salud", "total_factura": 500.0},
        ]
    )


def _estimado_esperado_enero(contrato) -> float:
    fila_p1 = pd.Series(
        {"poliza": "P1", "forma_pago": "A", "razon_social": "ASISA PARTICULARES",
         "fecha_efecto": date(2026, 1, 10)}
    )
    fila_p2 = pd.Series(
        {"poliza": "P2", "forma_pago": "M", "razon_social": "ASISA VIDA TRANQUILIDAD",
         "fecha_efecto": date(2026, 1, 12)}
    )
    est_p1 = estimar_comision_poliza(
        fila_p1, contrato, prima_anual=500.0, fecha_referencia=date(2026, 1, 1)
    )
    est_p2 = estimar_comision_poliza(
        fila_p2, contrato, prima_anual=144.0, prima_recibo_mensual=12.0,
        fecha_referencia=date(2026, 1, 1),
    )
    rappel = calcular_rappel_inicial(
        contrato, fecha_referencia=date(2026, 1, 1), produccion_mes_salud=500.0
    )
    bruto = est_p1.comision_bruta_estimada + est_p2.comision_bruta_estimada + rappel.importe
    return aplicar_retencion(bruto, contrato)


def test_calcula_estimado_y_real_para_periodos_completos(contrato):
    resultado = calcular_calibracion(_df_polizas(), _df_facturacion(), _df_factura_pdf(), contrato)

    periodos = {p.mes: p for p in resultado.periodos}
    assert set(periodos) == {"2026-01", "2026-02"}

    esperado_enero = _estimado_esperado_enero(contrato)
    assert periodos["2026-01"].estimado == pytest.approx(esperado_enero)
    assert periodos["2026-01"].real == 300.0
    assert periodos["2026-01"].diferencia == pytest.approx(round(esperado_enero - 300.0, 2))


def test_excluye_periodo_sin_facturacion_con_nota(contrato):
    resultado = calcular_calibracion(_df_polizas(), _df_facturacion(), _df_factura_pdf(), contrato)

    excluidos_por_periodo = {e.periodo: e for e in resultado.excluidos}
    assert "2026-03" in excluidos_por_periodo
    assert "Sin Facturación" in excluidos_por_periodo["2026-03"].motivo
    # Marzo NO debe aparecer entre los periodos comparados.
    assert "2026-03" not in {p.mes for p in resultado.periodos}


def test_excluye_periodo_con_polizas_incompletas_con_nota(contrato):
    resultado = calcular_calibracion(_df_polizas(), _df_facturacion(), _df_factura_pdf(), contrato)

    excluidos_por_periodo = {e.periodo: e for e in resultado.excluidos}
    assert "2026-05" in excluidos_por_periodo
    assert "no cruzan con ninguna póliza" in excluidos_por_periodo["2026-05"].motivo
    assert "2026-05" not in {p.mes for p in resultado.periodos}


def test_sesgo_medio_eur_y_pct(contrato):
    resultado = calcular_calibracion(_df_polizas(), _df_facturacion(), _df_factura_pdf(), contrato)

    diffs = [p.diferencia for p in resultado.periodos]
    esperado_sesgo_eur = round(sum(diffs) / len(diffs), 2)
    assert resultado.sesgo_medio_eur == pytest.approx(esperado_sesgo_eur)

    pcts = [p.diferencia_pct for p in resultado.periodos]
    esperado_sesgo_pct = round(sum(pcts) / len(pcts), 1)
    assert resultado.sesgo_medio_pct == pytest.approx(esperado_sesgo_pct)


def test_df_factura_pdf_vacio_no_revienta(contrato):
    resultado = calcular_calibracion(_df_polizas(), _df_facturacion(), pd.DataFrame(), contrato)
    assert resultado.periodos == []
    assert resultado.excluidos == []
    assert resultado.sesgo_medio_eur is None
    assert resultado.sesgo_medio_pct is None


def test_sin_ninguna_poliza_excluye_todos_los_periodos_reales(contrato):
    resultado = calcular_calibracion(pd.DataFrame(), _df_facturacion(), _df_factura_pdf(), contrato)
    assert resultado.periodos == []
    motivos = {e.periodo: e.motivo for e in resultado.excluidos}
    assert "No hay ninguna Póliza cargada" in motivos["2026-01"]


# --- estimar_comision_y_rappel_periodo --------------------------------------

def _fusion_altas_enero():
    # Mismo caso que _df_polizas()/_df_facturacion() para enero: P1 (salud
    # anual, 500€) + P2 (vida mensual, recibo 12€/mes).
    return pd.DataFrame(
        [
            {"poliza": "P1", "forma_pago": "A", "razon_social": "ASISA PARTICULARES",
             "fecha_efecto": date(2026, 1, 10), "prima_neta": 500.0},
            {"poliza": "P2", "forma_pago": "M", "razon_social": "ASISA VIDA TRANQUILIDAD",
             "fecha_efecto": date(2026, 1, 12), "prima_neta": 12.0},
        ]
    )


def test_estimar_comision_y_rappel_periodo_desglose_completo(contrato):
    resultado = estimar_comision_y_rappel_periodo(_fusion_altas_enero(), contrato, "2026-01")

    est_p1 = estimar_comision_poliza(
        pd.Series({"poliza": "P1", "forma_pago": "A", "razon_social": "ASISA PARTICULARES",
                   "fecha_efecto": date(2026, 1, 10)}),
        contrato, prima_anual=500.0, fecha_referencia=date(2026, 1, 1),
    )
    est_p2 = estimar_comision_poliza(
        pd.Series({"poliza": "P2", "forma_pago": "M", "razon_social": "ASISA VIDA TRANQUILIDAD",
                   "fecha_efecto": date(2026, 1, 12)}),
        contrato, prima_anual=144.0, prima_recibo_mensual=12.0, fecha_referencia=date(2026, 1, 1),
    )
    comision_bruta_esperada = round(est_p1.comision_bruta_estimada + est_p2.comision_bruta_estimada, 2)
    rappel_esperado = calcular_rappel_inicial(
        contrato, fecha_referencia=date(2026, 1, 1), produccion_mes_salud=500.0
    )

    assert resultado.periodo == "2026-01"
    assert resultado.produccion_salud == pytest.approx(500.0)  # solo salud, vida excluida
    assert resultado.comision_bruta == pytest.approx(comision_bruta_esperada)
    assert resultado.rappel.importe == pytest.approx(rappel_esperado.importe)
    assert resultado.total_bruto == pytest.approx(
        round(comision_bruta_esperada + rappel_esperado.importe, 2)
    )
    assert resultado.total_neto == pytest.approx(
        aplicar_retencion(comision_bruta_esperada + rappel_esperado.importe, contrato)
    )


def test_estimacion_periodo_expone_contrato_salud_vida_incluso_sin_vida(contrato):
    """El objeto real debe mantener el contrato que consume Vista rápida."""
    con_vida = estimar_comision_y_rappel_periodo(_fusion_altas_enero(), contrato, "2026-01")
    sin_vida = estimar_comision_y_rappel_periodo(
        _fusion_altas_enero().query("poliza == 'P1'"), contrato, "2026-01"
    )

    for resultado in (con_vida, sin_vida):
        assert hasattr(resultado, "produccion_salud")
        assert hasattr(resultado, "produccion_vida")
        assert hasattr(resultado, "comision_salud")
        assert hasattr(resultado, "comision_vida")
        assert resultado.comision_bruta == resultado.comision_salud + resultado.comision_vida

    assert sin_vida.produccion_vida == 0.0
    assert sin_vida.comision_vida == 0.0


def test_estimar_comision_y_rappel_periodo_neto_aplica_retencion_configurada(contrato):
    resultado = estimar_comision_y_rappel_periodo(_fusion_altas_enero(), contrato, "2026-01")
    # Retención actual del YAML: 15%. Comprobación directa, no solo vía aplicar_retencion,
    # para detectar si algún día cambia el % en config/contrato.yaml sin darse cuenta.
    assert contrato.retencion_irpf == pytest.approx(0.15)
    assert resultado.total_neto == pytest.approx(round(resultado.total_bruto * 0.85, 2))


def test_estimar_comision_y_rappel_periodo_sin_altas_da_todo_cero(contrato):
    resultado = estimar_comision_y_rappel_periodo(pd.DataFrame(columns=["poliza"]), contrato, "2026-04")
    assert resultado.produccion_salud == 0.0
    assert resultado.comision_bruta == 0.0
    assert resultado.total_bruto == resultado.rappel.importe
    assert resultado.total_neto == aplicar_retencion(resultado.rappel.importe, contrato)


def test_estimar_periodo_separa_salud_vida_y_recibo_recurrente(contrato):
    altas = pd.DataFrame([
        {"poliza": "S", "forma_pago": "A", "razon_social": "ASISA PARTICULARES", "fecha_efecto": date(2026, 4, 1), "prima_neta": 500.0},
        {"poliza": "VN", "forma_pago": "M", "razon_social": "ASISA VIDA TRANQUILIDAD", "fecha_efecto": date(2026, 4, 1), "prima_neta": 10.0},
    ])
    recibos = pd.DataFrame([
        {"poliza": "VN", "forma_pago": "M", "razon_social": "ASISA VIDA TRANQUILIDAD", "fecha_efecto": date(2026, 4, 1), "prima_neta": 10.0},
        {"poliza": "VR", "forma_pago": "M", "razon_social": "ASISA VIDA TRANQUILIDAD", "fecha_efecto": date(2025, 10, 1), "prima_neta": 20.0},
    ])
    resultado = estimar_comision_y_rappel_periodo(altas, contrato, "2026-04", recibos)
    assert resultado.produccion_salud == 500.0
    assert resultado.produccion_vida == 120.0  # solo la nueva Vida
    assert resultado.comision_salud == 125.0
    assert resultado.comision_vida == 18.0  # 10*60% + 20*60%; incluye la recurrente
    assert resultado.comision_bruta == resultado.comision_salud + resultado.comision_vida
    assert resultado.total_bruto == resultado.comision_bruta + resultado.rappel.importe
    assert resultado.total_neto == pytest.approx(resultado.total_bruto * (1 - contrato.retencion_irpf))


# --- Vida: comisión de recibos recurrentes (no solo primera alta) ----------

def _fusion_altas_julio_real():
    # Las 5 pólizas reales de Salud de julio 2026 (ver tests/test_eiac_integracion.py):
    # 2 confirmadas por CSV oficial, 3 provisionales de EIAC (razon_social_asumida).
    filas = [
        {"poliza": "64174100", "forma_pago": "M", "razon_social": "ASISA PARTICULARES",
         "fecha_efecto": date(2026, 6, 30), "prima_neta": 89.70},
        {"poliza": "64201679", "forma_pago": "M", "razon_social": "ASISA PARTICULARES",
         "fecha_efecto": date(2026, 6, 30), "prima_neta": 126.00},
        {"poliza": "64226440", "forma_pago": "M", "razon_social": "ASISA PARTICULARES",
         "fecha_efecto": date(2026, 7, 1), "prima_neta": 148.20, "razon_social_asumida": True},
        {"poliza": "64261922", "forma_pago": "M", "razon_social": "ASISA PARTICULARES",
         "fecha_efecto": date(2026, 7, 7), "prima_neta": 49.20, "razon_social_asumida": True},
        {"poliza": "64276918", "forma_pago": "A", "razon_social": "ASISA PARTICULARES",
         "fecha_efecto": date(2026, 7, 1), "prima_neta": 1409.40, "razon_social_asumida": True},
    ]
    return pd.DataFrame(filas)


def _fusion_recibos_vida_julio_real():
    # 3 pólizas Vida reales dadas de alta en mayo (fecha_efecto 2026-05-15,
    # NO son "primera alta" de julio) con un recibo recurrente cobrado en
    # julio -- Vida devenga comisión en CADA recibo, no solo en el primero.
    # Bug real: antes de este fix, estos 49,49€ brutos no se contaban.
    filas = [
        {"poliza": "64110228", "forma_pago": "M", "razon_social": "ASISA VIDA TRANQUILIDAD",
         "fecha_efecto": date(2026, 5, 15), "prima_neta": 28.29},
        {"poliza": "64110254", "forma_pago": "M", "razon_social": "ASISA VIDA TRANQUILIDAD",
         "fecha_efecto": date(2026, 5, 15), "prima_neta": 35.27},
        {"poliza": "64101698", "forma_pago": "M", "razon_social": "ASISA VIDA TRANQUILIDAD",
         "fecha_efecto": date(2026, 5, 15), "prima_neta": 18.93},
    ]
    return pd.DataFrame(filas)


def test_estimar_comision_y_rappel_periodo_incluye_vida_recurrente_caso_real_julio(contrato):
    """Caso real completo de julio 2026 (Sebastián, verificación manual):
    5 altas de Salud (1.591,65€ de comisión bruta) + 3 recibos Vida
    recurrentes (49,49€ de comisión bruta, antes ausentes del cálculo) +
    rappel (1.200€, tope máximo) = 2.841,14€ bruto -> 2.414,97€ NETO
    exactos tras la retención del 15%.
    """
    resultado = estimar_comision_y_rappel_periodo(
        _fusion_altas_julio_real(), contrato, "2026-07", _fusion_recibos_vida_julio_real()
    )
    assert resultado.comision_bruta == pytest.approx(1641.14, abs=0.01)
    assert resultado.rappel.importe == pytest.approx(1200.0)
    assert resultado.total_bruto == pytest.approx(2841.14, abs=0.01)
    assert resultado.total_neto == pytest.approx(2414.97, abs=0.01)


def test_estimar_comision_y_rappel_periodo_sin_fusion_recibos_no_cuenta_vida_recurrente(contrato):
    # Comportamiento anterior (sin pasar fusion_recibos_periodo): los
    # recibos Vida recurrentes NO se cuentan -- reproduce el bug real que
    # motivó este fix, para dejar constancia de la diferencia.
    resultado = estimar_comision_y_rappel_periodo(_fusion_altas_julio_real(), contrato, "2026-07")
    assert resultado.comision_bruta == pytest.approx(1591.65, abs=0.01)
    assert resultado.total_neto == pytest.approx(2372.90, abs=0.01)


# --- Pólizas no activas (anuladas/baja) no cuentan producción --------------
# Investigación pedida por Sebastián (julio 2026) a raíz del caso real
# 64171931 (ASISA Travel and You, anulada por EIAC tras su alta -- ver
# tests/test_eiac_integracion.py). Esa póliza real no tuvo ningún recibo,
# así que no sirve para probar el filtro de extremo a extremo aquí; estos
# dos tests son SINTÉTICOS a propósito, para fijar el comportamiento general
# que el código no garantizaba antes de este fix.

def test_estimar_comision_y_rappel_periodo_excluye_alta_de_poliza_anulada(contrato):
    fusion = pd.DataFrame(
        [
            {"poliza": "ACTIVA", "forma_pago": "A", "razon_social": "ASISA PARTICULARES",
             "fecha_efecto": date(2026, 6, 10), "prima_neta": 500.0, "situacion": "A"},
            {"poliza": "ANULADA", "forma_pago": "A", "razon_social": "ASISA TRAVEL AND YOU",
             "fecha_efecto": date(2026, 6, 10), "prima_neta": 123.43, "situacion": "B"},
        ]
    )
    resultado = estimar_comision_y_rappel_periodo(fusion, contrato, "2026-06")
    # Solo ACTIVA cuenta: la anulada (situacion="B") no debe sumar ni a
    # producción ni a comisión, aunque su alta caiga en el periodo.
    assert resultado.produccion_salud == pytest.approx(500.0)
    est_activa = estimar_comision_poliza(
        pd.Series({"poliza": "ACTIVA", "forma_pago": "A", "razon_social": "ASISA PARTICULARES",
                   "fecha_efecto": date(2026, 6, 10)}),
        contrato, prima_anual=500.0, fecha_referencia=date(2026, 6, 1),
    )
    assert resultado.comision_bruta == pytest.approx(est_activa.comision_bruta_estimada)


def test_estimar_comision_y_rappel_periodo_excluye_recibo_vida_de_poliza_anulada(contrato):
    fusion_recibos = pd.DataFrame(
        [
            {"poliza": "VIDA-ANULADA", "forma_pago": "M", "razon_social": "ASISA VIDA TRANQUILIDAD",
             "fecha_efecto": date(2026, 5, 15), "prima_neta": 28.29, "situacion": "B"},
        ]
    )
    resultado = estimar_comision_y_rappel_periodo(
        pd.DataFrame(columns=["poliza"]), contrato, "2026-07", fusion_recibos
    )
    assert resultado.comision_bruta == 0.0


def test_estimar_comision_y_rappel_periodo_sin_columna_situacion_cuenta_igual_que_antes(contrato):
    # Compatibilidad: llamadas/tests que no incluyen "situacion" (p.ej.
    # _fusion_altas_enero() de arriba) deben seguir contando todo, igual
    # que antes de este fix.
    resultado = estimar_comision_y_rappel_periodo(_fusion_altas_enero(), contrato, "2026-01")
    assert resultado.produccion_salud == pytest.approx(500.0)


# --- Anulación: cuenta ANTES de fecha_baja, se excluye DESDE fecha_baja -----
# Caso real: póliza 64171931 (ASISA Travel and You), alta 10/06/2026,
# anulada el 19/06/2026 (FechaAnulacion real vía EIAC) -- Liquidación real
# de junio muestra un recibo COBRADO 10/06-19/06/2026 con 24,69€ de
# comisión, ANTERIOR a la anulación. El primer fix (ago-2026) excluía la
# póliza entera por "situacion" sin mirar fechas, escondiendo esos 24,69€
# reales. Ahora debe contar en su periodo real (junio) y solo dejar de
# contar en periodos posteriores a la anulación.


def test_alta_de_poliza_anulada_cuenta_si_es_anterior_a_fecha_baja(contrato):
    fusion = pd.DataFrame(
        [
            {"poliza": "64171931", "forma_pago": "A", "razon_social": "ASISA TRAVEL AND YOU",
             "fecha_efecto": date(2026, 6, 10), "prima_neta": 123.43, "situacion": "B",
             "fecha_baja": date(2026, 6, 19)},
        ]
    )
    resultado = estimar_comision_y_rappel_periodo(fusion, contrato, "2026-06")
    est_esperada = estimar_comision_poliza(
        pd.Series({"poliza": "64171931", "forma_pago": "A", "razon_social": "ASISA TRAVEL AND YOU",
                   "fecha_efecto": date(2026, 6, 10)}),
        contrato, prima_anual=123.43, fecha_referencia=date(2026, 6, 1),
    )
    assert resultado.comision_bruta == pytest.approx(est_esperada.comision_bruta_estimada)
    assert resultado.comision_bruta > 0.0


def test_alta_de_poliza_anulada_no_cuenta_si_es_posterior_a_fecha_baja(contrato):
    # Misma póliza, pero con una "alta" (recibo) posterior a su propia
    # anulación -- producción futura de una anulada, el caso que el
    # primer fix (ago-2026) ya arreglaba y que este cambio NO debe
    # reintroducir.
    fusion = pd.DataFrame(
        [
            {"poliza": "64171931", "forma_pago": "A", "razon_social": "ASISA TRAVEL AND YOU",
             "fecha_efecto": date(2026, 7, 1), "prima_neta": 123.43, "situacion": "B",
             "fecha_baja": date(2026, 6, 19)},
        ]
    )
    resultado = estimar_comision_y_rappel_periodo(fusion, contrato, "2026-07")
    assert resultado.comision_bruta == 0.0
    assert resultado.produccion_salud == 0.0


def test_recibo_vida_de_poliza_anulada_cuenta_si_es_anterior_a_fecha_baja(contrato):
    fusion_recibos = pd.DataFrame(
        [
            {"poliza": "VIDA-ANULADA", "forma_pago": "M", "razon_social": "ASISA VIDA TRANQUILIDAD",
             "fecha_efecto": date(2026, 5, 15), "fecha_desde": date(2026, 6, 1),
             "prima_neta": 28.29, "situacion": "B", "fecha_baja": date(2026, 6, 19)},
        ]
    )
    resultado = estimar_comision_y_rappel_periodo(
        pd.DataFrame(columns=["poliza"]), contrato, "2026-06", fusion_recibos
    )
    assert resultado.comision_bruta > 0.0


def test_recibo_vida_de_poliza_anulada_no_cuenta_si_es_posterior_a_fecha_baja(contrato):
    fusion_recibos = pd.DataFrame(
        [
            {"poliza": "VIDA-ANULADA", "forma_pago": "M", "razon_social": "ASISA VIDA TRANQUILIDAD",
             "fecha_efecto": date(2026, 5, 15), "fecha_desde": date(2026, 7, 1),
             "prima_neta": 28.29, "situacion": "B", "fecha_baja": date(2026, 6, 19)},
        ]
    )
    resultado = estimar_comision_y_rappel_periodo(
        pd.DataFrame(columns=["poliza"]), contrato, "2026-07", fusion_recibos
    )
    assert resultado.comision_bruta == 0.0


def test_poliza_anulada_sin_fecha_baja_se_excluye_como_antes(contrato):
    # Sin fecha_baja conocida (anulada pero sin fecha, o columna ausente):
    # criterio conservador anterior -- se excluye directamente. Cubre el
    # caso ya existente de test_estimar_comision_y_rappel_periodo_excluye_
    # alta_de_poliza_anulada, ahora con la columna fecha_baja presente
    # pero vacía (None), en vez de ausente del todo.
    fusion = pd.DataFrame(
        [
            {"poliza": "ANULADA-SIN-FECHA", "forma_pago": "A", "razon_social": "ASISA TRAVEL AND YOU",
             "fecha_efecto": date(2026, 6, 10), "prima_neta": 123.43, "situacion": "B",
             "fecha_baja": None},
        ]
    )
    resultado = estimar_comision_y_rappel_periodo(fusion, contrato, "2026-06")
    assert resultado.comision_bruta == 0.0


# --- Coherencia de fuentes de datos: Calibración vs. Vista rápida/Rappel ----


def _nombres_argumentos_posicionales(nodo_llamada: ast.Call) -> list[str | None]:
    return [arg.id if isinstance(arg, ast.Name) else None for arg in nodo_llamada.args]


def _llamadas_a(arbol: ast.Module, nombre_funcion: str) -> list[ast.Call]:
    return [
        nodo for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == nombre_funcion
    ]


def test_calibracion_usa_las_mismas_fuentes_de_datos_que_vista_rapida():
    """Bug real (ago-2026): la pestaña Calibración llamaba a
    calcular_calibracion() con df_polizas/df_facturacion (solo CSV oficial),
    mientras Vista rápida y Rappel usan df_polizas_con_eiac/
    df_facturacion_con_eiac (CSV oficial + provisionales de EIAC) para
    construir la fusión que pasan a estimar_comision_y_rappel_periodo.
    Ambas pestañas se supone que muestran "lo que el motor estimaría", así
    que deben partir de la misma fuente — si no, el "Estimado" de
    Calibración no es el número que el agente realmente ve en pantalla
    (caso real: subestimaba julio 2026 en +1.250€ frente a Vista rápida).

    Este test lee el CÓDIGO FUENTE de app.py (no ejecuta el dashboard) y
    comprueba, a nivel de nombres de variable, que:
    - calcular_calibracion() recibe df_polizas_con_eiac/df_facturacion_con_eiac.
    - La exportación a Excel (que reutiliza la misma calcular_calibracion,
      ver dashboard/exportacion.py) recibe esas mismas variables al
      construir el Excel completo.
    Así, si alguien vuelve a desalinearlas sin querer, este test falla en
    vez de descubrirse semanas/meses después comparando pantallas a mano.
    """
    arbol = ast.parse(APP_SRC_PATH.read_text())

    llamadas_calibracion = _llamadas_a(arbol, "calcular_calibracion")
    assert llamadas_calibracion, "no se encontró ninguna llamada a calcular_calibracion en app.py"
    for llamada in llamadas_calibracion:
        args = _nombres_argumentos_posicionales(llamada)
        assert args[0] == "df_polizas_con_eiac", (
            f"calcular_calibracion() en app.py:{llamada.lineno} recibe {args[0]!r} "
            "como 1er argumento -- debe ser df_polizas_con_eiac, la misma fuente "
            "que usan Vista rápida/Rappel, o el 'Estimado' de Calibración deja de "
            "ser el número que el agente ve en pantalla."
        )
        assert args[1] == "df_facturacion_con_eiac", (
            f"calcular_calibracion() en app.py:{llamada.lineno} recibe {args[1]!r} "
            "como 2º argumento -- debe ser df_facturacion_con_eiac, por el mismo motivo."
        )

    llamadas_excel = _llamadas_a(arbol, "_construir_excel_completo_cacheado")
    assert llamadas_excel, "no se encontró ninguna llamada a _construir_excel_completo_cacheado en app.py"
    for llamada in llamadas_excel:
        args = _nombres_argumentos_posicionales(llamada)
        assert args[0] == "df_polizas_con_eiac", (
            f"_construir_excel_completo_cacheado() en app.py:{llamada.lineno} recibe "
            f"{args[0]!r} como 1er argumento -- la hoja Calibracion del Excel exportado "
            "reutiliza calcular_calibracion() con este mismo dato (ver "
            "dashboard/exportacion.py::_hoja_calibracion), así que debe ser "
            "df_polizas_con_eiac para seguir coherente con la pestaña en pantalla."
        )
        assert args[1] == "df_facturacion_con_eiac", (
            f"_construir_excel_completo_cacheado() en app.py:{llamada.lineno} recibe "
            f"{args[1]!r} como 2º argumento -- debe ser df_facturacion_con_eiac, por "
            "el mismo motivo."
        )

    # Vista rápida y Rappel: confirmamos que sus fusiones (pasadas como 1er
    # argumento de estimar_comision_y_rappel_periodo) se construyen a partir
    # de un .merge(df_polizas_con_eiac, ...) -- si esto deja de ser cierto,
    # las aserciones de arriba estarían comparando Calibración contra una
    # Vista rápida que también cambió, sin detectar la desalineación real.
    merges_con_eiac = [
        nodo for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "merge"
        and nodo.args
        and isinstance(nodo.args[0], ast.Name)
        and nodo.args[0].id == "df_polizas_con_eiac"
    ]
    assert len(merges_con_eiac) >= 2, (
        "se esperaban al menos 2 fusiones con df_polizas_con_eiac en app.py "
        "(Vista rápida y Rappel) -- si esto cambió, revisar que las "
        "aserciones de coherencia de arriba sigan siendo válidas."
    )
