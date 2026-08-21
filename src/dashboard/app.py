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
    cargar_eiac_polizas,
    cargar_eiac_polizas_riesgos,
    cargar_eiac_recibos,
    cargar_facturacion,
    cargar_liquidacion,
    cargar_polizas,
    cargar_polizas_provisionales_eiac,
    cargar_factura_pdf,
    recalcular_resumen_mensual,
)
from db.schema import conectar, inicializar_schema
from engine.calibracion import (
    calcular_calibracion,
    corregir_periodo_liquidacion_vida_mensual,
    estimar_comision_y_rappel_periodo,
)
from engine.comisiones import estimar_comision_poliza, resumen_historial_ajustes_cartera
from engine.config_contrato import cargar_contrato
from engine.eiac_integracion import integrar_eiac
from engine.fiscal import anios_disponibles, calcular_retenciones_anio
from engine.objetivo import calcular_objetivo_anual
from engine.wanderlust import calcular_pae_anual
from engine.insights import (
    alertas_cambio_tarifa,
    construir_produccion_polizas,
    evolucion_mensual,
    hay_suficiente_historico,
    periodos_futuros_con_datos,
    polizas_salud_mensual_historial_irregular,
    primeras_altas_por_periodo,
    ranking_productos,
    ranking_provincias,
    resumen_produccion_periodo,
    siguiente_periodo,
    variacion_mes_actual_vs_anterior,
)
from engine.proyeccion import proyectar_cierre_mes
from engine.reconciliacion import detectar_polizas_sin_cobrar
from ingestion.eiac_xml import (
    detectar_tipo_eiac,
    parsear_eiac_polizas,
    parsear_eiac_polizas_riesgos,
    parsear_eiac_recibos,
)
from ingestion.facturacion import parsear_facturacion
from ingestion.factura_pdf import parsear_factura_pdf
from ingestion.liquidacion import parsear_liquidacion
from ingestion.polizas import parsear_polizas
from dashboard.exportacion import construir_excel_completo

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

# --- Carga de datos desde la BD ------------------------------------------------
# Se hace ANTES del sidebar para poder mostrar ahí el resumen de "ficheros
# ya cargados" (qué periodos hay de cada tipo).
@st.cache_data(ttl=60)
def cargar_datos():
    polizas = pd.read_sql("SELECT * FROM polizas", conn, parse_dates=["fecha_emision", "fecha_efecto", "fecha_baja"])
    facturacion = pd.read_sql("SELECT * FROM facturacion", conn, parse_dates=["fecha_desde", "fecha_hasta"])
    liquidacion = pd.read_sql("SELECT * FROM liquidacion", conn, parse_dates=["fecha_desde", "fecha_hasta"])
    factura_pdf = pd.read_sql("SELECT * FROM factura_pdf", conn)
    eiac_polizas = pd.read_sql(
        "SELECT * FROM eiac_polizas", conn, parse_dates=["fecha_efecto_inicial", "fecha_emision"]
    )
    eiac_recibos = pd.read_sql(
        "SELECT * FROM eiac_recibos", conn, parse_dates=["fecha_efecto_inicial", "fecha_emision"]
    )
    return polizas, facturacion, liquidacion, factura_pdf, eiac_polizas, eiac_recibos


df_polizas, df_facturacion, df_liquidacion, df_factura_pdf, df_eiac_polizas, df_eiac_recibos = cargar_datos()


@st.cache_data(ttl=60)
def _construir_excel_completo_cacheado(
    df_polizas, df_facturacion, df_liquidacion, df_factura_pdf, df_eiac_polizas, df_eiac_recibos
) -> bytes:
    return construir_excel_completo(
        df_polizas=df_polizas,
        df_facturacion=df_facturacion,
        df_liquidacion=df_liquidacion,
        df_factura_pdf=df_factura_pdf,
        df_eiac_polizas=df_eiac_polizas,
        df_eiac_recibos=df_eiac_recibos,
        contrato=contrato,
    )

# EIAC "en vivo": las pólizas provisionales que ya se persistieron en
# `polizas` (ver sidebar) hacen que df_polizas ya las incluya solo con
# recargar; esto es un colchón adicional para el caso en que todavía no se
# hayan persistido (p.ej. datos insertados fuera de la UI) y, sobre todo,
# para traducir eiac_recibos a la forma de Facturación en cada carga, ya
# que esos recibos NUNCA se escriben en la tabla `facturacion` (ver
# engine.eiac_integracion). Solo se usa en Vista rápida/Rappel/Resumen —
# el resto de pestañas sigue viendo únicamente los datos oficiales.
_resultado_eiac = integrar_eiac(df_eiac_polizas, df_eiac_recibos, df_polizas, df_facturacion)
df_polizas_con_eiac = (
    # drop_duplicates(keep="last"): una póliza todavía provisional
    # (origen='EIAC') puede aparecer AQUÍ DOS VECES -- una vez ya
    # persistida en df_polizas (leída de la BD) y otra vez recién
    # regenerada en polizas_provisionales con datos más frescos (p.ej. el
    # ramo, que ahora permite asumir un % de comisión que antes no se
    # podía) -- construir_polizas_provisionales_desde_eiac solo excluye
    # pólizas YA OFICIALES, no las que siguen siendo provisionales, así
    # que sin este dedup cada una de esas pólizas contaría DOS VECES en
    # producción/comisión. Se queda con la última (la recién regenerada).
    pd.concat([df_polizas, _resultado_eiac.polizas_provisionales], ignore_index=True)
    .drop_duplicates(subset="poliza", keep="last")
    if not _resultado_eiac.polizas_provisionales.empty else df_polizas
)
df_facturacion_con_eiac = (
    pd.concat([df_facturacion, _resultado_eiac.facturacion_eiac], ignore_index=True)
    if not _resultado_eiac.facturacion_eiac.empty else df_facturacion
)
# Excepción puntual, solo Vida con ciclo mensual (duracion_recibo_meses==1):
# corrige periodo_liquidacion cuando el literal del CSV no coincide con el
# que le corresponde por su propia fecha_desde -- ver docstring de
# corregir_periodo_liquidacion_vida_mensual (caso real 64110228/64110254).
df_facturacion_con_eiac = corregir_periodo_liquidacion_vida_mensual(
    df_facturacion_con_eiac, df_polizas_con_eiac, contrato
)

