"""Dashboard de Ventas — panel personal de comisiones y rappel.

Arranque: uv run streamlit run src/dashboard/app.py
"""

from __future__ import annotations

import calendar
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

RAIZ = Path(__file__).parent.parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from db.carga import (
    cargar_facturacion,
    cargar_liquidacion,
    cargar_polizas,
    cargar_factura_pdf,
    recalcular_resumen_mensual,
)
from db.schema import conectar, inicializar_schema
from engine.comisiones import estimar_comision_poliza
from engine.config_contrato import cargar_contrato
from engine.insights import (
    alertas_cambio_tarifa,
    construir_produccion_polizas,
    evolucion_mensual,
    hay_suficiente_historico,
    primeras_altas_por_periodo,
    ranking_productos,
    ranking_provincias,
    variacion_mes_actual_vs_anterior,
)
from engine.proyeccion import proyectar_cierre_mes
from engine.rappel import calcular_rappel_inicial
from engine.reconciliacion import detectar_polizas_sin_cobrar
from ingestion.facturacion import parsear_facturacion
from ingestion.factura_pdf import parsear_factura_pdf
from ingestion.liquidacion import parsear_liquidacion
from ingestion.polizas import parsear_polizas

# DASHBOARD_DB_PATH permite apuntar a otra BD (tests de humo con AppTest);
# sin la variable de entorno, el comportamiento es idéntico al de siempre.
DB_PATH = Path(os.environ.get("DASHBOARD_DB_PATH", str(RAIZ / "data" / "asisa.db")))
CONFIG_PATH = RAIZ / "config" / "contrato.yaml"
LOGO_PATH = RAIZ / "assets" / "asisa_logo.png"

AZUL_ASISA = "#003DA5"

st.set_page_config(
    page_title="Panel Sebastián · Agente Exclusivo",
    page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else "📊",
    layout="wide",
)

