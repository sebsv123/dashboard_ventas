from pathlib import Path

import pandas as pd
import pytest

from engine.calibracion import calcular_calibracion, estimar_comision_y_rappel_periodo
from engine.comisiones import aplicar_retencion, estimar_comision_poliza
from engine.config_contrato import cargar_contrato
from engine.rappel import calcular_rappel_inicial
from datetime import date

CONFIG_PATH = Path(__file__).parent.parent / "config" / "contrato.yaml"


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