# --- Cabecera -----------------------------------------------------------------
col_logo, col_titulo = st.columns([1, 4])
with col_logo:
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width=160)
with col_titulo:
    st.title("Panel Sebastián · Agente Exclusivo")
    st.caption("Comisiones y rappel — ASISA / ASISA VIDA")


def _formatear_periodos(periodos: list[str]) -> str:
    """"2026-04","2026-05","2026-07" -> "04, 05, 07-2026". Agrupa por año
    para que quede compacto cuando (lo normal) todos los periodos son del
    mismo año.

    OJO: Facturación/Factura PDF usan "AAAA-MM" pero Liquidación, en los
    ficheros reales de ASISA, usa "MM-AAAA" (confirmado con datos reales:
    "02-2026") — formatos distintos para el mismo concepto de columna
    "PER. LIQUIDACION". Se detecta cuál es cuál por longitud (4 dígitos es
    el año) en vez de asumir un orden fijo.
    """
    if not periodos:
        return "ninguno todavía"
    por_anio: dict[str, list[str]] = {}
    invalidos: list[str] = []
    for p in sorted(set(periodos)):
        partes = p.split("-")
        if len(partes) != 2:
            invalidos.append(p)
            continue
        a, b = partes
        anio, mes = (a, b) if len(a) == 4 else (b, a)
        por_anio.setdefault(anio, []).append(mes)
    texto = "; ".join(
        f"{', '.join(sorted(meses))}-{anio}" for anio, meses in sorted(por_anio.items())
    )
    if invalidos:
        texto = f"{texto}; {', '.join(invalidos)}" if texto else ", ".join(invalidos)
    return texto


def _aviso_comision_sin_razon_social(fusion_periodo: pd.DataFrame) -> None:
    """Avisa cuando parte de las altas del periodo son provisionales de
    EIAC sin `razon_social` confirmada — `estimar_comision_poliza` no
    puede buscar el % de comisión sin saber la entidad, así que esas
    pólizas aportan 0€ a "Comisión bruta estimada" aunque sí tengan
    producción. Sin este aviso, un 0€ ahí parece "no hay comisión" en vez
    de "no se puede estimar todavía" — mismo criterio que el resto del
    proyecto para no confundir ausencia de dato con un hecho real.
    """
    if fusion_periodo.empty:
        return
    n_sin_razon_social = int(fusion_periodo["razon_social"].isna().sum())
    if n_sin_razon_social == 0:
        return
    st.warning(
        f"⚠️ {n_sin_razon_social} alta(s) de este periodo son provisionales de "
        "EIAC sin entidad (razon_social) confirmada todavía — no se puede "
        "buscar su % de comisión, así que aportan 0€ a \"Comisión bruta "
        "estimada\" aunque sí cuentan en la producción y el rappel. Se "
        "estimará en cuanto llegue el CSV oficial de Pólizas."
    )


def _mostrar_bloque_produccion_periodo(periodo: str, etiqueta: str) -> None:
    """Bloque de métricas de un periodo: producción, comisión bruta
    estimada, rappel, total bruto y total NETO (el más destacado
    visualmente, a propósito — es la cifra que más quiere ver Sebastián
    de un vistazo). Usado por Vista rápida (mes actual/siguiente) y por
    el bloque de meses futuros con datos confirmados (normalmente EIAC).

    La comisión bruta reutiliza `estimar_comision_y_rappel_periodo` — el
    mismo cálculo que ya usa la pestaña Calibración — para no mantener
    dos fórmulas distintas de "lo que el motor estimaría" en el proyecto.
    """
    st.markdown(f"**{etiqueta} · {periodo}**")
    _resumen = resumen_produccion_periodo(df_polizas_con_eiac, df_facturacion_con_eiac, contrato, periodo)
    if not _resumen.tiene_datos:
        st.info(
            f"Todavía no hay datos de {periodo} — aparecerán en cuanto "
            "subas Facturación/Pólizas con ventas de ese periodo."
        )
        return
    if _resumen.polizas_detectadas == 0:
        st.warning(
            f"Hay recibos de {periodo} en Facturación, pero no se pudieron "
            "cruzar con ninguna póliza todavía — sube el CSV de Pólizas "
            "actualizado para completar este cálculo."
        )
        return

    _altas_periodo = primeras_altas_por_periodo(df_facturacion_con_eiac)
    _altas_periodo = _altas_periodo[_altas_periodo["periodo_liquidacion"] == periodo]
    # Inner join, igual criterio que resumen_produccion_periodo de arriba:
    # solo altas que sí cruzan con Pólizas, para que las métricas de producción
    # y el resto de cifras de este bloque partan del mismo conjunto de filas.
    _fusion_periodo = _altas_periodo.merge(df_polizas_con_eiac, on="poliza", how="inner")
    # TODOS los recibos del periodo (no solo primeras altas) -- Vida
    # devenga comisión en cada recibo cobrado, no solo en el primero.
    _recibos_periodo = df_facturacion_con_eiac[df_facturacion_con_eiac["periodo_liquidacion"] == periodo]
    _fusion_recibos_periodo = _recibos_periodo.merge(df_polizas_con_eiac, on="poliza", how="inner")
    _estimacion = estimar_comision_y_rappel_periodo(
        _fusion_periodo, contrato, periodo, _fusion_recibos_periodo, df_liquidacion
    )

    cm1, cm2, cm3 = st.columns(3)
    cm1.metric("Producción nueva Salud", f"{_estimacion.produccion_salud:,.2f} €")
    cm2.metric("Producción nueva Vida", f"{_estimacion.produccion_vida:,.2f} €")
    cm3.metric("Producción computada para rappel", f"{_estimacion.rappel.produccion_mes:,.2f} €", help=_estimacion.rappel.nota)
    cm4, cm5, cm6 = st.columns(3)
    cm4.metric("Comisión Salud estimada", f"{_estimacion.comision_salud:,.2f} €")
    cm5.metric(
        "Comisión Vida estimada", f"{_estimacion.comision_vida:,.2f} €",
        help="Incluye todos los recibos de Vida del periodo, también los recurrentes de pólizas vendidas en meses anteriores.",
    )
    cm6.metric(
        "Rappel estimado",
        f"{_estimacion.rappel.importe:,.2f} €",
        help=_estimacion.rappel.nota,
    )
    st.metric("Total bruto (comisión + rappel)", f"{_estimacion.total_bruto:,.2f} €")

    st.metric(
        "💰 Total NETO estimado",
        f"{_estimacion.total_neto:,.2f} €",
        help=(
            f"Total bruto tras aplicar la retención de IRPF del "
            f"{contrato.retencion_irpf:.0%} (config/contrato.yaml, "
            "aplicar_retencion()). Estimación del motor — confirmar "
            "siempre contra la Liquidación/Factura real."
        ),
    )
    _aviso_comision_sin_razon_social(_fusion_periodo)
    _mostrar_avisos_anualizacion_previa(_estimacion)
    _mostrar_aviso_historial_irregular(periodo)


