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

DETECCIÓN DE TRAVEL — caso real (póliza 64171931, ASISA Travel and You):
`DatosRamo` trae un tercer campo, `RamoEntidad`, que distingue Travel de
Salud normal de forma fiable (`RAVI` vs `RASA`, ver docstring de
`ingestion.eiac_xml`). Como "ASISA TRAVEL AND YOU" es el único producto
de viaje en `config/contrato.yaml` (20%/0%, muy distinto del 25%/20% de
Particulares), esta señal se comprueba ANTES que `_es_salud_por_ramo`
(que también detectaría esta póliza como "Salud" por su
`CodigoEntidad/CodigoInterno="Asisa"` compartido, y asumiría el % de
Particulares equivocado) — ver `_es_travel_por_ramo`.

DETECCIÓN DE VIDA — caso real (póliza 22594-64358396, Elias David
Gonzalez Pacheco, entidad ASISA VIDA, efecto 2026-08-05, prima_neta
~38,26€/mes): `RamoEntidad="VIDA"` (o `DescripcionRamo="Vida"`) es señal
confirmada de Vida, igual de fiable que "RAVI" para Travel. A diferencia
de Travel, `config/contrato.yaml` tiene varios productos de Vida con %
muy distintos (Tranquilidad 60/20, Tranquilidad Hipoteca 40/45,
Accidentes Senior 35/35), así que aquí NO se conoce el producto exacto —
se asume "ASISA VIDA TRANQUILIDAD" (el más habitual en la cartera) igual
que se hace con Salud/Particulares, marcado con `razon_social_asumida=
True` para bajar la confianza de la estimación — ver
`_es_vida_por_ramo`.

ANULACIÓN POR EIAC (`ClasePoliza=AN`/`SituacionPoliza=EX`) — investigado
en julio 2026 a raíz del mismo caso real: la póliza 64171931 fue anulada
(fichero POLI posterior, `FechaAnulacion` real) DESPUÉS de su alta
original. Se confirmó que `situacion` SÍ se actualiza correctamente en la
póliza provisional (el upsert de `db.carga.cargar_polizas_provisionales_eiac`
refresca la fila mientras siga siendo `origen='EIAC'`), pero el mapeo
`_SITUACION_EIAC_A_ASISA` no incluía "EX" — se añade aquí ("EX" -> "B",
igual que "BJ"). El problema más importante era otro: NADA en la cadena
de producción/comisión/rappel filtraba por `situacion` — se corrigió en
`engine.insights.polizas_activas` y `engine.calibracion.
estimar_comision_y_rappel_periodo`/`engine.objetivo._desglose_mes`. Para
esta póliza en concreto el bug nunca llegó a manifestarse en pantalla
(no tiene ningún recibo en los ficheros RECI reales, así que nunca generó
producción ni con ni sin el fix) — pero el mecanismo general sí estaba
roto para cualquier póliza que SÍ tuviera recibo y se anulase después.
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

# SituacionPoliza (EIAC) -> situacion (ASISA). "EV" (en vigor) -> "A" y
# "EX" (extinguida) -> "B" están confirmados con datos reales (caso
# 64171931: anulación con FechaAnulacion real). "BJ" -> "B" sigue siendo
# una suposición razonable sin confirmar todavía; si el código real no
# está aquí, se deja el código EIAC tal cual en vez de forzar un valor.
_SITUACION_EIAC_A_ASISA = {
    "EV": "A",
    "BJ": "B",
    "EX": "B",
}

# Señales de Salud y Vida confirmadas con datos reales (ver docstring del
# módulo) — ambas solo se detectan de forma afirmativa, nunca por
# descarte.
_TERMINOS_RAMO_SALUD = ("sanitaria", "salud")
_CODIGOS_ENTIDAD_SALUD_CONFIRMADOS = {"asisa"}

# RamoEntidad "RAVI" -- señal confirmada de Travel (caso real 64171931).
# Ver docstring del módulo: se comprueba ANTES que _es_salud_por_ramo.
_RAMO_ENTIDAD_TRAVEL_CONFIRMADOS = {"ravi"}

