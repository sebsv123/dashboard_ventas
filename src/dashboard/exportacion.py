"""Exportación de "todos los datos analizados" a un único Excel descargable.

Un solo botón en el sidebar (ver app.py) genera un .xlsx con una hoja por
tabla/analisis del dashboard, más una hoja "Avisos" en primera posición que
señala qué periodos futuros todavía no tienen ningún dato cargado — para que
quien lo abra en Excel no confunda "no aparece nada de agosto" con "no hubo
ventas en agosto", cuando en realidad es que aún falta subir el fichero.
"""

from __future__ import annotations

from datetime import date, timedelta
from io import BytesIO

import pandas as pd

from engine.calibracion import calcular_calibracion
from engine.config_contrato import ContratoConfig
from engine.insights import resumen_produccion_periodo, siguiente_periodo
from engine.objetivo import calcular_objetivo_anual
from engine.wanderlust import calcular_pae_anual


def _meses_del_anio_sin_datos(
    df_polizas: pd.DataFrame, df_facturacion: pd.DataFrame, contrato: ContratoConfig, hoy: date
) -> list[str]:
    """Periodos "AAAA-MM" desde el mes siguiente al actual hasta diciembre
    del año en curso que todavía NO tienen ningún dato cargado
    (`resumen_produccion_periodo(...).tiene_datos == False`).

    Se calcula dinámicamente (no se asume "agosto" a fuego) para que el
    aviso siga siendo correcto pase el tiempo que pase.
    """
    periodo_actual = f"{hoy.year:04d}-{hoy.month:02d}"
    periodo = siguiente_periodo(periodo_actual)
    sin_datos = []
    while periodo.startswith(f"{hoy.year:04d}"):
        resumen = resumen_produccion_periodo(df_polizas, df_facturacion, contrato, periodo)
        if not resumen.tiene_datos:
            sin_datos.append(periodo)
        periodo = siguiente_periodo(periodo)
    return sin_datos


def _hoja_avisos(
    df_polizas: pd.DataFrame, df_facturacion: pd.DataFrame, contrato: ContratoConfig, hoy: date
) -> pd.DataFrame:
    periodo_actual = f"{hoy.year:04d}-{hoy.month:02d}"
    meses_sin_datos = _meses_del_anio_sin_datos(df_polizas, df_facturacion, contrato, hoy)

    filas = [
        {"Aviso": f"Exportación generada el {hoy.isoformat()}."},
        {
            "Aviso": (
                "Este Excel refleja SOLO lo que ya está subido al dashboard a fecha de "
                "hoy. No es un cierre contable ni sustituye a la Liquidación/Factura real."
            )
        },
    ]
    if meses_sin_datos:
        listado = ", ".join(meses_sin_datos)
        filas.append(
            {
                "Aviso": (
                    f"⚠️ FALTAN DATOS de {listado} en adelante — todavía no se ha subido "
                    "ningún fichero de Facturación/Pólizas/EIAC de esos periodos. Es muy "
                    "probable que ya haya ventas realizadas en esos meses que aún NO están "
                    "contabilizadas aquí por falta de ese dato, no porque no haya habido "
                    "producción."
                )
            }
        )
    else:
        filas.append(
            {
                "Aviso": (
                    f"No hay periodos restantes de {hoy.year} sin ningún dato cargado a "
                    "fecha de esta exportación."
                )
            }
        )
    filas.append(
        {
            "Aviso": (
                f"Periodo actual detectado: {periodo_actual}. Revisa la hoja 'Objetivo "
                "anual' para el detalle mes a mes (columna '¿Completo?')."
            )
        }
    )
    return pd.DataFrame(filas)


def _hoja_objetivo_anual(
    df_polizas: pd.DataFrame, df_facturacion: pd.DataFrame, contrato: ContratoConfig, hoy: date
) -> pd.DataFrame:
    resultado = calcular_objetivo_anual(
        df_polizas, df_facturacion, contrato, anio=hoy.year, mes_hasta=hoy.month
    )
    return pd.DataFrame(
        [
            {
                "Periodo": m.periodo,
                "Salud mensual (€)": m.salud_mensual,
                "Salud anual (€)": m.salud_anual,
                "Vida (€)": m.vida,
                "Total (€)": m.total,
                "¿Completo?": "Sí" if m.completo else "NO — faltan datos",
                "¿Fuente?": "Solo EIAC (provisional)" if m.es_eiac_parcial else "CSV oficial",
            }
            for m in resultado.meses
        ]
    )