def _mostrar_aviso_historial_irregular(periodo: str) -> None:
    """Aviso agregado + detalle expandible de pólizas de salud mensual con
    historial de ajustes irregulares entre las altas de `periodo`. Compartido
    por Vista rápida y la pestaña Rappel — mismo criterio, mismo aviso."""
    irregulares = polizas_salud_mensual_historial_irregular(
        df_polizas_con_eiac, df_facturacion_con_eiac, df_liquidacion, contrato, periodo
    )
    if not irregulares:
        return
    st.warning(
        f"⚠️ {len(irregulares)} de tus pólizas de salud mensual de este mes "
        "tienen historial de ajustes irregulares (ver detalle) — la "
        "estimación total puede desviarse más de lo habitual por esto."
    )
    with st.expander("Ver pólizas a vigilar"):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Póliza": p.poliza,
                        "Último ajuste conocido": p.ultimo_accion,
                        "Periodo del ajuste": p.ultimo_periodo,
                        "Importe (€)": p.ultimo_importe,
                    }
                    for p in irregulares
                ]
            ),
            width="stretch",
            hide_index=True,
        )


def _mostrar_avisos_anualizacion_previa(estimacion) -> None:
    """Explica exclusiones de producción sin cambiar la vigencia de pólizas."""
    if not estimacion.exclusiones_anualizacion_salud:
        return
    st.warning(
        "⚠️ Algunas pólizas no se incluyen como nueva producción porque ya consta "
        "una anualización previa vigente. Siguen vigentes; el recibo actual se conserva "
        "como movimiento de cartera."
    )
    with st.expander("Ver exclusiones por anualización previa"):
        st.dataframe(
            pd.DataFrame([
                {
                    "Póliza": estado.poliza,
                    "Periodo anualización previa": estado.periodo_anualizacion,
                    "Comisión anualizada (€)": estado.importe_anualizacion,
                    "Motivo": estado.motivo,
                    "Recibo actual": "Conservado como movimiento de cartera",
                }
                for estado in estimacion.exclusiones_anualizacion_salud
            ]),
            width="stretch", hide_index=True,
        )


# --- Sidebar: subida de ficheros ----------------------------------------------
# uploader_key se incrementa tras cada "Procesar" con éxito: cambiar la key
# de un file_uploader es la forma de vaciarlo (Streamlit no tiene un método
# directo para "limpiar" un uploader ya montado).
if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0
if "mensajes_pendientes" not in st.session_state:
    st.session_state.mensajes_pendientes = []