# RamoEntidad "VIDA" -- señal confirmada de Vida (caso real 22594-64358396,
# ASISA VIDA, efecto 2026-08-05). Como con Travel, se comprueba ANTES que
# _es_salud_por_ramo (aunque "vida" no coincide con ningún término de
# _TERMINOS_RAMO_SALUD, por claridad se agrupa aquí con las demás señales
# afirmativas por ramo).
_RAMO_ENTIDAD_VIDA_CONFIRMADOS = {"vida"}
_TERMINOS_DESCRIPCION_RAMO_VIDA = ("vida",)

# razon_social por defecto cuando se confirma Salud pero no el producto
# exacto — el más habitual en la cartera; ver docstring del módulo.
RAZON_SOCIAL_SALUD_POR_DEFECTO = "ASISA PARTICULARES"

# razon_social cuando RamoEntidad confirma Travel — único producto de
# viaje en config/contrato.yaml, así que aquí SÍ se sabe el producto
# exacto (no solo la categoría), a diferencia del default de Salud de
# arriba.
RAZON_SOCIAL_TRAVEL = "ASISA TRAVEL AND YOU"

# razon_social por defecto cuando se confirma Vida pero no el producto
# exacto — "ASISA VIDA TRANQUILIDAD" (60%/20%) es el más habitual en la
# cartera de Vida; igual de incierto que el default de Salud de arriba
# (hay otros productos de Vida en config/contrato.yaml con % muy
# distintos: TRANQUILIDAD HIPOTECA 40/45, ACCIDENTES SENIOR 35/35).
RAZON_SOCIAL_VIDA_POR_DEFECTO = "ASISA VIDA TRANQUILIDAD"

COLUMNAS_FACTURACION_EIAC = [
    "poliza", "cliente_codigo", "cartera", "producto_nombre",
    "fecha_desde", "fecha_hasta", "prima_neta", "prima_total",
    "periodo_liquidacion", "duracion_recibo_meses", "nota_origen",
]

NOTA_EIAC_POLI_PROVISIONAL = "EIAC_POLI_provisional — pendiente de recibo real"

COLUMNAS_POLIZAS_PROVISIONALES = [
    "poliza", "cliente_codigo", "razon_social", "producto_base", "producto_codigo",
    "fecha_emision", "fecha_efecto", "fecha_baja", "forma_pago", "situacion",
    "provincia_tomador", "delegacion", "nombre_tomador", "origen", "nota_origen",
    "razon_social_asumida",
]


def _es_travel_por_ramo(ramo_entidad) -> bool:
    """True si RamoEntidad confirma Travel ("RAVI") — ver docstring del
    módulo y caso real 64171931."""
    return (
        isinstance(ramo_entidad, str)
        and ramo_entidad.strip().lower() in _RAMO_ENTIDAD_TRAVEL_CONFIRMADOS
    )


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


def _es_vida_por_ramo(ramo_entidad, descripcion_ramo) -> bool:
    """True si RamoEntidad="VIDA" o DescripcionRamo="Vida" — señal
    confirmada de Vida (caso real 22594-64358396, ver docstring del
    módulo). Se comprueba antes que _es_salud_por_ramo."""
    if (
        isinstance(ramo_entidad, str)
        and ramo_entidad.strip().lower() in _RAMO_ENTIDAD_VIDA_CONFIRMADOS
    ):
        return True
    if isinstance(descripcion_ramo, str) and any(
        t in descripcion_ramo.lower() for t in _TERMINOS_DESCRIPCION_RAMO_VIDA
    ):
        return True
    return False


