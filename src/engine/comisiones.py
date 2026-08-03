"""Motor de clasificación y estimación de comisiones por póliza.

Tres mecánicas distintas, todas validadas contra datos reales de Sebastián
(abril-julio 2026 + reconstrucción enero-marzo):

  1. VIDA (entidad ASISA VIDA): comisión = prima del recibo × % del Anexo I,
     cada recibo cobrado, sin rappel. Confianza: alta.

  2. SALUD ANUAL/PREPAGO (forma_pago='A'): un único recibo anual; comisión
     completa = prima anual × % primer año, en el mes de la fecha de EFECTO
     (columna "FECHA ALTA" del CSV de Pólizas). Confianza: alta.

  3. SALUD MENSUAL (forma_pago='M'): ASISA anticipa la comisión anualizada
     completa en el mes del primer recibo cobrado, y en los 11 meses
     siguientes aparecen movimientos de "regularización" cuyo patrón exacto
     no está cerrado al 100% (ver conversación / README). Para la ESTIMACIÓN
     seguimos el texto literal del contrato (anticipo en el primer recibo);
     los meses posteriores de la misma póliza no deberían generar comisión
     nueva, pero esto se marca siempre como estimación de confianza media,
     nunca como hecho, hasta reconciliar con la Liquidación real.

     Refinamiento (`refinar_confianza_salud_mensual`): no hemos encontrado
     una fórmula fiable para predecir el IMPORTE de esas regularizaciones,
     pero el propio historial real de Liquidación de cada póliza sí dice
     si esa póliza YA tuvo algún ajuste alguna vez (casos reales: 63938090,
     con un EXTORNO ANUALIZADA + 3 ANUALIZADA -> historial irregular; frente
     a 63920702, con un único ANUALIZADA -> sin ajustes conocidos). Eso basta
     para avisar de qué pólizas concretas son más inciertas, sin necesidad de
     acertar el importe.
"""

from __future__ import annotations

import calendar
import dataclasses
import unicodedata
from dataclasses import dataclass
from datetime import date

import pandas as pd

from engine.config_contrato import ContratoConfig

# Acciones del historial real de Liquidación que representan un ajuste de
# la comisión anualizada de una póliza de salud mensual (el anticipo
# original es "ANUALIZADA"; un ajuste posterior puede ser un nuevo evento
# "ANUALIZADA" o un "EXTORNO ANUALIZADA" que lo revierte).
ACCIONES_AJUSTE_ANUALIZADA = ("ANUALIZADA", "EXTORNO ANUALIZADA")

TIPO_VIDA = "vida"
TIPO_SALUD_ANUAL = "salud_anual"
TIPO_SALUD_MENSUAL = "salud_mensual"


@dataclass
class EstimacionComision:
    poliza: str
    tipo: str
    mes_devengo: str  # "AAAA-MM"
    comision_bruta_estimada: float
    confianza: str  # "alta" | "media" | "baja"
    nota: str = ""


@dataclass(frozen=True)
class EstadoAnualizacionSalud:
    """Estado histórico de un anticipo anualizado antes de un periodo."""

    poliza: str
    anualizacion_vigente: bool
    periodo_anualizacion: str | None = None
    importe_anualizacion: float | None = None
    motivo: str = ""