with st.sidebar:
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width=120)
    st.header("Subir ficheros")

    # Mensajes de éxito de la subida anterior (se guardan en session_state
    # porque el rerun que vacía los uploaders borra las variables locales).
    for m in st.session_state.mensajes_pendientes:
        st.success(m)
    st.session_state.mensajes_pendientes = []

    uk = st.session_state.uploader_key
    st.subheader("Semanal")
    f_facturacion = st.file_uploader(
        "Facturación (CSV)", type="csv", accept_multiple_files=True, key=f"facturacion_{uk}"
    )
    f_polizas = st.file_uploader(
        "Pólizas (CSV)", type="csv", accept_multiple_files=True, key=f"polizas_{uk}"
    )

    st.subheader("Mensual (cuando ASISA liquide)")
    f_liquidacion = st.file_uploader(
        "Liquidación (CSV)", type="csv", accept_multiple_files=True, key=f"liquidacion_{uk}"
    )
    f_factura_pdf = st.file_uploader(
        "Factura (PDF)", type="pdf", accept_multiple_files=True, key=f"factura_pdf_{uk}"
    )

    st.subheader("EIAC (TIREA, XML)")
    st.caption(
        "Se cruza automáticamente con Pólizas/Facturación por el número de "
        "póliza (parte final de IdPoliza) — así Rappel/Vista rápida/Resumen "
        "ya cuentan ventas llegadas solo por EIAC, sin esperar al CSV del "
        "portal. Se detecta si es de pólizas o de recibos por el nombre "
        "del fichero (EIAC-ENV-POLI-* / EIAC-ENV-RECI-*)."
    )
    f_eiac = st.file_uploader(
        "Ficheros EIAC (XML)", type="xml", accept_multiple_files=True, key=f"eiac_{uk}"
    )

    if st.button("Procesar ficheros subidos", type="primary", width="stretch"):
        mensajes = []
        try:
            for f in f_facturacion:
                df = parsear_facturacion(f)
                n = cargar_facturacion(conn, df)
                mensajes.append(f"Facturación ({f.name}): {n} filas nuevas importadas.")
            for f in f_polizas:
                df = parsear_polizas(f)
                n = cargar_polizas(conn, df)
                mensajes.append(f"Pólizas ({f.name}): {n} registros actualizados.")
            for f in f_liquidacion:
                df = parsear_liquidacion(f)
                n = cargar_liquidacion(conn, df)
                mensajes.append(f"Liquidación ({f.name}): {n} filas nuevas importadas.")
            for f in f_factura_pdf:
                facturas = parsear_factura_pdf(f)
                n = cargar_factura_pdf(conn, facturas)
                mensajes.append(f"Factura PDF ({f.name}): {n} entidad(es) importada(s).")
            for f in f_eiac:
                tipo = detectar_tipo_eiac(f.name)
                if tipo == "polizas":
                    df = parsear_eiac_polizas(f)
                    n = cargar_eiac_polizas(conn, df)
                    mensajes.append(f"EIAC Pólizas ({f.name}): {n} registros actualizados.")
                    # Mismo fichero, segunda pasada: el detalle de TODOS los
                    # asegurados (pólizas familiares con varios <Riesgo>),
                    # no solo el NumeroOrden=1 que ya guardó cargar_eiac_polizas.
                    f.seek(0)
                    df_riesgos = parsear_eiac_polizas_riesgos(f)
                    if not df_riesgos.empty:
                        cargar_eiac_polizas_riesgos(conn, df_riesgos)
                else:
                    df = parsear_eiac_recibos(f)
                    n = cargar_eiac_recibos(conn, df)
                    mensajes.append(f"EIAC Recibos ({f.name}): {n} filas nuevas importadas.")

            if f_eiac:
                # Crea en `polizas` las provisionales que EIAC trae y que el
                # CSV oficial todavía no tiene — se lee el estado actual de
                # la BD (no la variable df_polizas cacheada) para no perder
                # pólizas oficiales subidas en este mismo lote.
                df_polizas_bd = pd.read_sql("SELECT poliza, origen FROM polizas", conn)
                df_facturacion_bd = pd.read_sql("SELECT poliza FROM facturacion", conn)
                df_eiac_polizas_bd = pd.read_sql(
                    "SELECT * FROM eiac_polizas", conn,
                    parse_dates=["fecha_efecto_inicial", "fecha_emision"],
                )
                df_eiac_recibos_bd = pd.read_sql(
                    "SELECT * FROM eiac_recibos", conn,
                    parse_dates=["fecha_efecto_inicial", "fecha_emision"],
                )
                resultado_integracion = integrar_eiac(
                    df_eiac_polizas_bd, df_eiac_recibos_bd, df_polizas_bd, df_facturacion_bd
                )
                carga_provisionales = cargar_polizas_provisionales_eiac(
                    conn, resultado_integracion.polizas_provisionales
                )
                mensajes.append(
                    "EIAC: "
                    f"{carga_provisionales.nuevas} pólizas provisionales nuevas, "
                    f"{carga_provisionales.actualizadas} actualizadas y "
                    f"{carga_provisionales.sin_cambios} sin cambios. Total provisional "
                    f"en Pólizas: {carga_provisionales.total_provisionales}."
                    + (f" {carga_provisionales.ignoradas_oficiales} ignoradas por existir ya como oficiales."
                       if carga_provisionales.ignoradas_oficiales else "")
                )
                if resultado_integracion.no_reconocidos:
                    mensajes.append(
                        f"⚠️ EIAC: {len(resultado_integracion.no_reconocidos)} IdPoliza no "
                        "reconocido(s) — revisar en '📂 Ficheros ya cargados'."
                    )

            if not mensajes:
                st.warning("No has seleccionado ningún fichero.")
            else:
                recalcular_resumen_mensual(conn, contrato)
                st.cache_data.clear()
                # Vacía los uploaders (nueva key) y conserva los mensajes de
                # éxito para mostrarlos justo después del rerun.
                st.session_state.mensajes_pendientes = mensajes
                st.session_state.uploader_key += 1
                st.rerun()
        except ValueError as e:
            st.error(f"Error al procesar: {e}")

    with st.expander("📂 Ficheros ya cargados"):
        st.caption(f"**Facturación:** {_formatear_periodos(list(df_facturacion['periodo_liquidacion'].dropna().unique())) if not df_facturacion.empty else 'ninguno todavía'}")
        st.caption(f"**Liquidación:** {_formatear_periodos(list(df_liquidacion['periodo_liquidacion'].dropna().unique())) if not df_liquidacion.empty else 'ninguno todavía'}")
        st.caption(f"**Factura PDF:** {_formatear_periodos(list(df_factura_pdf['periodo'].dropna().unique())) if not df_factura_pdf.empty else 'ninguno todavía'}")
        if df_polizas.empty:
            st.caption("**Pólizas:** ninguna todavía")
        else:
            ultima_actualizacion = str(df_polizas["fecha_import"].max())[:19]
            st.caption(
                f"**Pólizas:** {len(df_polizas)} en cartera (foto completa, "
                f"sin periodos propios) · última actualización {ultima_actualizacion}"
            )
        if df_eiac_polizas.empty and df_eiac_recibos.empty:
            st.caption("**EIAC (TIREA):** ninguno todavía")
        else:
            n_provisionales_bd = int((df_polizas.get("origen") == "EIAC").sum()) if not df_polizas.empty else 0
            st.caption(
                f"**EIAC (TIREA):** {len(df_eiac_polizas)} póliza(s), "
                f"{len(df_eiac_recibos)} recibo(s) (deduplicados) · "
                f"{n_provisionales_bd} póliza(s) provisional(es) en Pólizas "
                "en total de cartera, pendientes de confirmar con el CSV oficial"
            )
        if _resultado_eiac.no_reconocidos:
            st.warning(
                f"⚠️ {len(_resultado_eiac.no_reconocidos)} IdPoliza de EIAC no se "
                "pudieron reconocer (formato inesperado) — revisar manualmente:"
            )
            st.dataframe(
                pd.DataFrame(
                    [
                        {"IdPoliza EIAC": r.id_poliza_eiac, "Motivo": r.motivo}
                        for r in _resultado_eiac.no_reconocidos
                    ]
                ),
                width="stretch",
                hide_index=True,
            )

    st.divider()
    st.subheader("Descargar todo")
    st.caption(
        "Un único Excel con todos los datos analizados (Pólizas, "
        "Facturación, Liquidación, Factura PDF, EIAC, Objetivo anual, "
        "Wanderlust/PAE y Calibración), más una hoja de avisos que señala "
        "qué meses todavía no tienen ningún dato cargado."
    )
    excel_bytes = _construir_excel_completo_cacheado(
        df_polizas_con_eiac, df_facturacion_con_eiac, df_liquidacion,
        df_factura_pdf, df_eiac_polizas, df_eiac_recibos,
    )
    st.download_button(
        "⬇️ Descargar Excel completo",
        data=excel_bytes,
        file_name=f"dashboard_ventas_{date.today().isoformat()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
    )

    st.divider()
    st.caption(
        "Los ficheros de Facturación/Pólizas dan una vista **estimada** "
        "en tiempo casi real. La Liquidación/Factura mensual es la que "
        "confirma los números **reales**."
    )

if df_polizas_con_eiac.empty and df_facturacion_con_eiac.empty:
    # Con las versiones "_con_eiac" (no las oficiales a secas): un usuario
    # que solo ha subido ficheros EIAC todavía (sin CSV oficial de
    # Facturación/Pólizas) sí tiene datos que mostrar — pararía aquí en
    # falso si comprobara solo df_polizas/df_facturacion.
    st.info(
        "Todavía no hay datos cargados. Sube al menos un fichero de "
        "Facturación y Pólizas (o EIAC) desde el panel lateral para empezar."
    )
    st.stop()