def _clasificar_producto_por_ramo(
    ramo_entidad, descripcion_ramo, codigo_entidad_interno
) -> tuple[str | None, bool, str]:
    """Clasifica Salud/Vida/Travel por las señales de ramo/entidad -- misma
    lógica tanto si vienen de `<Poliza>` como de `<Recibo><DatosPoliza>`
    (idéntica estructura, ver docstring del módulo). Devuelve
    (razon_social, razon_social_asumida, fragmento_de_nota)."""
    if _es_travel_por_ramo(ramo_entidad):
        return RAZON_SOCIAL_TRAVEL, True, (
            f"Producto detectado como {RAZON_SOCIAL_TRAVEL} por RamoEntidad='{ramo_entidad}' "
            "— único producto de viaje en el contrato, señal inequívoca (no un default "
            "estadístico como el de Salud)."
        )
    if _es_vida_por_ramo(ramo_entidad, descripcion_ramo):
        return RAZON_SOCIAL_VIDA_POR_DEFECTO, True, (
            "Producto exacto no confirmado, % asumido por defecto "
            f"({RAZON_SOCIAL_VIDA_POR_DEFECTO}) — EIAC confirma Vida por "
            f"RamoEntidad='{ramo_entidad}'/DescripcionRamo, pero no el producto concreto "
            "(Tranquilidad/Tranquilidad Hipoteca/Accidentes Senior/etc.)."
        )
    if _es_salud_por_ramo(descripcion_ramo, codigo_entidad_interno):
        return RAZON_SOCIAL_SALUD_POR_DEFECTO, True, (
            "Producto exacto no confirmado, % asumido por defecto "
            f"({RAZON_SOCIAL_SALUD_POR_DEFECTO}) — EIAC confirma Salud por "
            "DescripcionRamo/CodigoEntidad, pero no el producto concreto (Particulares/"
            "Red Sanitaria/etc.)."
        )
    return None, False, (
        "razon_social desconocida — ni DescripcionRamo ni CodigoEntidad confirman Salud "
        "para esta póliza, así que no se asume ningún % de comisión sin revisar manualmente."
    )


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


def _a_timestamp(valor) -> pd.Timestamp | None:
    """Como `_a_fecha`, pero devuelve `pd.Timestamp` en vez de `date` --
    necesario en columnas que se concatenan con `df_polizas` (leído con
    `pd.read_sql(parse_dates=...)`, que usa `pd.Timestamp`): mezclar
    `date` y `Timestamp` en la misma columna "object" la deja sin tipo
    homogéneo y rompe la serialización a Arrow de `st.dataframe` (bug
    real: pestaña Pólizas, columna `fecha_emision`, ago-2026)."""
    fecha = _a_fecha(valor)
    return pd.Timestamp(fecha) if fecha is not None else None


def _extraer_y_registrar(id_poliza_eiac: str, no_reconocidos: list[NumeroPolizaExtraido]) -> str | None:
    resultado = extraer_numero_poliza_asisa(id_poliza_eiac)
    if not resultado.reconocido:
        no_reconocidos.append(resultado)
        return None
    return resultado.numero_poliza


def _periodo_liquidacion_ciclo_16_15(fecha) -> str:
    """Reconstruye el periodo_liquidacion real de ASISA (ciclo 16→15) a
    partir de una fecha — ver docstring del módulo para la justificación y
    el caso real que la confirma."""
    mes_calendario = f"{fecha.year:04d}-{fecha.month:02d}"
    if fecha.day >= _DIA_CORTE_CICLO:
        return siguiente_periodo(mes_calendario)
    return mes_calendario


def _periodo_liquidacion_recibo(fecha_efecto, fecha_emision) -> str:
    """Regla de negocio confirmada por Sebastián (caso real 24848-64659995,
    FechaEfectoInicial=2026-08-12 / FechaEmision=2026-08-19): para que un
    recibo cuente en el periodo X hacen falta DOS condiciones simultáneas,
    fecha_efecto Y fecha_emision dentro del mismo ciclo 16→15 de X. Si la
    emisión cae en un ciclo posterior al de la fecha de efecto (recibo
    generado después del corte del 15), el recibo entra en ese periodo
    posterior aunque la fecha de efecto sea anterior -- nunca al revés
    (una emisión más temprana no adelanta un efecto tardío, ese caso ya lo
    cubre `_periodo_liquidacion_ciclo_16_15` con la fecha de efecto sola).
    Por eso el periodo final es el MÁXIMO (más tardío) entre ambos --
    comparación de string funciona porque el formato "YYYY-MM" ordena
    igual que la fecha real.

    `fecha_emision` es `None` para cualquier recibo cargado antes de esta
    regla (columna nueva, sin backfill todavía -- ver
    `engine.eiac_integracion` y la conversación que añadió esta función):
    en ese caso se usa solo `fecha_efecto`, exactamente el comportamiento
    de antes de esta regla."""
    periodo = _periodo_liquidacion_ciclo_16_15(fecha_efecto)
    if fecha_emision is not None:
        periodo_emision = _periodo_liquidacion_ciclo_16_15(fecha_emision)
        if periodo_emision > periodo:
            periodo = periodo_emision
    return periodo


