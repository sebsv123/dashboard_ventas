from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from engine.comisiones import (
    TIPO_SALUD_ANUAL,
    TIPO_SALUD_MENSUAL,
    estimar_comision_poliza,
    evaluar_historial_ajustes_poliza,
    refinar_confianza_salud_mensual,
    resumen_historial_ajustes_cartera,
)
from engine.config_contrato import cargar_contrato
from engine.insights import polizas_salud_mensual_historial_irregular

CONFIG_PATH = Path(__file__).parent.parent / "config" / "contrato.yaml"


@pytest.fixture
def contrato():
    return cargar_contrato(CONFIG_PATH)


def _fila_salud_mensual(poliza: str, fecha_efecto: date) -> pd.Series:
    return pd.Series(
        {
            "poliza": poliza,
            "razon_social": "ASISA PARTICULARES",
            "forma_pago": "M",
            "fecha_efecto": fecha_efecto,
        }
    )


def _df_liquidacion_63938090() -> pd.DataFrame:
    # Caso real documentado: EXTORNO ANUALIZADA + 3 ANUALIZADA -> historial irregular.
    return pd.DataFrame(
        [
            {"poliza": "63938090", "accion": "EXTORNO ANUALIZADA", "comision": -265.17, "periodo_liquidacion": "03-2026"},
            {"poliza": "63938090", "accion": "ANUALIZADA", "comision": 318.2, "periodo_liquidacion": "03-2026"},
            {"poliza": "63938090", "accion": "ANUALIZADA", "comision": 26.52, "periodo_liquidacion": "04-2026"},
            {"poliza": "63938090", "accion": "ANUALIZADA", "comision": 238.65, "periodo_liquidacion": "05-2026"},
        ]
    )


def _df_liquidacion_63920702() -> pd.DataFrame:
    # Caso real documentado: un único ANUALIZADA -> sin ajustes conocidos.
    return pd.DataFrame(
        [
            {"poliza": "63920702", "accion": "ANUALIZADA", "comision": 170.53, "periodo_liquidacion": "02-2026"},
        ]
    )


def test_historial_irregular_63938090(contrato):
    historial = evaluar_historial_ajustes_poliza(_df_liquidacion_63938090(), "63938090")
    assert historial.tiene_ajustes_previos is True
    assert historial.num_eventos == 4
    # El último evento cronológicamente es el ANUALIZADA de 05-2026 (238.65€).
    assert historial.ultimo_periodo == "05-2026"
    assert historial.ultimo_importe == pytest.approx(238.65)
    assert historial.ultimo_accion == "ANUALIZADA"


def test_historial_sin_ajustes_63920702(contrato):
    historial = evaluar_historial_ajustes_poliza(_df_liquidacion_63920702(), "63920702")
    assert historial.tiene_ajustes_previos is False
    assert historial.num_eventos == 1


def test_historial_poliza_sin_ningun_evento():
    df_liquidacion = pd.DataFrame(
        [{"poliza": "OTRA", "accion": "PRODUCCION", "comision": 10.0, "periodo_liquidacion": "01-2026"}]
    )
    historial = evaluar_historial_ajustes_poliza(df_liquidacion, "63920702")
    assert historial.tiene_ajustes_previos is False
    assert historial.num_eventos == 0


def test_historial_df_liquidacion_vacio_no_revienta():
    historial = evaluar_historial_ajustes_poliza(pd.DataFrame(), "63920702")
    assert historial.tiene_ajustes_previos is False
    assert historial.num_eventos == 0


def test_refinar_confianza_baja_a_baja_con_historial_irregular(contrato):
    fila = _fila_salud_mensual("63938090", date(2026, 2, 10))
    estimacion = estimar_comision_poliza(fila, contrato, prima_anual=360.0)
    assert estimacion.confianza == "media"  # confianza inicial del motor genérico

    refinada = refinar_confianza_salud_mensual(estimacion, _df_liquidacion_63938090())
    assert refinada.confianza == "baja"
    assert "ya tuvo ajustes de comisión en el pasado" in refinada.nota
    assert "238.65" in refinada.nota
    assert "05-2026" in refinada.nota
    # El resto de la estimación no cambia, solo confianza/nota.
    assert refinada.comision_bruta_estimada == estimacion.comision_bruta_estimada
    assert refinada.poliza == estimacion.poliza