# --- Vista rápida: mes actual y siguiente --------------------------------------
# Visible ANTES de las pestañas, a propósito: es lo primero que se ve al
# abrir el dashboard, sin tener que navegar a buscarlo.
st.markdown("## 📅 Vista rápida — mes actual y siguiente")
st.caption(
    "Solo refleja lo que YA está subido a la base de datos — no es un "
    "recordatorio de ventas mencionadas de palabra. Si acabas de cerrar "
    "una póliza y no la ves aquí, sube el CSV de Facturación/Pólizas "
    "actualizado y vuelve a mirar."
)

_hoy_vista_rapida = date.today()
_periodo_actual = f"{_hoy_vista_rapida.year:04d}-{_hoy_vista_rapida.month:02d}"
_periodo_siguiente = siguiente_periodo(_periodo_actual)

col_periodo_actual, col_periodo_siguiente = st.columns(2)
for _col, _periodo, _etiqueta in (
    (col_periodo_actual, _periodo_actual, "Mes actual"),
    (col_periodo_siguiente, _periodo_siguiente, "Mes siguiente"),
):
    with _col:
        _mostrar_bloque_produccion_periodo(_periodo, _etiqueta)

# --- Producción confirmada más allá del mes siguiente --------------------------
# Típicamente por EIAC, que se adelanta al CSV oficial de Facturación (caso
# real: una póliza con FechaEfectoInicial en septiembre subida en julio). Va
# aquí, dentro de Vista rápida y no en Objetivo anual, porque es la misma
# idea que "mes actual/siguiente" de arriba (producción ya detectada de un
# periodo concreto, con su rappel) — Objetivo anual es una vista acumulada
# del año en curso hasta el mes de hoy, no está pensada para periodos
# sueltos más allá de la fecha actual. Si no hay ningún periodo futuro con
# datos, no se muestra nada (nunca un bloque vacío ni un 0€ engañoso).
_periodos_futuros = periodos_futuros_con_datos(df_facturacion_con_eiac, _periodo_siguiente)
if _periodos_futuros:
    st.markdown("### 📅 Producción confirmada — meses futuros")
    st.caption(
        "Periodos más allá del mes siguiente que YA tienen alguna venta "
        "detectada — normalmente porque llegó por EIAC antes que el CSV "
        "oficial de Facturación/Pólizas."
    )
    cols_futuros = st.columns(len(_periodos_futuros))
    for _col, _periodo in zip(cols_futuros, _periodos_futuros):
        with _col:
            _mostrar_bloque_produccion_periodo(_periodo, "Periodo futuro")

st.divider()

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
(
    tab_resumen, tab_polizas, tab_rappel, tab_alertas, tab_insights, tab_irpf,
    tab_objetivo, tab_wanderlust, tab_calibracion,
) = st.tabs(
    [
        "📊 Resumen", "📋 Pólizas", "🎯 Rappel", "⚠️ Alertas", "📈 Insights",
        "💶 Retenciones IRPF", "🎯 Objetivo anual", "🏆 Wanderlust / PAE", "📐 Calibración",
    ]
)