def construir_facturacion_desde_eiac(
    df_eiac_polizas: pd.DataFrame,
    df_eiac_recibos: pd.DataFrame,
    df_facturacion_existente: pd.DataFrame,
) -> tuple[pd.DataFrame, list[NumeroPolizaExtraido]]:
    """Traduce `eiac_recibos` a la misma forma que Facturación, y COMPLETA
    con producción provisional desde `eiac_polizas` para pólizas que
    todavía no tienen recibo EIAC.

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

    SEGUNDO BLOQUE (ago-2026, caso real 64529498/64527386/64171805): la
    producción se devenga desde la fecha de efecto de la póliza, no desde
    que llega el recibo — ASISA suele enviar primero el EIAC-ENV-POLI (con
    la prima ya confirmada, `eiac_polizas.prima_neta_poliza`) y el recibo
    llega días o semanas después. Sin este bloque, esas pólizas eran
    invisibles para producción/rappel mientras tanto. Una póliza entra
    aquí SOLO si: (a) tiene `prima_neta_poliza` (si el XML no lo trajo, no
    se inventa), (b) NO tiene ya una fila en el CSV oficial (misma
    prioridad de arriba), y (c) NO tiene ya ningún recibo EIAC — en cuanto
    llegue el recibo real (aunque sea "PE"), la prioridad ya existente
    recibo > póliza (ver primer bucle) hace que esta fila provisional deje
    de generarse automáticamente, sin lógica adicional.
    """
    no_reconocidos: list[NumeroPolizaExtraido] = []
    polizas_csv_oficial = (
        set(df_facturacion_existente["poliza"]) if not df_facturacion_existente.empty else set()
    )

    filas = []
    polizas_con_recibo: set[str] = set()
    if not df_eiac_recibos.empty:
        for _, r in df_eiac_recibos.iterrows():
            numero_poliza = _extraer_y_registrar(r["id_poliza"], no_reconocidos)
            if numero_poliza is None:
                continue
            polizas_con_recibo.add(numero_poliza)
            if numero_poliza in polizas_csv_oficial:
                continue  # el CSV oficial ya tiene esta póliza: nunca sobrescribir ni adelantarse
            fecha_efecto = _a_fecha(r["fecha_efecto_inicial"])
            if fecha_efecto is None:
                continue
            fecha_emision = _a_fecha(r.get("fecha_emision"))

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
                    "periodo_liquidacion": _periodo_liquidacion_recibo(fecha_efecto, fecha_emision),
                    "duracion_recibo_meses": None,
                    "nota_origen": None,
                }
            )

    if not df_eiac_polizas.empty:
        for _, p in df_eiac_polizas.iterrows():
            numero_poliza = _extraer_y_registrar(p["id_poliza"], no_reconocidos)
            if numero_poliza is None:
                continue
            if numero_poliza in polizas_csv_oficial:
                continue  # el CSV oficial ya tiene esta póliza: nunca sobrescribir ni adelantarse
            if numero_poliza in polizas_con_recibo:
                continue  # ya hay recibo (real, aunque sea "PE"): el recibo gana siempre
            prima_neta_poliza = p.get("prima_neta_poliza")
            if prima_neta_poliza is None or pd.isna(prima_neta_poliza):
                continue  # el XML de póliza no traía prima -- no se inventa
            fecha_efecto = _a_fecha(p.get("fecha_efecto_inicial"))
            if fecha_efecto is None:
                continue

            filas.append(
                {
                    "poliza": numero_poliza,
                    "cliente_codigo": p.get("cliente_codigo"),
                    "cartera": "EIAC_POLI",
                    "producto_nombre": None,
                    "fecha_desde": pd.Timestamp(fecha_efecto),
                    "fecha_hasta": None,
                    "prima_neta": float(prima_neta_poliza),
                    "prima_total": None,
                    "periodo_liquidacion": _periodo_liquidacion_ciclo_16_15(fecha_efecto),
                    "duracion_recibo_meses": None,
                    "nota_origen": NOTA_EIAC_POLI_PROVISIONAL,
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

    SEGUNDO BLOQUE (ago-2026, caso real 64572908): si un recibo EIAC llega
    SIN que exista ningún `<Poliza>` correspondiente todavía (el caso
    inverso del que ya cubre el primer bloque), la póliza es igualmente
    invisible para producción/rappel -- el `INNER JOIN` con Pólizas
    descarta la fila sin ningún dato con el que clasificarla. El propio
    `<Recibo>` trae su bloque `<DatosPoliza>` con la MISMA estructura de
    ramo/entidad/forma de pago que `<Poliza>` (confirmado con datos
    reales), así que basta con reutilizar la misma clasificación por ramo.
    `situacion` se asume "A" (activa) sin poder confirmarlo -- un recibo
    COBRADO es la evidencia más fuerte posible de que la póliza existe y
    está viva, pero se marca explícitamente como sin confirmar en la nota.
    """
    no_reconocidos: list[NumeroPolizaExtraido] = []
    if df_eiac_polizas.empty and df_eiac_recibos.empty:
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

    # pista_forma_pago y señales de ramo/entidad por póliza: la primera no
    # nula vista en sus recibos. De paso, qué pólizas YA tienen algún
    # recibo EIAC -- para distinguir en la nota "producción confirmada por
    # recibo" de "producción provisional por fecha de efecto, todavía sin
    # recibo" (ver construir_facturacion_desde_eiac, mismo criterio de
    # prioridad) y para el segundo bloque (pólizas SOLO por recibo, sin
    # ningún <Poliza> -- caso real 64572908).
    pista_por_poliza: dict[str, str] = {}
    polizas_con_recibo: set[str] = set()
    ramo_recibo_por_poliza: dict[str, dict] = {}
    primer_recibo_por_poliza: dict[str, pd.Series] = {}
    if not df_eiac_recibos.empty:
        for _, r in df_eiac_recibos.iterrows():
            numero_poliza = _extraer_y_registrar(r["id_poliza"], no_reconocidos)
            if numero_poliza is None:
                continue
            polizas_con_recibo.add(numero_poliza)
            pista = r.get("pista_forma_pago")
            if numero_poliza not in pista_por_poliza and isinstance(pista, str) and pista:
                pista_por_poliza[numero_poliza] = pista
            if numero_poliza not in ramo_recibo_por_poliza and any(
                isinstance(r.get(campo), str) and r.get(campo)
                for campo in ("ramo_entidad", "descripcion_ramo", "codigo_entidad_interno")
            ):
                ramo_recibo_por_poliza[numero_poliza] = {
                    "ramo_entidad": r.get("ramo_entidad"),
                    "descripcion_ramo": r.get("descripcion_ramo"),
                    "codigo_entidad_interno": r.get("codigo_entidad_interno"),
                }
            primer_recibo_por_poliza.setdefault(numero_poliza, r)

    filas = []
    polizas_con_eiac_poliza: set[str] = set()
    for _, p in df_eiac_polizas.iterrows():
        numero_poliza = _extraer_y_registrar(p["id_poliza"], no_reconocidos)
        if numero_poliza is None:
            continue
        polizas_con_eiac_poliza.add(numero_poliza)
        if numero_poliza in polizas_existentes:
            continue  # ya confirmada por el CSV oficial: no crear provisional

        situacion_eiac = p.get("situacion_poliza")
        situacion = _SITUACION_EIAC_A_ASISA.get(situacion_eiac, situacion_eiac)

        pista = pista_por_poliza.get(numero_poliza)
        forma_pago = _PISTA_A_FORMA_PAGO.get(pista)

        # prima_neta_poliza (DatosImportes/Importes/PrimaNeta a nivel
        # <Poliza>) es SIEMPRE la prima ANUALIZADA de la póliza completa,
        # tenga el ciclo de facturación que tenga -- verificado con datos
        # reales: pólizas Vida mensuales confirmadas (64110228/64110254/
        # 64101698) tienen prima_neta_poliza = prima_neta mensual × 12
        # exacto. Sin recibo (pista=None) no hay forma_pago real conocida,
        # pero forzar "A" aquí es lo correcto para que
        # engine.insights.resumen_produccion_periodo/engine.calibracion NO
        # multipliquen por 12 una cifra que ya es anual (bug real: sin
        # esto, producción de agosto se inflaba x12 -- 595,80€ pasaban a
        # contar como 7.149,60€).
        sin_recibo_con_prima = (
            numero_poliza not in polizas_con_recibo
            and p.get("prima_neta_poliza") is not None
            and not pd.isna(p.get("prima_neta_poliza"))
        )
        if forma_pago is None and sin_recibo_con_prima:
            forma_pago = "A"

        razon_social, razon_social_asumida, fragmento_nota = _clasificar_producto_por_ramo(
            p.get("ramo_entidad"), p.get("descripcion_ramo"), p.get("codigo_entidad_interno")
        )
        nota = f"Origen: EIAC (id_poliza={p['id_poliza']}), pendiente de confirmar con Pólizas oficial. {fragmento_nota}"
        if pista and forma_pago:
            nota += f" forma_pago provisional inferida de pista_forma_pago='{pista}'."

        prima_neta_poliza = p.get("prima_neta_poliza")
        if sin_recibo_con_prima:
            nota += (
                f" ⚠️ PRODUCCIÓN PROVISIONAL: {prima_neta_poliza:,.2f}€ (ya anualizada) "
                "por fecha de efecto de la póliza (EIAC-ENV-POLI) — todavía sin recibo "
                "EIAC que confirme el cobro. forma_pago='A' asumido para que se cuente "
                "tal cual, sin duplicar x12 (ver docstring). En cuanto llegue el recibo "
                "real, sustituye automáticamente a esta cifra."
            )

        filas.append(
            {
                "poliza": numero_poliza,
                "cliente_codigo": p.get("cliente_codigo"),
                "razon_social": razon_social,
                "producto_base": None,
                "producto_codigo": None,
                "fecha_emision": _a_timestamp(p.get("fecha_emision")),
                "fecha_efecto": _a_timestamp(p.get("fecha_efecto_inicial")),
                # Fecha real de anulación (DatosAnulacion/FechaAnulacion) si
                # la hay -- necesaria para que el filtro de anulación de
                # engine.calibracion pueda distinguir producción/comisión
                # ANTERIOR a la anulación (cuenta) de la POSTERIOR (no
                # cuenta), caso real: póliza 64171931.
                "fecha_baja": _a_timestamp(p.get("fecha_anulacion")),
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

    # SEGUNDO BLOQUE: pólizas que SOLO existen por recibo (sin ningún
    # <Poliza> todavía) -- ver docstring de la función, caso real 64572908.
    for numero_poliza, r in primer_recibo_por_poliza.items():
        if numero_poliza in polizas_existentes:
            continue  # ya confirmada por el CSV oficial
        if numero_poliza in polizas_con_eiac_poliza:
            continue  # ya generada arriba desde <Poliza> -- no duplicar

        ramo = ramo_recibo_por_poliza.get(numero_poliza, {})
        razon_social, razon_social_asumida, fragmento_nota = _clasificar_producto_por_ramo(
            ramo.get("ramo_entidad"), ramo.get("descripcion_ramo"), ramo.get("codigo_entidad_interno")
        )
        pista = pista_por_poliza.get(numero_poliza)
        forma_pago = _PISTA_A_FORMA_PAGO.get(pista)
        fecha_efecto = _a_timestamp(r.get("fecha_efecto_inicial"))

        nota = (
            f"Origen: EIAC (id_poliza={r['id_poliza']}), pendiente de confirmar con "
            f"Pólizas oficial. SIN ningún <Poliza> (EIAC-ENV-POLI) todavía — clasificada "
            f"a partir del propio recibo (EIAC-ENV-RECI). {fragmento_nota} situacion='A' "
            "asumida sin confirmar (un recibo cobrado es evidencia fuerte de póliza "
            "activa, pero no un dato directo de situación de póliza)."
        )
        if pista and forma_pago:
            nota += f" forma_pago provisional inferida de pista_forma_pago='{pista}'."

        filas.append(
            {
                "poliza": numero_poliza,
                "cliente_codigo": None,
                "razon_social": razon_social,
                "producto_base": None,
                "producto_codigo": None,
                "fecha_emision": None,
                "fecha_efecto": fecha_efecto,
                "fecha_baja": None,
                "forma_pago": forma_pago,
                "situacion": "A",
                "provincia_tomador": None,
                "delegacion": None,
                "nombre_tomador": None,
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
        df_eiac_polizas, df_eiac_recibos, df_facturacion_existente
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