# --- Estilo básico con el azul corporativo -----------------------------------
st.markdown(
    f"""
    <style>
        .stTabs [data-baseweb="tab-list"] {{ gap: 8px; }}
        .stTabs [aria-selected="true"] {{
            background-color: {AZUL_ASISA}20;
            border-bottom: 3px solid {AZUL_ASISA};
        }}
        h1, h2, h3 {{ color: {AZUL_ASISA}; }}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_conn():
    conn = conectar(DB_PATH)
    inicializar_schema(conn)
    return conn


@st.cache_resource
def get_contrato():
    return cargar_contrato(CONFIG_PATH)


conn = get_conn()
contrato = get_contrato()

# --- Cabecera -----------------------------------------------------------------
col_logo, col_titulo = st.columns([1, 4])
with col_logo:
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width=160)
with col_titulo:
    st.title("Panel Sebastián · Agente Exclusivo")
    st.caption("Comisiones y rappel — ASISA / ASISA VIDA")

# --- Sidebar: subida de ficheros ----------------------------------------------
with st.sidebar:
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width=120)
    st.header("Subir ficheros")

    st.subheader("Semanal")
    f_facturacion = st.file_uploader("Facturación (CSV)", type="csv", key="facturacion")
    f_polizas = st.file_uploader("Pólizas (CSV)", type="csv", key="polizas")

    st.subheader("Mensual (cuando ASISA liquide)")
    f_liquidacion = st.file_uploader("Liquidación (CSV)", type="csv", key="liquidacion")
    f_factura_pdf = st.file_uploader("Factura (PDF)", type="pdf", key="factura_pdf")

    if st.button("Procesar ficheros subidos", type="primary", width="stretch"):
        mensajes = []
        try:
            if f_facturacion:
                df = parsear_facturacion(f_facturacion)
                n = cargar_facturacion(conn, df)
                mensajes.append(f"Facturación: {n} filas nuevas importadas.")
            if f_polizas:
                df = parsear_polizas(f_polizas)
                n = cargar_polizas(conn, df)
                mensajes.append(f"Pólizas: {n} registros actualizados.")
            if f_liquidacion:
                df = parsear_liquidacion(f_liquidacion)
                n = cargar_liquidacion(conn, df)
                mensajes.append(f"Liquidación: {n} filas nuevas importadas.")
            if f_factura_pdf:
                facturas = parsear_factura_pdf(f_factura_pdf)
                n = cargar_factura_pdf(conn, facturas)
                mensajes.append(f"Factura PDF: {n} entidad(es) importada(s).")
            if not mensajes:
                st.warning("No has seleccionado ningún fichero.")
            else:
                recalcular_resumen_mensual(conn, contrato)
            for m in mensajes:
                st.success(m)
            st.cache_data.clear()
        except ValueError as e:
            st.error(f"Error al procesar: {e}")

    st.divider()
    st.caption(
        "Los ficheros de Facturación/Pólizas dan una vista **estimada** "
        "en tiempo casi real. La Liquidación/Factura mensual es la que "
        "confirma los números **reales**."
    )


# --- Carga de datos desde la BD ------------------------------------------------
@st.cache_data(ttl=60)
def cargar_datos():
    polizas = pd.read_sql("SELECT * FROM polizas", conn, parse_dates=["fecha_emision", "fecha_efecto", "fecha_baja"])
    facturacion = pd.read_sql("SELECT * FROM facturacion", conn, parse_dates=["fecha_desde", "fecha_hasta"])
    liquidacion = pd.read_sql("SELECT * FROM liquidacion", conn, parse_dates=["fecha_desde", "fecha_hasta"])
    factura_pdf = pd.read_sql("SELECT * FROM factura_pdf", conn)
    return polizas, facturacion, liquidacion, factura_pdf


df_polizas, df_facturacion, df_liquidacion, df_factura_pdf = cargar_datos()

if df_polizas.empty and df_facturacion.empty:
    st.info(
        "Todavía no hay datos cargados. Sube al menos un fichero de "
        "Facturación y Pólizas desde el panel lateral para empezar."
    )
    st.stop()

# --- Tabs -----------------------------------------------------------------
# NOTA sobre "periodo" en este dashboard — no es la misma noción en todas
# las pestañas, y mezclarlas fue la causa de un bug real (pólizas con
# fecha_efecto a fin de mes contadas en el mes calendario equivocado):
#   - Resumen: SIN filtro de periodo. Son snapshots de cartera completa
#     (pólizas activas ahora mismo, histórico completo de facturas PDF).
#   - Rappel: usa periodo_liquidacion de Facturación (columna PER.
#     LIQUIDACION, el ciclo real 16->15 que calcula ASISA) para decidir
#     qué pólizas son "nuevas altas del mes en curso" — nunca el mes
#     calendario de fecha_efecto de Pólizas (ver
#     engine.insights.primeras_altas_por_periodo).
#   - Insights: todo el histórico (como Resumen), pero al agrupar "por mes"
#     también usa periodo_liquidacion vía construir_produccion_polizas,
#     por la misma razón que Rappel.
tab_resumen, tab_polizas, tab_rappel, tab_alertas, tab_insights = st.tabs(
    ["📊 Resumen", "📋 Pólizas", "🎯 Rappel", "⚠️ Alertas", "📈 Insights"]
)

# =============================================================================
# TAB: Resumen
# =============================================================================
with tab_resumen:
    st.subheader("Resumen general (cartera completa, sin filtro de periodo)")

    polizas_activas = df_polizas[df_polizas["situacion"] == "A"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pólizas activas", len(polizas_activas))
    c2.metric("Provincias distintas", df_polizas["provincia_tomador"].nunique())
    if not df_factura_pdf.empty:
        ultimo = df_factura_pdf.sort_values("periodo").iloc[-1]
        c3.metric("Último rappel confirmado", f"{ultimo['rappel']:.2f} €")
        c4.metric("Última factura total", f"{ultimo['total_factura']:.2f} €")

    st.divider()
    st.subheader("Producción por provincia")
    if not df_polizas.empty:
        conteo_provincia = df_polizas["provincia_tomador"].value_counts().reset_index()
        conteo_provincia.columns = ["provincia", "polizas"]
        fig = px.bar(conteo_provincia, x="provincia", y="polizas", color_discrete_sequence=[AZUL_ASISA])
        st.plotly_chart(fig, width="stretch")

    st.subheader("Producción por producto")
    if not df_polizas.empty:
        conteo_producto = df_polizas["razon_social"].value_counts().reset_index()
        conteo_producto.columns = ["producto", "polizas"]
        fig2 = px.pie(conteo_producto, names="producto", values="polizas")
        st.plotly_chart(fig2, width="stretch")

    if not df_factura_pdf.empty:
        st.subheader("Histórico de facturación confirmada")
        hist = df_factura_pdf.sort_values("periodo")[
            ["periodo", "entidad_nombre", "rappel", "total_liquidacion", "total_factura"]
        ]
        st.dataframe(hist, width="stretch", hide_index=True)

# =============================================================================
# TAB: Pólizas
# =============================================================================
with tab_polizas:
    st.subheader("Detalle de pólizas")
    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        filtro_producto = st.multiselect(
            "Producto", options=sorted(df_polizas["razon_social"].dropna().unique())
        )
    with col_f2:
        filtro_provincia = st.multiselect(
            "Provincia", options=sorted(df_polizas["provincia_tomador"].dropna().unique())
        )
    with col_f3:
        opciones_situacion = sorted(df_polizas["situacion"].dropna().unique())
        default_situacion = ["A"] if "A" in opciones_situacion else []
        filtro_situacion = st.multiselect(
            "Situación", options=opciones_situacion, default=default_situacion
        )

    df_mostrar = df_polizas.copy()
    if filtro_producto:
        df_mostrar = df_mostrar[df_mostrar["razon_social"].isin(filtro_producto)]
    if filtro_provincia:
        df_mostrar = df_mostrar[df_mostrar["provincia_tomador"].isin(filtro_provincia)]
    if filtro_situacion:
        df_mostrar = df_mostrar[df_mostrar["situacion"].isin(filtro_situacion)]

    st.dataframe(
        df_mostrar[
            [
                "poliza", "razon_social", "forma_pago", "situacion", "fecha_emision",
                "fecha_efecto", "provincia_tomador", "nombre_tomador",
            ]
        ],
        width="stretch",
        hide_index=True,
    )

# =============================================================================
# TAB: Rappel
# =============================================================================
with tab_rappel:
    st.subheader("Proyección de rappel del mes en curso")

    hoy = date.today()
    mes_texto = f"{hoy.year:04d}-{hoy.month:02d}"

    # PERIODO: se determina por periodo_liquidacion de Facturación (el ciclo
    # real 16->15 que ya calcula ASISA), NUNCA por el mes calendario de
    # fecha_efecto de Pólizas — Pólizas es una foto de cartera sin noción de
    # periodo. Ver el docstring de primeras_altas_por_periodo para el caso
    # real que motivó esto (pólizas con efecto 30/06 que devengan en julio).
    altas_mes = primeras_altas_por_periodo(df_facturacion)
    altas_mes = altas_mes[altas_mes["periodo_liquidacion"] == mes_texto]
    altas_mes = altas_mes.merge(
        df_polizas[["poliza", "forma_pago", "razon_social"]], on="poliza", how="inner"
    )

    nuevas_mes_salud = altas_mes[~altas_mes["razon_social"].isin(contrato.comisiones_vida.keys())]
    nuevas_mes_vida = altas_mes[altas_mes["razon_social"].isin(contrato.comisiones_vida.keys())]

    # La prima del primer recibo (el mismo que fija el periodo) se anualiza
    # si la póliza es mensual — ya no hace falta un lookup aparte a
    # Facturación "por el recibo más reciente".
    produccion_salud = 0.0
    for _, p in nuevas_mes_salud.iterrows():
        prima = p["prima_neta"]
        produccion_salud += prima if p["forma_pago"] == "A" else prima * 12

    resultado_rappel = calcular_rappel_inicial(
        contrato, fecha_referencia=hoy, produccion_mes_salud=produccion_salud
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Producción estimada del mes", f"{resultado_rappel.produccion_mes:,.2f} €")
    c2.metric("Objetivo del tramo", f"{resultado_rappel.objetivo_mes:,.2f} €")
    c3.metric(
        "Rappel estimado",
        f"{resultado_rappel.importe:,.2f} €",
        help=resultado_rappel.nota,
    )

    if resultado_rappel.confianza == "media":
        st.warning(
            f"⚠️ Estimación de confianza **media**: {resultado_rappel.nota}",
        )
    else:
        st.success("✅ Estimación de confianza alta (contrastada con datos reales).")

    st.progress(min(resultado_rappel.porcentaje_objetivo / 100, 1.0))
    st.caption(f"{resultado_rappel.porcentaje_objetivo:.1f}% del objetivo del tramo actual")

    # Proyección de cierre de mes: solo tiene sentido para el mes en curso
    # (esta pestaña, de momento, siempre calcula sobre "hoy").
    st.divider()
    st.subheader("Proyección de cierre de mes")
    dias_totales_mes = calendar.monthrange(hoy.year, hoy.month)[1]
    proyeccion = proyectar_cierre_mes(
        produccion_acumulada_hasta_hoy=produccion_salud,
        dia_actual_del_mes=hoy.day,
        dias_totales_del_mes=dias_totales_mes,
        contrato=contrato,
        fecha_referencia=hoy,
    )
    cp1, cp2 = st.columns(2)
    cp1.metric("Producción proyectada a fin de mes", f"{proyeccion.produccion_proyectada:,.2f} €")
    cp2.metric("Rappel proyectado a ese ritmo", f"{proyeccion.rappel_proyectado.importe:,.2f} €")
    st.info(f"📈 {proyeccion.mensaje}")
    st.caption(
        "Proyección estadística simple (ritmo diario medio × días del mes), "
        "no una predicción garantizada — nunca sustituye al dato real de Liquidación."
    )

    if not df_factura_pdf.empty:
        st.divider()
        st.subheader("Histórico de rappel real (confirmado)")
        hist_rappel = df_factura_pdf[df_factura_pdf["rappel"] > 0].sort_values("periodo")
        fig = px.bar(hist_rappel, x="periodo", y="rappel", color_discrete_sequence=[AZUL_ASISA])
        rappel_maximo = contrato.rappel_inicial.maximo
        rappel_minimo = contrato.rappel_inicial.minimo
        fig.add_hline(
            y=rappel_maximo, line_dash="dash",
            annotation_text=f"Máximo ({rappel_maximo:,.0f}€)",
        )
        fig.add_hline(
            y=rappel_minimo, line_dash="dot",
            annotation_text=f"Mínimo ({rappel_minimo:,.0f}€)",
        )
        st.plotly_chart(fig, width="stretch")

# =============================================================================
# TAB: Alertas
# =============================================================================
with tab_alertas:
    st.subheader("Pólizas pendientes de revisar")
    st.caption(
        "Pólizas activas cuya fecha de efecto ya debería haberse liquidado "
        f"(margen de {contrato.dias_margen_alerta} días) y no aparecen en "
        "ningún fichero de Liquidación importado."
    )

    if df_facturacion.empty:
        st.info("Sube al menos un mes de Facturación para poder calcular alertas.")
    else:
        primer_mes_con_datos = df_facturacion["fecha_desde"].min().date()
        alertas = detectar_polizas_sin_cobrar(
            df_polizas, df_liquidacion, contrato,
            fecha_hoy=date.today(), primer_mes_con_datos=primer_mes_con_datos,
        )
        if not alertas:
            st.success("✅ No hay pólizas pendientes de revisar ahora mismo.")
        else:
            st.warning(f"{len(alertas)} póliza(s) para revisar:")
            for a in alertas:
                with st.expander(f"Póliza {a.poliza} — {a.nombre_tomador} ({a.dias_desde_efecto} días)"):
                    st.write(f"**Entidad:** {a.razon_social}")
                    st.write(f"**Fecha de efecto:** {a.fecha_efecto}")
                    st.write(a.nota)
                    st.text_area("Resolución (tu nota, ej. 'error mío' / 'error ASISA')", key=f"nota_{a.poliza}")

# =============================================================================
# TAB: Insights
# =============================================================================
with tab_insights:
    st.subheader("Histórico y tendencias")
    st.caption(
        "Esta pestaña mira SIEMPRE todo el histórico de la base de datos, "
        "igual que Alertas — ignora el selector de periodo de las demás vistas."
    )

    df_produccion = construir_produccion_polizas(df_polizas, df_facturacion, contrato)

    if df_produccion.empty:
        st.info("Todavía no hay pólizas con fecha de efecto para calcular insights.")
    elif not hay_suficiente_historico(df_produccion):
        st.warning(
            "⚠️ Necesitas más histórico para ver tendencias (al menos 2 meses "
            "distintos de datos). De momento solo se muestran los rankings."
        )

    if not df_produccion.empty:
        if hay_suficiente_historico(df_produccion):
            st.markdown("### Evolución mensual de producción")
            evolucion = evolucion_mensual(df_produccion)

            fig_polizas = px.line(
                evolucion, x="periodo", y="polizas_nuevas", color="tipo", markers=True,
                color_discrete_map={"salud": AZUL_ASISA, "vida": "#F2A900"},
                title="Nº de pólizas nuevas por mes",
            )
            st.plotly_chart(fig_polizas, width="stretch")

            fig_prima = px.line(
                evolucion, x="periodo", y="prima_anual_total", color="tipo", markers=True,
                color_discrete_map={"salud": AZUL_ASISA, "vida": "#F2A900"},
                title="Prima anualizada total por mes",
            )
            st.plotly_chart(fig_prima, width="stretch")

            st.markdown("### Mes a mes")
            variacion = variacion_mes_actual_vs_anterior(df_produccion, date.today())
            if variacion.tendencia == "sin_datos":
                st.info(
                    f"Sin producción registrada en {variacion.periodo_anterior} "
                    "para poder comparar."
                )
            else:
                flecha = "↑" if variacion.tendencia == "subida" else "↓"
                st.metric(
                    f"Producción {variacion.periodo_actual} vs {variacion.periodo_anterior}",
                    f"{variacion.produccion_actual:,.2f} €",
                    delta=f"{flecha} {variacion.variacion_pct:.1f}%",
                )

        st.divider()
        col_rank1, col_rank2 = st.columns(2)
        with col_rank1:
            st.markdown("### Ranking de productos")
            fig_prod = px.bar(
                ranking_productos(df_produccion).head(10),
                x="prima_anual_total", y="razon_social", orientation="h",
                color_discrete_sequence=[AZUL_ASISA],
            )
            fig_prod.update_layout(yaxis={"categoryorder": "total ascending"})
            st.plotly_chart(fig_prod, width="stretch")

        with col_rank2:
            st.markdown("### Ranking de provincias")
            fig_prov = px.bar(
                ranking_provincias(df_produccion).head(10),
                x="prima_anual_total", y="provincia_tomador", orientation="h",
                color_discrete_sequence=[AZUL_ASISA],
            )
            fig_prov.update_layout(yaxis={"categoryorder": "total ascending"})
            st.plotly_chart(fig_prov, width="stretch")

    st.divider()
    st.markdown("### Próximos cambios de tarifa")
    st.caption(
        f"Pólizas de salud mensual a menos de {contrato.dias_antelacion_cambio_tarifa} "
        "días de cumplir su primer año: van a pasar de % producción a % mantenimiento."
    )
    alertas_tarifa = alertas_cambio_tarifa(df_polizas, contrato, date.today())
    if not alertas_tarifa:
        st.success("✅ Ninguna póliza próxima a cambiar de tarifa ahora mismo.")
    else:
        for a in alertas_tarifa:
            st.warning(
                f"Póliza {a.poliza} ({a.razon_social}): {a.dias_para_cambio} días "
                f"para el cambio de tarifa. {a.nota}"
            )