# =============================================================================
# TAB: Resumen
# =============================================================================
with tab_resumen:
    st.subheader("Resumen general (cartera completa, sin filtro de periodo)")

    polizas_activas = df_polizas_con_eiac[df_polizas_con_eiac["situacion"] == "A"]
    c1, c2 = st.columns(2)
    c1.metric(
        "Pólizas activas", len(polizas_activas),
        help="Incluye provisionales de EIAC pendientes de confirmar con el CSV oficial.",
    )
    c2.metric("Provincias distintas", df_polizas_con_eiac["provincia_tomador"].nunique())

    if not df_factura_pdf.empty:
        st.markdown("**Última factura confirmada, por entidad**")
        st.caption(
            "Siempre por separado: ASISA Salud y ASISA Vida son facturas "
            "independientes con su propio periodo — Vida nunca tiene "
            "rappel, y mezclarlas en una sola cifra puede coger la entidad "
            "equivocada si ambas están disponibles para el mismo mes."
        )
        cols_entidad = st.columns(len(contrato.entidades))
        for col, datos_entidad in zip(cols_entidad, contrato.entidades.values()):
            facturas_entidad = df_factura_pdf[
                df_factura_pdf["entidad_cif"] == datos_entidad["razon_social_cif"]
            ]
            nombre = datos_entidad["nombre"]
            if facturas_entidad.empty:
                col.metric(nombre, "Sin facturas todavía")
                continue
            ultimo = facturas_entidad.sort_values("periodo").iloc[-1]
            if datos_entidad["tiene_rappel"]:
                ayuda = f"Rappel de {ultimo['periodo']}: {ultimo['rappel']:.2f} €"
            else:
                ayuda = "Esta entidad nunca tiene rappel (no existe ese Anexo en su contrato)."
            col.metric(
                f"{nombre} · {ultimo['periodo']}",
                f"{ultimo['total_factura']:.2f} €",
                help=ayuda,
            )

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
    st.caption(
        "Incluye provisionales de EIAC pendientes de confirmar con el CSV "
        "oficial (columna Origen='EIAC') — mira Nota origen para ver si su "
        "producción está confirmada por un recibo real o si es todavía "
        "provisional por fecha de efecto (⚠️ PRODUCCIÓN PROVISIONAL)."
    )
    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        filtro_producto = st.multiselect(
            "Producto", options=sorted(df_polizas_con_eiac["razon_social"].dropna().unique())
        )
    with col_f2:
        filtro_provincia = st.multiselect(
            "Provincia", options=sorted(df_polizas_con_eiac["provincia_tomador"].dropna().unique())
        )
    with col_f3:
        opciones_situacion = sorted(df_polizas_con_eiac["situacion"].dropna().unique())
        default_situacion = ["A"] if "A" in opciones_situacion else []
        filtro_situacion = st.multiselect(
            "Situación", options=opciones_situacion, default=default_situacion
        )

    df_mostrar = df_polizas_con_eiac.copy()
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
                "fecha_efecto", "provincia_tomador", "nombre_tomador", "origen", "nota_origen",
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
    altas_mes = primeras_altas_por_periodo(df_facturacion_con_eiac)
    altas_mes = altas_mes[altas_mes["periodo_liquidacion"] == mes_texto]
    fusion_mes = altas_mes.merge(df_polizas_con_eiac, on="poliza", how="inner")
    # TODOS los recibos del mes (no solo primeras altas) -- Vida devenga
    # comisión en cada recibo cobrado, no solo en el primero.
    recibos_mes = df_facturacion_con_eiac[df_facturacion_con_eiac["periodo_liquidacion"] == mes_texto]
    fusion_recibos_mes = recibos_mes.merge(df_polizas_con_eiac, on="poliza", how="inner")

    # Misma función que Calibración y Vista rápida — no duplicar la fórmula
    # de comisión/rappel/neto en cada pestaña (ver engine.calibracion).
    estimacion_mes = estimar_comision_y_rappel_periodo(
        fusion_mes, contrato, mes_texto, fusion_recibos_mes, df_liquidacion
    )
    resultado_rappel = estimacion_mes.rappel
    produccion_salud = estimacion_mes.produccion_salud
    produccion_vida = estimacion_mes.produccion_vida

    c1, c2, c3 = st.columns(3)
    c1.metric("Producción nueva Salud", f"{estimacion_mes.produccion_salud:,.2f} €")
    c2.metric("Producción nueva Vida", f"{estimacion_mes.produccion_vida:,.2f} €")
    c3.metric("Producción computada para rappel", f"{resultado_rappel.produccion_mes:,.2f} €", help=resultado_rappel.nota)
    c4, c5, c6 = st.columns(3)
    c4.metric("Comisión Salud estimada", f"{estimacion_mes.comision_salud:,.2f} €")
    c5.metric("Comisión Vida estimada", f"{estimacion_mes.comision_vida:,.2f} €", help="Incluye todos los recibos de Vida del periodo, también los recurrentes de pólizas vendidas en meses anteriores.")
    c6.metric(
        "Rappel estimado",
        f"{resultado_rappel.importe:,.2f} €",
        help=resultado_rappel.nota,
    )

    c7, c8 = st.columns(2)
    c7.metric("Total bruto (comisión + rappel)", f"{estimacion_mes.total_bruto:,.2f} €")
    c8.metric(
        "💰 Total NETO estimado",
        f"{estimacion_mes.total_neto:,.2f} €",
        help=(
            f"Total bruto tras aplicar la retención de IRPF del "
            f"{contrato.retencion_irpf:.0%} (config/contrato.yaml)."
        ),
    )

    _n_altas_eiac = int((altas_mes["poliza"].isin(_resultado_eiac.facturacion_eiac["poliza"])).sum())
    if _n_altas_eiac:
        st.caption(
            f"📐 Incluye {_n_altas_eiac} alta(s) que solo están en EIAC todavía "
            "(sin Facturación/Pólizas oficial de este periodo)."
        )
    _aviso_comision_sin_razon_social(fusion_mes)
    _mostrar_avisos_anualizacion_previa(estimacion_mes)

    if resultado_rappel.confianza == "media":
        st.warning(
            f"⚠️ Estimación de confianza **media**: {resultado_rappel.nota}",
        )
    else:
        st.success("✅ Estimación de confianza alta (contrastada con datos reales).")

    st.progress(min(resultado_rappel.porcentaje_objetivo / 100, 1.0))
    st.caption(f"{resultado_rappel.porcentaje_objetivo:.1f}% del objetivo del tramo actual")

    _mostrar_aviso_historial_irregular(mes_texto)

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
        produccion_mes_vida=produccion_vida,
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
    st.markdown("### Pólizas próximas a cumplir 12 meses")
    st.caption(
        f"Pólizas de salud mensual a menos de {contrato.dias_antelacion_cambio_tarifa} "
        "días de cumplir su primer aniversario. Podría cambiar el criterio o porcentaje de comisión, "
        "pero todavía no está confirmado con una Liquidación real. El motor no aplica automáticamente "
        "el porcentaje de mantenimiento."
    )
    alertas_tarifa = alertas_cambio_tarifa(df_polizas, contrato, date.today())
    if not alertas_tarifa:
        st.success("✅ Ninguna póliza próxima a cumplir 12 meses ahora mismo.")
    else:
        for a in alertas_tarifa:
            st.warning(
                f"Póliza {a.poliza} ({a.razon_social}): {a.dias_para_cambio} días "
                f"para cumplir 12 meses. {a.nota}"
            )

# =============================================================================
# TAB: Retenciones IRPF
# =============================================================================
with tab_irpf:
    st.subheader("Retenciones IRPF ya practicadas")
    st.caption(
        "Dato real, tomado de las Facturas PDF ya confirmadas — pensado para "
        "anticipar la declaración de la renta del año siguiente con una cifra "
        "consultable. Esto NO es asesoría fiscal: no se calcula IRPF a pagar "
        "ni se proyecta nada, solo se suma lo que ya consta como retenido."
    )

    if df_factura_pdf.empty:
        st.info("Sube al menos una Factura PDF para ver las retenciones.")
    else:
        anios = anios_disponibles(df_factura_pdf)
        anio_actual = str(date.today().year)
        indice_default = anios.index(anio_actual) if anio_actual in anios else 0
        anio_sel = st.selectbox("Año fiscal", anios, index=indice_default)

        retencion = calcular_retenciones_anio(df_factura_pdf, anio_sel)

        if retencion.desglose_mensual.empty:
            st.info(f"Todavía no hay Facturas PDF de {anio_sel}.")
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric(f"Total retenido en {anio_sel}", f"{retencion.total_retenido:,.2f} €")
            c2.metric("Base factura total", f"{retencion.total_base:,.2f} €")
            c3.metric("Total factura neto", f"{retencion.total_factura_neto:,.2f} €")

            st.divider()
            st.markdown("### Desglose mensual")
            fig_irpf = px.bar(
                retencion.desglose_mensual, x="periodo", y="irpf_retenido",
                color_discrete_sequence=[AZUL_ASISA],
                labels={"periodo": "Periodo", "irpf_retenido": "IRPF retenido (€)"},
            )
            st.plotly_chart(fig_irpf, width="stretch")
            st.dataframe(
                retencion.desglose_mensual.rename(
                    columns={
                        "periodo": "Periodo",
                        "base_factura": "Base factura (€)",
                        "irpf_retenido": "IRPF retenido (€)",
                        "total_factura": "Total factura neto (€)",
                    }
                ),
                width="stretch",
                hide_index=True,
            )

            st.markdown("### Desglose por entidad")
            st.dataframe(
                retencion.desglose_entidad.rename(
                    columns={
                        "entidad_nombre": "Entidad",
                        "base_factura": "Base factura (€)",
                        "irpf_retenido": "IRPF retenido (€)",
                        "total_factura": "Total factura neto (€)",
                    }
                ),
                width="stretch",
                hide_index=True,
            )