def test_refinar_confianza_se_mantiene_media_sin_historial(contrato):
    fila = _fila_salud_mensual("63920702", date(2026, 1, 10))
    estimacion = estimar_comision_poliza(fila, contrato, prima_anual=360.0)

    refinada = refinar_confianza_salud_mensual(estimacion, _df_liquidacion_63920702())
    assert refinada.confianza == "media"
    assert "Sin ajustes históricos conocidos" in refinada.nota
    assert "asumimos que se mantiene así" in refinada.nota


def test_refinar_confianza_no_afecta_a_tipos_distintos_de_salud_mensual(contrato):
    fila = pd.Series(
        {
            "poliza": "P-ANUAL",
            "razon_social": "ASISA PARTICULARES",
            "forma_pago": "A",
            "fecha_efecto": date(2026, 1, 10),
        }
    )
    estimacion = estimar_comision_poliza(fila, contrato, prima_anual=500.0)
    assert estimacion.tipo == TIPO_SALUD_ANUAL

    refinada = refinar_confianza_salud_mensual(estimacion, _df_liquidacion_63938090())
    # No es salud mensual: se devuelve intacta, sin tocar confianza/nota.
    assert refinada == estimacion


def test_refinar_confianza_df_liquidacion_vacio_mantiene_media(contrato):
    fila = _fila_salud_mensual("NUEVA-POLIZA-SIN-HISTORIAL", date(2026, 6, 1))
    estimacion = estimar_comision_poliza(fila, contrato, prima_anual=360.0)

    refinada = refinar_confianza_salud_mensual(estimacion, pd.DataFrame())
    assert refinada.confianza == "media"
    assert "Sin ajustes históricos conocidos" in refinada.nota


def _df_polizas_dashboard():
    return pd.DataFrame(
        [
            {"poliza": "63938090", "forma_pago": "M", "razon_social": "ASISA PARTICULARES"},
            {"poliza": "63920702", "forma_pago": "M", "razon_social": "ASISA PARTICULARES"},
            {"poliza": "P-VIDA", "forma_pago": "M", "razon_social": "ASISA VIDA TRANQUILIDAD"},
            {"poliza": "P-ANUAL", "forma_pago": "A", "razon_social": "ASISA PARTICULARES"},
        ]
    )


def _df_liquidacion_dashboard():
    return pd.concat([_df_liquidacion_63938090(), _df_liquidacion_63920702()], ignore_index=True)


def _df_facturacion_dashboard():
    return pd.DataFrame(
        [
            {"poliza": "63938090", "periodo_liquidacion": "2026-02", "prima_neta": 30.0, "fecha_desde": "2026-02-10"},
            {"poliza": "63920702", "periodo_liquidacion": "2026-01", "prima_neta": 20.0, "fecha_desde": "2026-01-10"},
        ]
    )


def test_polizas_salud_mensual_historial_irregular_solo_lista_las_afectadas(contrato):
    resultado = polizas_salud_mensual_historial_irregular(
        _df_polizas_dashboard(), _df_facturacion_dashboard(), _df_liquidacion_dashboard(),
        contrato, "2026-02",
    )
    assert len(resultado) == 1
    assert resultado[0].poliza == "63938090"
    assert resultado[0].ultimo_periodo == "05-2026"
    assert resultado[0].ultimo_importe == pytest.approx(238.65)


def test_polizas_salud_mensual_historial_irregular_periodo_sin_irregulares(contrato):
    resultado = polizas_salud_mensual_historial_irregular(
        _df_polizas_dashboard(), _df_facturacion_dashboard(), _df_liquidacion_dashboard(),
        contrato, "2026-01",
    )
    assert resultado == []


def test_polizas_salud_mensual_historial_irregular_dfs_vacios_no_revienta(contrato):
    assert polizas_salud_mensual_historial_irregular(
        pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), contrato, "2026-01"
    ) == []


def test_resumen_historial_ajustes_cartera_cuenta_solo_salud_mensual(contrato):
    resumen = resumen_historial_ajustes_cartera(_df_polizas_dashboard(), _df_liquidacion_dashboard(), contrato)
    # De la cartera: 63938090 (irregular), 63920702 (sin ajustes) son salud
    # mensual; P-VIDA es Vida (excluida); P-ANUAL es salud anual (excluida).
    assert resumen.total_salud_mensual == 2
    assert resumen.con_historial_irregular == 1
    assert resumen.pct_irregular == pytest.approx(50.0)


def test_resumen_historial_ajustes_cartera_vacia_no_revienta(contrato):
    resumen = resumen_historial_ajustes_cartera(pd.DataFrame(), pd.DataFrame(), contrato)
    assert resumen.total_salud_mensual == 0
    assert resumen.con_historial_irregular == 0
    assert resumen.pct_irregular is None