def _fecha_corte_mes_anterior(hoy: date) -> date:
    """Último día del mes natural anterior a `hoy` (p.ej. hoy=2026-07-26 ->
    2026-06-30) — la fecha de corte que suele usar ASISA en sus correos de
    seguimiento de Wanderlust."""
    primer_dia_mes_actual = hoy.replace(day=1)
    return primer_dia_mes_actual - timedelta(days=1)


def _hoja_wanderlust(
    df_polizas: pd.DataFrame, df_facturacion: pd.DataFrame, contrato: ContratoConfig, hoy: date
) -> pd.DataFrame:
    """Desglose por categoría del PAE (Primas Anualizadas Equivalentes) del
    incentivo Wanderlust — mismo motor y mismo criterio de transparencia que
    el resto de hojas (ver `engine.wanderlust`, NO usa periodo_liquidacion,
    solo fecha_efecto dentro del año en curso).

    Añade también, si el mes anterior completo cae dentro del mismo año que
    se está calculando (no aplica en enero, cuyo mes anterior es diciembre
    del año pasado), una fila con el PAE acumulado a esa fecha de corte —
    útil para comparar contra un correo de seguimiento de ASISA con una
    fecha de corte concreta (p.ej. "a 30 de junio").
    """
    resultado = calcular_pae_anual(df_polizas, df_facturacion, contrato, anio=hoy.year)

    filas = [
        {"Categoría": d.categoria, "PAE (€)": d.pae, "Altas": d.polizas_alta, "Anuladas": d.polizas_baja, "Nota": ""}
        for d in sorted(resultado.por_categoria.values(), key=lambda d: -d.pae)
    ]
    filas.append(
        {
            "Categoría": f"TOTAL {hoy.year}",
            "PAE (€)": resultado.pae_total,
            "Altas": sum(d.polizas_alta for d in resultado.por_categoria.values()),
            "Anuladas": sum(d.polizas_baja for d in resultado.por_categoria.values()),
            "Nota": "",
        }
    )

    if resultado.sin_categoria:
        filas.append(
            {
                "Categoría": "Sin categoría PAE reconocida",
                "PAE (€)": None,
                "Altas": resultado.sin_categoria,
                "Anuladas": None,
                "Nota": "Pólizas con fecha_efecto en el año pero razon_social no reconocida — no suman al total.",
            }
        )

    if resultado.objetivo:
        filas.append(
            {
                "Categoría": "% cumplimiento objetivo PAE individual",
                "PAE (€)": resultado.porcentaje,
                "Altas": None,
                "Anuladas": None,
                "Nota": f"Objetivo configurado: {resultado.objetivo:,.2f} € (config/contrato.yaml, wanderlust.objetivo_paes).",
            }
        )
    else:
        filas.append(
            {
                "Categoría": "Objetivo PAE individual",
                "PAE (€)": None,
                "Altas": None,
                "Anuladas": None,
                "Nota": "Todavía sin definir (ASISA aún no lo ha comunicado) — no se muestra ningún % inventado.",
            }
        )

    corte = _fecha_corte_mes_anterior(hoy)
    if corte.year == hoy.year:
        df_polizas_corte = df_polizas[
            df_polizas["fecha_efecto"].notna()
            & (pd.to_datetime(df_polizas["fecha_efecto"]) <= pd.Timestamp(corte))
        ]
        resultado_corte = calcular_pae_anual(df_polizas_corte, df_facturacion, contrato, anio=hoy.year)
        filas.append(
            {
                "Categoría": f"PAE acumulado a {corte.isoformat()} (mes anterior completo)",
                "PAE (€)": resultado_corte.pae_total,
                "Altas": sum(d.polizas_alta for d in resultado_corte.por_categoria.values()),
                "Anuladas": sum(d.polizas_baja for d in resultado_corte.por_categoria.values()),
                "Nota": "Solo pólizas con fecha_efecto <= esta fecha — para comparar contra un correo de seguimiento de ASISA con esa fecha de corte.",
            }
        )

    return pd.DataFrame(filas)