# =============================================================================
# TAB: Objetivo anual
# =============================================================================
with tab_objetivo:
    st.subheader("Objetivo de producción del año")
    st.caption(
        "Suma toda la producción nueva del año natural en curso (salud "
        "mensual anualizada, salud prepago anual íntegra, y vida "
        "anualizada), usando el mismo periodo real (periodo_liquidacion) "
        "que el resto del dashboard — no el mes calendario de fecha_efecto. "
        "Incluye también las altas que solo están en EIAC todavía, igual "
        "que Vista rápida/Rappel/Resumen."
    )

    _hoy_objetivo = date.today()
    objetivo_input = st.number_input(
        "Objetivo anual (€)",
        min_value=0.0,
        value=float(contrato.objetivo_produccion_anual),
        step=1000.0,
        help=(
            "Por defecto viene de config/contrato.yaml "
            "(objetivos.produccion_anual). Cambiarlo aquí solo afecta a "
            "esta sesión del dashboard."
        ),
    )

    resultado_objetivo = calcular_objetivo_anual(
        df_polizas_con_eiac, df_facturacion_con_eiac, contrato,
        anio=_hoy_objetivo.year, mes_hasta=_hoy_objetivo.month, objetivo=objetivo_input,
    )

    st.metric(
        f"Producción acumulada en {resultado_objetivo.anio}",
        f"{resultado_objetivo.produccion_total:,.2f} € de {resultado_objetivo.objetivo:,.2f} €",
    )
    st.progress(min(resultado_objetivo.porcentaje / 100, 1.0))
    st.caption(f"{resultado_objetivo.porcentaje:.1f}% del objetivo anual")

    if resultado_objetivo.meses_incompletos:
        st.warning(
            "⚠️ Datos incompletos en: "
            f"{', '.join(resultado_objetivo.meses_incompletos)} — falta subir "
            "Facturación y/o Pólizas de esos periodos. El acumulado de arriba "
            "NO los incluye, así que probablemente sea mayor en realidad."
        )

    if resultado_objetivo.meses_eiac_parcial:
        st.warning(
            "📐 Periodos que dependen SOLO de EIAC, sin ninguna fila de "
            "Facturación oficial todavía: "
            f"{', '.join(resultado_objetivo.meses_eiac_parcial)} — sí cuentan "
            "en el acumulado de arriba, pero su importe puede cambiar cuando "
            "llegue el CSV oficial de ese periodo (que siempre tiene prioridad)."
        )

    st.divider()
    st.markdown("### Desglose por tipo y mes")
    tabla_meses = pd.DataFrame(
        [
            {
                "Periodo": m.periodo,
                "Salud mensual (€)": m.salud_mensual,
                "Salud anual (€)": m.salud_anual,
                "Vida (€)": m.vida,
                "Total (€)": m.total,
                "¿Completo?": "✅" if m.completo else "⚠️ faltan datos",
                "¿Fuente?": "📐 Solo EIAC" if m.es_eiac_parcial else "✅ CSV oficial",
            }
            for m in resultado_objetivo.meses
        ]
    )
    fig_objetivo = px.bar(
        tabla_meses, x="Periodo",
        y=["Salud mensual (€)", "Salud anual (€)", "Vida (€)"],
        color_discrete_sequence=[AZUL_ASISA, "#F2A900", "#7DB343"],
    )
    st.plotly_chart(fig_objetivo, width="stretch")
    st.dataframe(tabla_meses, width="stretch", hide_index=True)

# =============================================================================
# TAB: Wanderlust / PAE — incentivo comercial ASISA 2026
# =============================================================================
with tab_wanderlust:
    st.subheader("Wanderlust / PAE — incentivo comercial ASISA 2026")
    st.caption(
        "PAE (Primas Anualizadas Equivalentes) = variación de cartera "
        "anualizada por producto, ponderada por un multiplicador (config/"
        "contrato.yaml, wanderlust.multiplicadores_paes), entre el 1 de "
        "enero y el 31 de diciembre de 2026. DISTINTO del 'Objetivo anual' "
        "de al lado: no es producción bruta en euros. Y a diferencia del "
        "resto del dashboard, aquí NO se usa periodo_liquidacion ni el "
        "ciclo 16→15 — una póliza cuenta en cuanto su fecha de efecto cae "
        "dentro del año natural 2026, sin importar cuándo se factura. "
        "Incluye también las altas que solo están en EIAC todavía, igual "
        "que Vista rápida/Objetivo anual."
    )
    st.info(
        "⚠️ Multirramo Salud + Accidentes: el agente a veces vende "
        "Accidentes dentro de una póliza de Salud, pero con los datos "
        "actuales no hay forma fiable de distinguirlo — el campo SUBRAMO de "
        "Liquidación solo se ha visto con el valor 'VACIO' y ni siquiera se "
        "guarda hoy en la base de datos, y EIAC tampoco trae una señal "
        "equivalente. Estas pólizas se clasifican solo por su razon_social "
        "de Salud, así que el Accidentes incluido dentro de una multirramo "
        "puede que no se contabilice aparte en 'ASISA Accidentes' todavía "
        "— pendiente de confirmar con más datos."
    )

    _anio_pae = 2026
    _objetivo_pae_config = contrato.wanderlust_objetivo_paes
    objetivo_pae_input = st.number_input(
        "Objetivo PAE individual (opcional, lo comunica ASISA por agente)",
        min_value=0.0,
        value=float(_objetivo_pae_config) if _objetivo_pae_config else 0.0,
        step=100.0,
        help=(
            "Por defecto viene de config/contrato.yaml "
            "(wanderlust.objetivo_paes). Déjalo en 0 mientras no lo sepas — "
            "sin objetivo no se muestra ningún % de cumplimiento inventado, "
            "solo el PAE acumulado en bruto."
        ),
    )
    objetivo_pae = objetivo_pae_input if objetivo_pae_input > 0 else None

    resultado_pae = calcular_pae_anual(
        df_polizas_con_eiac, df_facturacion_con_eiac, contrato,
        anio=_anio_pae, objetivo=objetivo_pae,
    )

    if objetivo_pae:
        st.metric(
            f"PAE acumulado {_anio_pae}",
            f"{resultado_pae.pae_total:,.2f} de {objetivo_pae:,.2f}",
        )
        st.progress(min((resultado_pae.porcentaje or 0) / 100, 1.0))
        st.caption(f"{resultado_pae.porcentaje:.1f}% del objetivo PAE")
    else:
        st.metric(f"PAE acumulado {_anio_pae}", f"{resultado_pae.pae_total:,.2f}")
        st.caption(
            "Sin objetivo PAE individual definido todavía — solo se "
            "muestra el acumulado bruto, sin % de cumplimiento."
        )

    if resultado_pae.sin_categoria:
        st.warning(
            f"⚠️ {resultado_pae.sin_categoria} póliza(s) con fecha de "
            f"efecto en {_anio_pae} no se pudieron clasificar en ninguna "
            "categoría PAE (producto/razon_social desconocido) — no suman "
            "al acumulado de arriba."
        )

    st.divider()
    st.markdown("### Desglose por producto")
    tabla_pae = pd.DataFrame(
        [
            {
                "Categoría": d.categoria,
                "PAE (€)": d.pae,
                "Altas": d.polizas_alta,
                "Anuladas": d.polizas_baja,
            }
            for d in sorted(resultado_pae.por_categoria.values(), key=lambda d: -d.pae)
        ]
    )
    if not tabla_pae.empty:
        fig_pae = px.bar(
            tabla_pae, x="Categoría", y="PAE (€)",
            color_discrete_sequence=[AZUL_ASISA],
        )
        st.plotly_chart(fig_pae, width="stretch")
        st.dataframe(tabla_pae, width="stretch", hide_index=True)
    else:
        st.info(f"Todavía no hay pólizas con fecha de efecto en {_anio_pae} clasificadas.")