def _normalizar_accion_liquidacion(accion: object) -> str:
    """Normaliza espacios, mayúsculas y tildes de las acciones ASISA."""
    texto = unicodedata.normalize("NFKD", str(accion or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.upper().split())


def obtener_estado_anualizacion_salud(
    df_liquidacion: pd.DataFrame, poliza: str, periodo_estimado: str
) -> EstadoAnualizacionSalud:
    """Indica si una Salud mensual ya tenía un anticipo anual vigente.

    Solo se consideran movimientos anteriores al periodo estimado. Se ordenan
    por ``fecha_desde`` cuando existe y, si falta, por periodo de Liquidación
    normalizado (AAAA-MM), preservando el orden estable de origen como último
    desempate. Un ``EXTORNO ANUALIZADA`` solo reduce el saldo si su comisión
    es negativa; otros ajustes, incluido ``A DESCONTAR``, no lo cancelan.
    """
    if df_liquidacion.empty or "poliza" not in df_liquidacion.columns:
        return EstadoAnualizacionSalud(poliza, False)

    periodo_limite = _periodo_liquidacion_ordenable(periodo_estimado)
    movimientos = df_liquidacion[df_liquidacion["poliza"] == poliza].copy()
    if movimientos.empty or "periodo_liquidacion" not in movimientos.columns:
        return EstadoAnualizacionSalud(poliza, False)
    movimientos["_periodo"] = movimientos["periodo_liquidacion"].map(_periodo_liquidacion_ordenable)
    movimientos = movimientos[movimientos["_periodo"] < periodo_limite]
    if movimientos.empty:
        return EstadoAnualizacionSalud(poliza, False)

    movimientos["_fecha"] = pd.to_datetime(movimientos.get("fecha_desde"), errors="coerce")
    movimientos["_fecha_orden"] = movimientos["_fecha"].fillna(
        pd.to_datetime(movimientos["_periodo"] + "-01", errors="coerce")
    )
    movimientos["_orden_estable"] = range(len(movimientos))
    movimientos = movimientos.sort_values(["_fecha_orden", "_periodo", "_orden_estable"], na_position="last")

    saldo = 0.0
    ultima_positiva = None
    for _, movimiento in movimientos.iterrows():
        accion = _normalizar_accion_liquidacion(movimiento.get("accion"))
        importe = movimiento.get("comision")
        comision = 0.0 if importe is None or pd.isna(importe) else float(importe)
        es_extorno_anualizada = "EXTORNO" in accion and "ANUALIZADA" in accion
        es_anualizada_positiva = "ANUALIZADA" in accion and not es_extorno_anualizada and comision > 0
        if es_anualizada_positiva:
            saldo += comision
            ultima_positiva = movimiento
        elif es_extorno_anualizada and comision < 0:
            saldo = max(0.0, saldo + comision)

    if saldo <= 0.005 or ultima_positiva is None:
        return EstadoAnualizacionSalud(poliza, False)
    return EstadoAnualizacionSalud(
        poliza=poliza,
        anualizacion_vigente=True,
        periodo_anualizacion=ultima_positiva["periodo_liquidacion"],
        importe_anualizacion=float(ultima_positiva["comision"]),
        motivo="anualización previa vigente",
    )


def clasificar_poliza(fila_poliza: pd.Series, contrato: ContratoConfig) -> str:
    """Determina la mecánica de comisión de una póliza (fila del maestro Pólizas)."""
    if fila_poliza["razon_social"] in contrato.comisiones_vida:
        return TIPO_VIDA
    if fila_poliza["forma_pago"] == "A":
        return TIPO_SALUD_ANUAL
    return TIPO_SALUD_MENSUAL


def _pct_comision_salud(contrato: ContratoConfig, razon_social: str, es_primer_anio: bool) -> float:
    # NOTA IMPORTANTE (sin resolver): el campo `.mantenimiento` de cada
    # bloque de comisiones_salud en config/contrato.yaml no se lee en NINGÚN
    # sitio del código — aquí siempre se devuelve `.produccion`, tanto para
    # `primer_anio` como para `segundo_anio_en_adelante`.
    #
    # La idea original al diseñar el proyecto fue "año 2 en adelante = %
    # de mantenimiento", pero eso NUNCA se ha contrastado contra una
    # Liquidación real — a diferencia del resto de reglas de este motor,
    # que sí están validadas con datos reales del agente (ver docstring del
    # módulo). Existe una lectura alternativa igual de plausible: que el
    # contrato distinga "nuevas altas" (% de producción) de "cambios de
    # mediador en pólizas ya existentes" (% de mantenimiento), es decir,
    # que "mantenimiento" no dependa de la antigüedad de la póliza sino de
    # si es un traspaso de otro mediador — algo que este motor no modela
    # todavía. No tocar esta función hasta tener un dato real de Liquidación
    # de una póliza de Salud con más de 12 meses que lo confirme o lo
    # descarte. Mientras tanto, ver `es_primer_anio` en `estimar_comision_poliza`:
    # cuando es False, la confianza de la estimación baja a "media" para
    # dejar claro que ese % no está confirmado.
    tabla = contrato.comisiones_salud.get(razon_social)
    if tabla is None:
        return 0.0
    bloque = tabla.primer_anio if es_primer_anio else tabla.segundo_anio_en_adelante
    return bloque.produccion


def meses_transcurridos(fecha_efecto: date, fecha_referencia: date) -> int:
    """Nº de meses completos transcurridos entre fecha_efecto y fecha_referencia.

    Pública a propósito: es el único criterio de "¿cuándo cumple 12 meses
    una póliza?" del proyecto. Cualquier otro módulo que necesite saber si
    una póliza ya está en año 2+ (p.ej. las alertas de aniversario de
    `engine.insights`) debe reutilizar esta función o `fecha_cambio_a_mantenimiento`
    en vez de reinventar la aritmética de fechas — dos implementaciones
    distintas del mismo concepto pueden divergir en años bisiestos o fechas
    de efecto a fin de mes.
    """
    meses = (fecha_referencia.year - fecha_efecto.year) * 12 + (
        fecha_referencia.month - fecha_efecto.month
    )
    if fecha_referencia.day < fecha_efecto.day:
        meses -= 1
    return max(meses, 0)


def fecha_cambio_a_mantenimiento(fecha_efecto: date) -> date:
    """Fecha del primer aniversario usada como punto de revisión.

    Coincide exactamente con el criterio de `meses_transcurridos` (mismo
    mes/día un año después), salvo cuando ese día no existe en el mes
    objetivo (p.ej. una póliza con efecto el 31 de un mes, o el 29 de
    febrero de un año bisiesto) — en ese caso el criterio de
    `meses_transcurridos` no se cumple hasta el día 1 del mes siguiente,
    y esta función refleja lo mismo para no divergir del motor de comisiones.
    """
    anio_objetivo = fecha_efecto.year + 1
    mes_objetivo = fecha_efecto.month
    ultimo_dia_mes_objetivo = calendar.monthrange(anio_objetivo, mes_objetivo)[1]
    if fecha_efecto.day <= ultimo_dia_mes_objetivo:
        return date(anio_objetivo, mes_objetivo, fecha_efecto.day)
    if mes_objetivo == 12:
        return date(anio_objetivo + 1, 1, 1)
    return date(anio_objetivo, mes_objetivo + 1, 1)


def _pct_comision_vida(contrato: ContratoConfig, razon_social: str, meses_transcurridos: int) -> float:
    datos = contrato.comisiones_vida.get(razon_social, {})
    if "produccion" in datos:
        # Igual que Salud: a partir del segundo año (12 meses) aplica
        # el % de Mantenimiento, no el de Producción.
        if meses_transcurridos < 12:
            return datos["produccion"]
        return datos.get("mantenimiento", datos["produccion"])
    # Productos con escalado por año (p.ej. AV Accidentes Compromiso 10).
    anio_index = meses_transcurridos // 12
    if anio_index <= 0:
        return datos.get("primer_anio", 0.0)
    if anio_index == 1:
        return datos.get("segundo_anio", datos.get("tercer_anio_en_adelante", 0.0))
    return datos.get("tercer_anio_en_adelante", datos.get("segundo_anio", 0.0))


def estimar_comision_poliza(
    fila_poliza: pd.Series,
    contrato: ContratoConfig,
    prima_anual: float,
    prima_recibo_mensual: float | None = None,
    fecha_referencia: date | None = None,
) -> EstimacionComision:
    """Estima la comisión de una póliza dado su tipo, sin necesidad de Liquidación.

    `fecha_referencia` (por defecto hoy) se usa para determinar si la póliza
    lleva 12 meses o más activa. Para Salud, el porcentaje aplicado en año
    2+ sigue pendiente de validar con una Liquidación real.
    """
    tipo = clasificar_poliza(fila_poliza, contrato)
    razon_social = fila_poliza["razon_social"]
    fecha_efecto: date | None = fila_poliza["fecha_efecto"]
    # OJO: cuando fecha_efecto viene de una consulta SQL (pd.read_sql con
    # parse_dates), un valor nulo llega como pd.NaT, no como None — y
    # bool(pd.NaT) es True, así que un simple "if fecha_efecto" no basta
    # para detectar la ausencia de fecha (se creía comprobado, no lo estaba).
    # Se exige además que sea una `date` de verdad (isinstance cubre también
    # pd.Timestamp, que hereda de date): un "" o un NaN suelto de pd.isna()
    # sin este chequeo se colarían como "fecha válida" y reventarían igual
    # más abajo al intentar leer .year/.month.
    tiene_fecha_efecto = isinstance(fecha_efecto, date) and not pd.isna(fecha_efecto)
    ref = fecha_referencia if fecha_referencia is not None else date.today()
    meses_desde_efecto = meses_transcurridos(fecha_efecto, ref) if tiene_fecha_efecto else 0
    es_primer_anio = meses_desde_efecto < 12
    mes_devengo = (
        f"{fecha_efecto.year:04d}-{fecha_efecto.month:02d}" if tiene_fecha_efecto else ""
    )

    if tipo == TIPO_VIDA:
        pct = _pct_comision_vida(contrato, razon_social, meses_desde_efecto)
        base = prima_recibo_mensual if prima_recibo_mensual is not None else prima_anual / 12
        return EstimacionComision(
            poliza=fila_poliza["poliza"],
            tipo=tipo,
            mes_devengo=mes_devengo,
            comision_bruta_estimada=round(base * pct, 2),
            confianza="alta",
            nota="Vida: comisión por recibo, sin rappel.",
        )

    # Nota de confianza pendiente para cualquier póliza de Salud en año 2+:
    # el % que se aplica en ese caso (ver _pct_comision_salud) nunca se ha
    # contrastado contra una Liquidación real. El importe no cambia por
    # esto, solo se marca la estimación como menos segura hasta validarlo.
    nota_pendiente_ano2 = (
        "Póliza en año 2+: el % aplicado no está confirmado con datos "
        "reales todavía — pendiente de validar contra una Liquidación real "
        "de una póliza con más de 12 meses."
    )

    if tipo == TIPO_SALUD_ANUAL:
        pct = _pct_comision_salud(contrato, razon_social, es_primer_anio)
        if es_primer_anio:
            confianza = "alta"
            nota = "Salud prepago anual: comisión íntegra en el mes de efecto."
        else:
            confianza = "media"
            nota = f"Salud prepago anual: póliza en año 2+, porcentaje aplicado pendiente de validar. {nota_pendiente_ano2}"
        return EstimacionComision(
            poliza=fila_poliza["poliza"],
            tipo=tipo,
            mes_devengo=mes_devengo,
            comision_bruta_estimada=round(prima_anual * pct, 2),
            confianza=confianza,
            nota=nota,
        )

    # TIPO_SALUD_MENSUAL
    pct = _pct_comision_salud(contrato, razon_social, es_primer_anio)
    nota_mensual = (
        "Salud mensual: anticipo estimado en el mes del primer recibo. "
        "Los meses 2-12 de esta misma póliza no deberían sumar comisión "
        "nueva, pero el mecanismo exacto de regularización de ASISA no "
        "está cerrado al 100% — confirmar siempre contra Liquidación real."
    )
    if not es_primer_anio:
        nota_mensual = f"{nota_mensual} {nota_pendiente_ano2}"
    return EstimacionComision(
        poliza=fila_poliza["poliza"],
        tipo=tipo,
        mes_devengo=mes_devengo,
        comision_bruta_estimada=round(prima_anual * pct, 2),
        # La mecánica mensual ya era "media" independientemente del año
        # (por la incertidumbre de la regularización de ASISA); el año 2+
        # no la baja más, pero sí añade el aviso en la nota.
        confianza="media",
        nota=nota_mensual,
    )


def aplicar_retencion(importe_bruto: float, contrato: ContratoConfig) -> float:
    """Comisión/rappel neto tras aplicar la retención de IRPF configurada."""
    return round(importe_bruto * (1 - contrato.retencion_irpf), 2)


@dataclass
class HistorialAjustesPoliza:
    poliza: str
    tiene_ajustes_previos: bool  # más de 1 evento ANUALIZADA/EXTORNO ANUALIZADA
    num_eventos: int
    ultimo_periodo: str | None = None
    ultimo_importe: float | None = None
    ultimo_accion: str | None = None


def _periodo_liquidacion_ordenable(periodo: object) -> str:
    """Normaliza un periodo de Liquidación a "AAAA-MM" para poder ordenar
    cronológicamente.

    OJO: en los ficheros reales de ASISA, Liquidación usa "MM-AAAA"
    (confirmado con datos reales: "02-2026"), al revés que Facturación/
    Factura PDF, que usan "AAAA-MM" — se detecta cuál es cuál por longitud
    (4 dígitos es el año) en vez de asumir un orden fijo (mismo criterio
    que `_formatear_periodos` en el dashboard).
    """
    partes = str(periodo or "").split("-")
    if len(partes) != 2:
        return str(periodo or "")
    a, b = partes
    return f"{a}-{b}" if len(a) == 4 else f"{b}-{a}"


def evaluar_historial_ajustes_poliza(df_liquidacion: pd.DataFrame, poliza: str) -> HistorialAjustesPoliza:
    """Cuenta apariciones de eventos ANUALIZADA/EXTORNO ANUALIZADA en el
    historial real de Liquidación de una póliza de salud mensual.

    No intenta predecir el importe del próximo ajuste (no sigue una
    fórmula fiable) — solo distingue pólizas cuyo historial nunca mostró
    más de un evento de este tipo (probablemente sin ajustes todavía) de
    las que ya tuvieron alguno (más inciertas de cara a la estimación).
    """
    if df_liquidacion.empty:
        return HistorialAjustesPoliza(poliza, tiene_ajustes_previos=False, num_eventos=0)

    eventos = df_liquidacion[
        (df_liquidacion["poliza"] == poliza)
        & (df_liquidacion["accion"].isin(ACCIONES_AJUSTE_ANUALIZADA))
    ]
    if eventos.empty:
        return HistorialAjustesPoliza(poliza, tiene_ajustes_previos=False, num_eventos=0)

    ordenados = eventos.assign(
        _orden=eventos["periodo_liquidacion"].map(_periodo_liquidacion_ordenable)
    ).sort_values("_orden")
    ultimo = ordenados.iloc[-1]
    return HistorialAjustesPoliza(
        poliza=poliza,
        tiene_ajustes_previos=len(eventos) > 1,
        num_eventos=len(eventos),
        ultimo_periodo=ultimo["periodo_liquidacion"],
        ultimo_importe=float(ultimo["comision"]),
        ultimo_accion=ultimo["accion"],
    )


def refinar_confianza_salud_mensual(
    estimacion: EstimacionComision, df_liquidacion: pd.DataFrame
) -> EstimacionComision:
    """Afina la confianza de una estimación de Salud mensual con el
    historial real de Liquidación de esa póliza concreta, en vez de marcar
    "media" a ciegas para todas por igual.

    - Sin ajustes conocidos en el historial: se mantiene "media" (ver
      docstring del módulo), pero con una nota explícita de que es una
      suposición, no un hecho confirmado.
    - Con algún ajuste ya visto en el pasado (EXTORNO ANUALIZADA o una
      segunda ANUALIZADA): baja a "baja" — esta póliza concreta ya
      demostró no seguir el caso simple, así que es más probable que la
      estimación actual tampoco encaje.
    """
    if estimacion.tipo != TIPO_SALUD_MENSUAL:
        return estimacion

    historial = evaluar_historial_ajustes_poliza(df_liquidacion, estimacion.poliza)

    if not historial.tiene_ajustes_previos:
        nota = (
            f"{estimacion.nota} Sin ajustes históricos conocidos para esta "
            "póliza — asumimos que se mantiene así, pero no está garantizado."
        )
        return dataclasses.replace(estimacion, confianza="media", nota=nota)

    nota = (
        f"{estimacion.nota} Esta póliza ya tuvo ajustes de comisión en el "
        f"pasado (último conocido: {historial.ultimo_accion} de "
        f"{historial.ultimo_importe:.2f} € en {historial.ultimo_periodo}); "
        "es más probable que su comisión real difiera de esta estimación."
    )
    return dataclasses.replace(estimacion, confianza="baja", nota=nota)


def refinar_confianza_producto_asumido(
    estimacion: EstimacionComision, razon_social_asumida: bool
) -> EstimacionComision:
    """Ajusta la confianza a "media" cuando la comisión se calculó con un
    `razon_social` ASUMIDO por defecto, no confirmado.

    Caso real: pólizas provisionales de EIAC donde solo se sabe que son de
    Salud (por DescripcionRamo/CodigoEntidad), no el producto exacto —
    `engine.eiac_integracion.construir_polizas_provisionales_desde_eiac`
    asume "ASISA PARTICULARES" en vez de dejar la comisión en 0€.

    Confianza "media", no "baja": revisando TODO el histórico real de
    Sebastián (7 meses de datos), el 100% de sus ventas de Salud son
    ASISA PARTICULARES o ASISA RED SANITARIA — ambas al mismo 25%/20% —
    nunca ha vendido Travel, Pymes ni Integral (que sí tienen % distintos
    en el contrato). El producto exacto sigue sin confirmar, pero el %
    asumido tiene un respaldo real fuerte, no es una suposición a ciegas.
    """
    if not razon_social_asumida:
        return estimacion
    nota = (
        f"{estimacion.nota} % asumido en base a que el 100% del histórico "
        "de ventas de Salud del agente usa este mismo porcentaje; producto "
        "exacto no confirmado hasta que llegue el CSV oficial."
    ).strip()
    return dataclasses.replace(estimacion, confianza="media", nota=nota)


@dataclass
class ResumenHistorialAjustesCartera:
    """Cuántas pólizas de salud mensual de TODA la cartera (no solo las del
    mes en curso) tienen historial de ajustes irregulares, para explicar
    de dónde viene el rango de error del motor en la pestaña Calibración."""

    total_salud_mensual: int
    con_historial_irregular: int

    @property
    def pct_irregular(self) -> float | None:
        if not self.total_salud_mensual:
            return None
        return round(100 * self.con_historial_irregular / self.total_salud_mensual, 1)


def resumen_historial_ajustes_cartera(
    df_polizas: pd.DataFrame, df_liquidacion: pd.DataFrame, contrato: ContratoConfig
) -> ResumenHistorialAjustesCartera:
    """Recorre toda la cartera de salud mensual y cuenta cuántas pólizas ya
    mostraron algún ajuste en su historial de Liquidación (ver
    `evaluar_historial_ajustes_poliza`). Es una foto de la cartera completa,
    no de un periodo concreto — pensada para dar contexto agregado, no para
    decidir la confianza de una estimación individual (eso lo hace
    `refinar_confianza_salud_mensual`, póliza a póliza).
    """
    if df_polizas.empty:
        return ResumenHistorialAjustesCartera(0, 0)

    salud_mensual = df_polizas[
        (df_polizas["forma_pago"] == "M")
        & (~df_polizas["razon_social"].isin(contrato.comisiones_vida.keys()))
    ]
    con_historial_irregular = sum(
        evaluar_historial_ajustes_poliza(df_liquidacion, poliza).tiene_ajustes_previos
        for poliza in salud_mensual["poliza"]
    )
    return ResumenHistorialAjustesCartera(len(salud_mensual), con_historial_irregular)