def _hoja_calibracion(
    df_polizas: pd.DataFrame,
    df_facturacion: pd.DataFrame,
    df_factura_pdf: pd.DataFrame,
    contrato: ContratoConfig,
    df_liquidacion: pd.DataFrame,
) -> pd.DataFrame:
    resultado = calcular_calibracion(df_polizas, df_facturacion, df_factura_pdf, contrato, df_liquidacion)
    filas = [
        {
            "Periodo": p.mes,
            "Estimado (€)": p.estimado,
            "Real (€)": p.real,
            "Diferencia (€)": p.diferencia,
            "Diferencia (%)": p.diferencia_pct,
            "Estado": "Incluido",
        }
        for p in resultado.periodos
    ]
    filas += [
        {
            "Periodo": e.periodo,
            "Estimado (€)": None,
            "Real (€)": None,
            "Diferencia (€)": None,
            "Diferencia (%)": None,
            "Estado": f"Excluido — {e.motivo}",
        }
        for e in resultado.excluidos
    ]
    return pd.DataFrame(filas)


def _quitar_timezone(df: pd.DataFrame) -> pd.DataFrame:
    """Excel no admite columnas datetime con timezone — Streamlit/pandas a
    veces las trae en UTC (p.ej. fecha_import). Se quita el tz conservando
    la fecha/hora local para que openpyxl no falle al escribir la hoja."""
    df = df.copy()
    for columna in df.columns:
        if isinstance(df[columna].dtype, pd.DatetimeTZDtype):
            df[columna] = df[columna].dt.tz_localize(None)
    return df


def construir_excel_completo(
    *,
    df_polizas: pd.DataFrame,
    df_facturacion: pd.DataFrame,
    df_liquidacion: pd.DataFrame,
    df_factura_pdf: pd.DataFrame,
    df_eiac_polizas: pd.DataFrame,
    df_eiac_recibos: pd.DataFrame,
    contrato: ContratoConfig,
    hoy: date | None = None,
) -> bytes:
    """Genera el .xlsx con todos los datos analizados, una hoja por tabla.

    Se usan las tablas "_con_eiac" donde tiene sentido (Pólizas/Facturación)
    para que la exportación cuadre con lo que ya se ve en pantalla en Vista
    rápida/Rappel/Resumen — incluye provisionales de EIAC todavía sin
    confirmar por el CSV oficial.
    """
    hoy = hoy or date.today()
    buffer = BytesIO()

    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        _hoja_avisos(df_polizas, df_facturacion, contrato, hoy).to_excel(
            writer, sheet_name="Avisos", index=False
        )
        _quitar_timezone(df_polizas).to_excel(writer, sheet_name="Polizas", index=False)
        _quitar_timezone(df_facturacion).to_excel(writer, sheet_name="Facturacion", index=False)
        _quitar_timezone(df_liquidacion).to_excel(writer, sheet_name="Liquidacion", index=False)
        _quitar_timezone(df_factura_pdf).to_excel(writer, sheet_name="Factura PDF", index=False)
        _quitar_timezone(df_eiac_polizas).to_excel(writer, sheet_name="EIAC Polizas", index=False)
        _quitar_timezone(df_eiac_recibos).to_excel(writer, sheet_name="EIAC Recibos", index=False)
        _hoja_objetivo_anual(df_polizas, df_facturacion, contrato, hoy).to_excel(
            writer, sheet_name="Objetivo anual", index=False
        )
        _hoja_wanderlust(df_polizas, df_facturacion, contrato, hoy).to_excel(
            writer, sheet_name="Wanderlust PAE", index=False
        )
        _hoja_calibracion(df_polizas, df_facturacion, df_factura_pdf, contrato, df_liquidacion).to_excel(
            writer, sheet_name="Calibracion", index=False
        )

        for hoja in writer.sheets.values():
            for columna in hoja.columns:
                ancho = max((len(str(c.value)) for c in columna if c.value is not None), default=10)
                hoja.column_dimensions[columna[0].column_letter].width = min(ancho + 2, 60)

    return buffer.getvalue()