# =============================================================================
# TAB: Calibración del motor — estimado vs. real
# =============================================================================
with tab_calibracion:
    st.subheader("Calibración del motor — estimado vs. real")
    st.caption(
        "Para cada periodo donde YA hay Facturación+Pólizas completas Y "
        "Factura PDF real, recalcula lo que el motor de estimación (el "
        "mismo de Rappel y Vista rápida: `estimar_comision_poliza` + "
        "`calcular_rappel_inicial`) habría predicho, ignorando el dato "
        "real, y lo compara contra la Factura PDF de ese mismo mes. "
        "Sirve para saber cuánto fiarse del número en pantalla mientras "
        "el mes está en curso."
    )

    # df_polizas_con_eiac/df_facturacion_con_eiac -- las MISMAS fuentes que
    # usan Vista rápida y Rappel (ver estimar_comision_y_rappel_periodo más
    # abajo) -- para medir la fiabilidad del número que el agente realmente
    # ve en pantalla, no una versión aparte calculada solo con el CSV
    # oficial (bug real: subestimaba julio 2026 en +1.250€).
    resultado_calibracion = calcular_calibracion(
        df_polizas_con_eiac, df_facturacion_con_eiac, df_factura_pdf, contrato, df_liquidacion
    )

    if not resultado_calibracion.periodos and not resultado_calibracion.excluidos:
        st.info("Sube al menos una Factura PDF para poder calibrar el motor.")
    else:
        if resultado_calibracion.periodos:
            tabla_calibracion = pd.DataFrame(
                [
                    {
                        "Periodo": p.mes,
                        "Estimado (€)": p.estimado,
                        "Real (€)": p.real,
                        "Diferencia (€)": p.diferencia,
                        "Diferencia (%)": p.diferencia_pct,
                    }
                    for p in resultado_calibracion.periodos
                ]
            )
            st.markdown("### Periodos incluidos en la calibración")
            st.dataframe(tabla_calibracion, width="stretch", hide_index=True)

            c1, c2 = st.columns(2)
            sesgo_eur = resultado_calibracion.sesgo_medio_eur
            sesgo_pct = resultado_calibracion.sesgo_medio_pct
            c1.metric(
                "Sesgo medio (€)",
                f"{sesgo_eur:+,.2f} €" if sesgo_eur is not None else "—",
                help="Estimado - Real, promediado entre los periodos incluidos. "
                     "Positivo = el motor tiende a pasarse; negativo = a quedarse corto.",
            )
            c2.metric(
                "Sesgo medio (%)",
                f"{sesgo_pct:+.1f}%" if sesgo_pct is not None else "—",
                help="Sirve como referencia de +/-X% sobre lo que ves en pantalla "
                     "mientras el mes está en curso.",
            )

            fig_calibracion = px.bar(
                tabla_calibracion, x="Periodo", y=["Estimado (€)", "Real (€)"],
                barmode="group", color_discrete_sequence=[AZUL_ASISA, "#F2A900"],
            )
            st.plotly_chart(fig_calibracion, width="stretch")
        else:
            st.info(
                "Hay Facturas PDF reales, pero ningún periodo tiene "
                "Facturación/Pólizas completas todavía para poder calibrar."
            )

        if resultado_calibracion.excluidos:
            st.divider()
            st.markdown("### Periodos excluidos de la calibración")
            st.caption(
                "Estos meses NO se cuentan como fallos del motor: falta el dato "
                "de entrada (Facturación/Pólizas) para poder recalcular un "
                "estimado, así que no hay nada que comparar todavía."
            )
            for e in resultado_calibracion.excluidos:
                st.warning(f"**{e.periodo}**: {e.motivo}")

    st.divider()
    st.markdown("### ¿Por qué el rango de error es tan amplio?")
    _resumen_historial = resumen_historial_ajustes_cartera(df_polizas, df_liquidacion, contrato)
    if _resumen_historial.total_salud_mensual and _resumen_historial.pct_irregular is not None:
        _pct_sin_ajustes = round(100 - _resumen_historial.pct_irregular, 1)
        st.info(
            "📎 Investigación real sobre la regularización de Salud mensual: en la "
            f"cartera actual, {_resumen_historial.total_salud_mensual - _resumen_historial.con_historial_irregular} "
            f"de {_resumen_historial.total_salud_mensual} pólizas de salud mensual "
            f"({_pct_sin_ajustes}%) nunca han mostrado más de un evento de ajuste en "
            "Liquidación — se comportan como el caso simple del contrato (anticipo "
            "íntegro, sin regularización posterior). El resto sí tuvo algún ajuste "
            "(EXTORNO ANUALIZADA o una segunda ANUALIZADA), pero el importe y el "
            "momento varían de forma no predecible póliza a póliza — no siguen una "
            "fórmula común. Por eso el motor no intenta adivinar el importe del "
            "ajuste, solo avisa (en Rappel/Vista rápida) de qué pólizas concretas "
            "del mes ya tienen ese historial irregular."
        )
    else:
        st.caption(
            "Todavía no hay suficientes datos de Pólizas/Liquidación para calcular "
            "esta cifra de contexto."
        )
