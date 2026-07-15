"""Integración del canal EIAC con el motor de rappel/comisiones.

Correspondencia CONFIRMADA por Sebastián con 5 casos reales cruzados a
mano (64276918, 64254004, 64254007, 64261922, 64201679/64174100): la
parte tras el ÚLTIMO guión de `IdPoliza` coincide con el número de póliza
que usan Facturación/Pólizas/Liquidación de ASISA (ver
`ingestion.eiac_xml.extraer_numero_poliza_asisa`).

Con esa correspondencia, este módulo:
  1. Traduce `eiac_recibos` a un DataFrame con la misma forma que
     Facturación (poliza/periodo_liquidacion/prima_neta/fecha_desde), para
     que `engine.insights.primeras_altas_por_periodo` —y todo lo que
     depende de él: Rappel, Vista rápida, Resumen— incluya también las
     ventas que solo están en EIAC, sin esperar al CSV oficial.
  2. Genera filas PROVISIONALES de `polizas` a partir de `eiac_polizas`,
     solo para números de póliza que todavía no existen en la tabla
     oficial — nunca sobreescribe una póliza ya confirmada por el CSV.

RECONSTRUCCIÓN del ciclo 16→15 (confianza alta, no un dato directo de
EIAC): EIAC no trae la columna `periodo_liquidacion` que sí calcula
ASISA en Facturación/Liquidación, así que `construir_facturacion_desde_eiac`
la reconstruye aplicando la misma regla que ya documenta
`engine.insights.primeras_altas_por_periodo` (ventana de devengo
16-mes_M → 15-mes_M+1, perteneciente al periodo M+1) — confirmada con un
caso real de esa misma fuente (fecha_efecto 30/06/2026 → periodo
"2026-07"). Verificada de nuevo aquí con los 5 casos reales de julio de
Sebastián: dos de ellos (64174100, 64201679) tienen FechaEfectoInicial
30/06/2026 y SÍ caen en el periodo "2026-07" con esta regla, cuadrando
con la producción real verificada a mano (6.366,60€) — ver
`tests/test_eiac_integracion.py`. Sigue sin ser un dato confirmado
directamente por ASISA para el canal EIAC (podría haber un caso futuro
que la contradiga); cuando llegue el CSV de Facturación oficial de ese
mes, su periodo_liquidacion real debe prevalecer sobre esta reconstrucción.

AVISO sobre renumeración: el caso real 64201679/64174100 indica que, al
menos una vez, el número extraído de `IdPoliza` no coincidió 1:1 con el
número que apareció más tarde en el CSV oficial de Pólizas (posible
renovación/cambio de número). La validación de formato de
`extraer_numero_poliza_asisa` NO detecta este caso (el número extraído
es perfectamente válido) — solo una reconciliación posterior contra el
CSV oficial lo revela. Por eso las pólizas provisionales se marcan
siempre con `origen='EIAC'` y `nota_origen`, y nunca sobreescriben una
fila ya oficial (eso lo garantiza `db.carga.cargar_polizas_provisionales_eiac`
con INSERT OR IGNORE, no este módulo).

LÍMITE REAL observado al procesar los 8 ficheros EIAC completos de
Sebastián (no solo el fixture con los 5 casos aislados): el recibo de
64174100 SÍ está en los ficheros RECI reales, pero su póliza NUNCA
aparece en ninguno de los 3 ficheros POLI reales — probablemente por la
misma renumeración de arriba (¿la póliza "vive" bajo 64201679 del lado
POLI?). Sin un `<Poliza>` de origen, `construir_polizas_provisionales_desde_eiac`
no puede crear una fila provisional para ella (no hay forma_pago que
inferir), así que `resumen_produccion_periodo` la deja fuera del cálculo
de producción hasta que llegue esa póliza por POLI o por el CSV oficial.

EL CSV OFICIAL SIEMPRE GANA — regla añadida tras encontrar la primera
discrepancia real entre fuentes (Sebastián, verificación manual del
Objetivo anual): 6 pólizas con historial en el CSV oficial de Facturación
(a partir de abril) TAMBIÉN tenían un recibo EIAC con fecha de efecto de
febrero — y como `primeras_altas_por_periodo` se queda con el
periodo_liquidacion más antiguo entre fuentes, EIAC "ganaba" y desplazaba
la alta a marzo, con un importe que en 4 de los 6 casos NO coincidía con
el recibo mensual recurrente del CSV oficial (63946797: 97,78€ EIAC vs
49,59€ CSV; 63949664: exactamente el doble). En 7 meses de proyecto, el
CSV oficial ha coincidido siempre al céntimo contra la factura PDF real;
EIAC es nuevo y acaba de mostrar su primera discrepancia. Por eso ahora
`construir_facturacion_desde_eiac` IGNORA por completo cualquier recibo
EIAC de una póliza que ya tenga alguna fila en la Facturación oficial —
ni para el importe ni para el periodo. EIAC solo rellena huecos que el
CSV oficial todavía no cubre, nunca los sobrescribe ni se adelanta.

CONFIANZA DE LA COMISIÓN EN PÓLIZAS PROVISIONALES: EIAC no da la
`razon_social` exacta (el producto concreto: Particulares/Red Sanitaria/
etc.), pero SÍ da `DescripcionRamo` ("Asistencia sanitaria") y
`CodigoEntidad/CodigoInterno` ("Asisa"), confirmados con datos reales —
suficiente para saber que es Salud, aunque no qué producto exacto. Para
esos casos, `construir_polizas_provisionales_desde_eiac` asume
"ASISA PARTICULARES" (el producto más habitual en la cartera; varias
entidades de Salud comparten el mismo 25%/20%) en vez de dejar la
comisión en 0€ — marcado con `razon_social_asumida=True` para que
`engine.calibracion.estimar_comision_y_rappel_periodo` baje la confianza
de esa estimación a "baja" con una nota explícita, en vez de presentarla
con la misma confianza que una póliza con producto ya confirmado.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from engine.insights import siguiente_periodo
from ingestion.eiac_xml import NumeroPolizaExtraido, extraer_numero_poliza_asisa

# Día que marca el corte del ciclo real de ASISA: [16 de mes_M, 15 de
# mes_M+1] pertenece al periodo mes_M+1. Ver docstring del módulo.
_DIA_CORTE_CICLO = 16

# ClaseFormaPago -> forma_pago provisional. PISTA, no un hecho confirmado
# (ver `ingestion.eiac_xml._pista_forma_pago`) — se usa solo como valor de
# arranque hasta que llegue el CSV oficial de Pólizas.
_PISTA_A_FORMA_PAGO = {
    "posible_mensual": "M",
    "posible_prepago_anual": "A",
}

# SituacionPoliza (EIAC) -> situacion (ASISA). "EV" (en vigor) -> "A" SÍ
# está confirmado: es el único valor visto en los 8 ficheros reales de
# Sebastián. "BJ" -> "B" sigue siendo una suposición razonable sin
# confirmar (ninguna póliza de baja en esos 8 ficheros todavía); si el
# código real no está aquí, se deja el código EIAC tal cual en vez de
# forzar un valor.
_SITUACION_EIAC_A_ASISA = {
    "EV": "A",
    "BJ": "B",
}

# Señales de Salud confirmadas con datos reales (ver docstring del
# módulo). No hay ninguna señal de Vida confirmada todavía — por eso solo
# se detecta "es Salud" (afirmativo), nunca "es Vida" por descarte.
_TERMINOS_RAMO_SALUD = ("sanitaria", "salud")
_CODIGOS_ENTIDAD_SALUD_CONFIRMADOS = {"asisa"}

# razon_social por defecto cuando se confirma Salud pero no el producto
# exacto — el más habitual en la cartera; ver docstring del módulo.
RAZON_SOCIAL_SALUD_POR_DEFECTO = "ASISA PARTICULARES"

COLUMNAS_FACTURACION_EIAC = [
    "poliza", "cliente_codigo", "cartera", "producto_nombre",
    "fecha_desde", "fecha_hasta", "prima_neta", "prima_total",
    "periodo_liquidacion", "duracion_recibo_meses",
]

COLUMNAS_POLIZAS_PROVISIONALES = [
    "poliza", "cliente_codigo", "razon_social", "producto_base", "producto_codigo",
    "fecha_emision", "fecha_efecto", "fecha_baja", "forma_pago", "situacion",
    "provincia_tomador", "delegacion", "nombre_tomador", "origen", "nota_origen",
    "razon_social_asumida",
]


def _es_salud_por_ramo(descripcion_ramo, codigo_entidad_interno) -> bool:
    """True si DescripcionRamo o CodigoEntidad/CodigoInterno confirman
    Salud — ver docstring del módulo. Nunca infiere Vida por descarte:
    si no hay señal de Salud, se queda como desconocido (False)."""
    if isinstance(descripcion_ramo, str) and any(
        t in descripcion_ramo.lower() for t in _TERMINOS_RAMO_SALUD
    ):
        return True
    if (
        isinstance(codigo_entidad_interno, str)
        and codigo_entidad_interno.strip().lower() in _CODIGOS_ENTIDAD_SALUD_CONFIRMADOS
    ):
        return True
    return False


@dataclass
class ResultadoIntegracionEiac:
    facturacion_eiac: pd.DataFrame
    polizas_provisionales: pd.DataFrame
    no_reconocidos: list[NumeroPolizaExtraido] = field(default_factory=list)


def _a_fecha(valor):
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    if hasattr(valor, "date"):
        return valor.date() if pd.notna(valor) else None
    return valor


def _extraer_y_registrar(id_poliza_eiac: str, no_reconocidos: list[NumeroPolizaExtraido]) -> str | None:
    resultado = extraer_numero_poliza_asisa(id_poliza_eiac)
    if not resultado.reconocido:
        no_reconocidos.append(resultado)
        return None
    return resultado.numero_poliza


def _periodo_liquidacion_ciclo_16_15(fecha) -> str:
    """Reconstruye el periodo_liquidacion real de ASISA (ciclo 16→15) a
    partir de una fecha de efecto — ver docstring del módulo para la
    justificación y el caso real que la confirma."""
    mes_calendario = f"{fecha.year:04d}-{fecha.month:02d}"
    if fecha.day >= _DIA_CORTE_CICLO:
        return siguiente_periodo(mes_calendario)
    return mes_calendario


def construir_facturacion_desde_eiac(
    df_eiac_recibos: pd.DataFrame,
    df_facturacion_existente: pd.DataFrame,
) -> tuple[pd.DataFrame, list[NumeroPolizaExtraido]]:
    """Traduce `eiac_recibos` a la misma forma que Facturación.

    Se incluyen recibos "CO" y "PE" por igual: Facturación de ASISA
    tampoco distingue cobro (eso es cosa de Liquidación), así que se
    mantiene el mismo criterio — un recibo "PE" que nunca llegue a
    cobrarse se corregirá cuando llegue la Liquidación real, igual que
    pasaría con cualquier recibo facturado y luego impagado.

    `df_facturacion_existente` es el CSV oficial: cualquier póliza que YA
    tenga alguna fila ahí se EXCLUYE por completo de aquí, aunque EIAC
    reporte una fecha de efecto anterior — el CSV oficial gana siempre,
    tanto el periodo como el importe (ver docstring del módulo, caso real
    de las 6 pólizas de marzo).
    """
    no_reconocidos: list[NumeroPolizaExtraido] = []
    if df_eiac_recibos.empty:
        return pd.DataFrame(columns=COLUMNAS_FACTURACION_EIAC), no_reconocidos

    polizas_csv_oficial = (
        set(df_facturacion_existente["poliza"]) if not df_facturacion_existente.empty else set()
    )

    filas = []
    for _, r in df_eiac_recibos.iterrows():
        numero_poliza = _extraer_y_registrar(r["id_poliza"], no_reconocidos)
        if numero_poliza is None:
            continue
        if numero_poliza in polizas_csv_oficial:
            continue  # el CSV oficial ya tiene esta póliza: nunca sobrescribir ni adelantarse
        fecha_efecto = _a_fecha(r["fecha_efecto_inicial"])
        if fecha_efecto is None:
            continue

        prima_neta = r["prima_neta"] if pd.notna(r["prima_neta"]) else r["prima_total"]
        filas.append(
            {
                "poliza": numero_poliza,
                "cliente_codigo": None,
                "cartera": "EIAC",
                "producto_nombre": None,
                # pd.Timestamp, no `date`: df_facturacion (leído con
                # pd.read_sql(parse_dates=...)) usa Timestamp en
                # fecha_desde/fecha_hasta -- mezclar date y Timestamp en la
                # misma columna rompe sort_values al concatenar ambos
                # DataFrames (TypeError: 'values' is not ordered).
                "fecha_desde": pd.Timestamp(fecha_efecto),
                "fecha_hasta": None,
                "prima_neta": prima_neta,
                "prima_total": r["prima_total"],
                "periodo_liquidacion": _periodo_liquidacion_ciclo_16_15(fecha_efecto),
                "duracion_recibo_meses": None,
            }
        )
    return pd.DataFrame(filas, columns=COLUMNAS_FACTURACION_EIAC), no_reconocidos


def construir_polizas_provisionales_desde_eiac(
    df_eiac_polizas: pd.DataFrame,
    df_eiac_recibos: pd.DataFrame,
    df_polizas_existente: pd.DataFrame,
) -> tuple[pd.DataFrame, list[NumeroPolizaExtraido]]:
    """Genera filas PROVISIONALES de `polizas` a partir de `eiac_polizas`,
    solo para pólizas que todavía no existen en la tabla OFICIAL (origen=
    'ASISA_CSV'). Una póliza que ya es provisional (origen='EIAC') SÍ se
    vuelve a generar aquí — así una mejora en los datos de EIAC (p.ej. el
    ramo/entidad que antes no traía) puede refrescar una fila provisional
    ya creada; `db.carga.cargar_polizas_provisionales_eiac` es quien
    garantiza con su propio UPDATE... WHERE que esto nunca toque una fila
    ya oficial, así que aquí basta con distinguir por `origen` si la
    columna está disponible.
    """
    no_reconocidos: list[NumeroPolizaExtraido] = []
    if df_eiac_polizas.empty:
        return pd.DataFrame(columns=COLUMNAS_POLIZAS_PROVISIONALES), no_reconocidos

    if df_polizas_existente.empty:
        polizas_existentes: set[str] = set()
    elif "origen" in df_polizas_existente.columns:
        polizas_existentes = set(
            df_polizas_existente.loc[df_polizas_existente["origen"] == "ASISA_CSV", "poliza"]
        )
    else:
        # Sin columna 'origen' (p.ej. un SELECT poliza a secas) no se puede
        # distinguir oficial de provisional -- se trata todo como oficial,
        # el comportamiento conservador de antes de este cambio.
        polizas_existentes = set(df_polizas_existente["poliza"])

    # pista_forma_pago por póliza: la primera pista no nula vista en sus recibos.
    pista_por_poliza: dict[str, str] = {}
    if not df_eiac_recibos.empty:
        for _, r in df_eiac_recibos.iterrows():
            numero_poliza = _extraer_y_registrar(r["id_poliza"], no_reconocidos)
            if numero_poliza is None:
                continue
            pista = r.get("pista_forma_pago")
            if numero_poliza not in pista_por_poliza and isinstance(pista, str) and pista:
                pista_por_poliza[numero_poliza] = pista

    filas = []
    for _, p in df_eiac_polizas.iterrows():
        numero_poliza = _extraer_y_registrar(p["id_poliza"], no_reconocidos)
        if numero_poliza is None:
            continue
        if numero_poliza in polizas_existentes:
            continue  # ya confirmada por el CSV oficial: no crear provisional

        situacion_eiac = p.get("situacion_poliza")
        situacion = _SITUACION_EIAC_A_ASISA.get(situacion_eiac, situacion_eiac)

        pista = pista_por_poliza.get(numero_poliza)
        forma_pago = _PISTA_A_FORMA_PAGO.get(pista)

        es_salud = _es_salud_por_ramo(p.get("descripcion_ramo"), p.get("codigo_entidad_interno"))
        if es_salud:
            razon_social = RAZON_SOCIAL_SALUD_POR_DEFECTO
            razon_social_asumida = True
            nota = (
                f"Origen: EIAC (id_poliza={p['id_poliza']}), pendiente de confirmar "
                "con Pólizas oficial. Producto exacto no confirmado, % asumido por "
                f"defecto ({RAZON_SOCIAL_SALUD_POR_DEFECTO}) — EIAC confirma Salud "
                "por DescripcionRamo/CodigoEntidad, pero no el producto concreto "
                "(Particulares/Red Sanitaria/etc.)."
            )
        else:
            razon_social = None
            razon_social_asumida = False
            nota = (
                f"Origen: EIAC (id_poliza={p['id_poliza']}), pendiente de confirmar "
                "con Pólizas oficial. razon_social desconocida — ni DescripcionRamo "
                "ni CodigoEntidad confirman Salud para esta póliza, así que no se "
                "asume ningún % de comisión sin revisar manualmente."
            )
        if forma_pago:
            nota += f" forma_pago provisional inferida de pista_forma_pago='{pista}'."

        filas.append(
            {
                "poliza": numero_poliza,
                "cliente_codigo": p.get("cliente_codigo"),
                "razon_social": razon_social,
                "producto_base": None,
                "producto_codigo": None,
                "fecha_emision": _a_fecha(p.get("fecha_emision")),
                "fecha_efecto": _a_fecha(p.get("fecha_efecto_inicial")),
                "fecha_baja": None,
                "forma_pago": forma_pago,
                "situacion": situacion,
                "provincia_tomador": None,
                "delegacion": None,
                "nombre_tomador": p.get("descripcion_riesgo"),
                "origen": "EIAC",
                "nota_origen": nota,
                "razon_social_asumida": razon_social_asumida,
            }
        )
    return pd.DataFrame(filas, columns=COLUMNAS_POLIZAS_PROVISIONALES), no_reconocidos


def integrar_eiac(
    df_eiac_polizas: pd.DataFrame,
    df_eiac_recibos: pd.DataFrame,
    df_polizas_existente: pd.DataFrame,
    df_facturacion_existente: pd.DataFrame,
) -> ResultadoIntegracionEiac:
    """Punto de entrada único: combina la traducción de Recibos y la
    generación de Pólizas provisionales, con la lista de `IdPoliza` que no
    se pudieron reconocer (formato inesperado) de ambas fuentes.

    `df_facturacion_existente` (el CSV oficial) tiene prioridad absoluta:
    ver docstring del módulo y de `construir_facturacion_desde_eiac`.
    """
    facturacion_eiac, no_reconocidos_recibos = construir_facturacion_desde_eiac(
        df_eiac_recibos, df_facturacion_existente
    )
    polizas_provisionales, no_reconocidos_polizas = construir_polizas_provisionales_desde_eiac(
        df_eiac_polizas, df_eiac_recibos, df_polizas_existente
    )

    vistos: set[str] = set()
    no_reconocidos: list[NumeroPolizaExtraido] = []
    for item in no_reconocidos_recibos + no_reconocidos_polizas:
        if item.id_poliza_eiac in vistos:
            continue
        vistos.add(item.id_poliza_eiac)
        no_reconocidos.append(item)

    return ResultadoIntegracionEiac(
        facturacion_eiac=facturacion_eiac,
        polizas_provisionales=polizas_provisionales,
        no_reconocidos=no_reconocidos,
    )
