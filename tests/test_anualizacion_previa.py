"""Regresión: un recibo de cartera no reabre un anticipo Salud ya vigente."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from engine.calibracion import estimar_comision_y_rappel_periodo
from engine.comisiones import obtener_estado_anualizacion_salud
from engine.config_contrato import cargar_contrato
from engine.wanderlust import calcular_pae_anual


CONFIG_PATH = Path(__file__).parent.parent / "config" / "contrato.yaml"


@pytest.fixture
def contrato():
    return cargar_contrato(CONFIG_PATH)


def _salud(poliza: str, prima: float, efecto=date(2026, 6, 1)) -> dict:
    return {
        "poliza": poliza, "forma_pago": "M", "razon_social": "ASISA PARTICULARES",
        "fecha_efecto": efecto, "prima_neta": prima, "situacion": "A",
    }


def _movimientos(*filas: tuple[str, float, str]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "poliza": "SALUD-YA-ANUALIZADA", "accion": accion, "comision": comision,
            "periodo_liquidacion": periodo, "fecha_desde": f"{periodo[:4]}-{periodo[5:]}-01",
        }
        for accion, comision, periodo in filas
    ])


def test_recibo_cartera_no_duplica_salud_rappel_y_conserva_vida(contrato):
    """Caso agregado sintético equivalente a la regresión de agosto."""
    liquidacion = _movimientos((" ANUALIZADA ", 429.60, "2026-06"))
    altas = pd.DataFrame([
        _salud("SALUD-LEGITIMA", 263.00, date(2026, 8, 1)),  # 3.156,00 € legítimos
        _salud("SALUD-YA-ANUALIZADA", 143.20),
    ])
    recibos = pd.concat([
        altas,
        pd.DataFrame([{
            "poliza": "VIDA-RECURRENTE", "forma_pago": "M",
            "razon_social": "ASISA VIDA TRANQUILIDAD", "fecha_efecto": date(2026, 5, 1),
            "prima_neta": 159.5166666667, "situacion": "A",
        }]),
    ], ignore_index=True)

    resultado = estimar_comision_y_rappel_periodo(
        altas, contrato, "2026-08", recibos, liquidacion
    )

    assert resultado.produccion_salud == pytest.approx(3156.00)
    assert resultado.comision_salud == pytest.approx(789.00)
    assert resultado.rappel.produccion_mes == pytest.approx(3156.00)
    assert resultado.rappel.importe == pytest.approx(1136.16)
    assert resultado.comision_vida == pytest.approx(95.71)
    assert resultado.produccion_vida == 0.0
    assert resultado.total_bruto == pytest.approx(2020.87)
    assert resultado.total_neto == pytest.approx(1717.74)
    assert [e.poliza for e in resultado.exclusiones_anualizacion_salud] == ["SALUD-YA-ANUALIZADA"]
    assert resultado.exclusiones_anualizacion_salud[0].motivo == "anualización previa vigente"
    # El DataFrame de Facturación/recibos no se altera: sigue existiendo el movimiento de cartera.
    assert "SALUD-YA-ANUALIZADA" in set(recibos["poliza"])
    assert altas.loc[altas["poliza"] == "SALUD-YA-ANUALIZADA", "situacion"].iloc[0] == "A"


def test_primera_alta_salud_sin_liquidacion_previa_sigue_contando(contrato):
    altas = pd.DataFrame([_salud("SALUD-NUEVA", 100.0, date(2026, 8, 1))])
    resultado = estimar_comision_y_rappel_periodo(altas, contrato, "2026-08", df_liquidacion=pd.DataFrame())
    assert resultado.produccion_salud == 1200.0
    assert resultado.comision_salud == 300.0
    assert resultado.exclusiones_anualizacion_salud == []


def test_extorno_completo_libera_y_reanualizacion_vuelve_a_bloquear(contrato):
    extornada = _movimientos(
        ("ANUALIZADA", 100.0, "2026-06"), ("EXTORNO anualizada", -100.0, "2026-07"),
    )
    estado_extornado = obtener_estado_anualizacion_salud(extornada, "SALUD-YA-ANUALIZADA", "2026-08")
    assert estado_extornado.anualizacion_vigente is False

    alta = pd.DataFrame([_salud("SALUD-YA-ANUALIZADA", 100.0, date(2026, 8, 1))])
    resultado_extornado = estimar_comision_y_rappel_periodo(
        alta, contrato, "2026-08", df_liquidacion=extornada
    )
    assert resultado_extornado.produccion_salud == 1200.0

    reanualizada = pd.concat([extornada, _movimientos(("ANUALIZADA", 100.0, "2026-07"))], ignore_index=True)
    estado_reanualizado = obtener_estado_anualizacion_salud(
        reanualizada, "SALUD-YA-ANUALIZADA", "2026-08"
    )
    assert estado_reanualizado.anualizacion_vigente is True
    resultado_reanualizado = estimar_comision_y_rappel_periodo(
        alta, contrato, "2026-08", df_liquidacion=reanualizada
    )
    assert resultado_reanualizado.produccion_salud == 0.0
    assert resultado_reanualizado.comision_salud == 0.0


def test_ajuste_a_descontar_no_cancela_anualizacion_positiva():
    movimientos = _movimientos(("ANUALIZADA", 100.0, "2026-06"), ("A DESCONTAR", 100.0, "2026-07"))
    estado = obtener_estado_anualizacion_salud(movimientos, "SALUD-YA-ANUALIZADA", "2026-08")
    assert estado.anualizacion_vigente is True


def test_recibo_posterior_no_duplica_pae_wanderlust(contrato):
    polizas = pd.DataFrame([{
        "poliza": "SALUD-YA-ANUALIZADA", "razon_social": "ASISA PARTICULARES", "forma_pago": "M",
        "fecha_efecto": date(2026, 6, 1), "situacion": "A",
    }])
    facturacion = pd.DataFrame([
        {"poliza": "SALUD-YA-ANUALIZADA", "prima_neta": 143.20, "periodo_liquidacion": "2026-06", "fecha_desde": "2026-06-01"},
        {"poliza": "SALUD-YA-ANUALIZADA", "prima_neta": 143.20, "periodo_liquidacion": "2026-08", "fecha_desde": "2026-08-01"},
    ])
    resultado = calcular_pae_anual(polizas, facturacion, contrato, 2026)
    assert resultado.pae_total == pytest.approx(1718.40)
    assert resultado.por_categoria["Salud Particulares"].polizas_alta == 1
