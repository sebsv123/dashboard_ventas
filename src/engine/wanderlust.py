"""PAE (Primas Anualizadas Equivalentes) — incentivo comercial "Wanderlust"
de ASISA para 2026. Bases legales adjuntas al agente (fuera de este repo).

DISTINTO del "Objetivo anual" (`engine.objetivo`): aquí no se mide
producción bruta en euros, sino la variación de cartera anualizada por
producto, ponderada por un multiplicador (`config/contrato.yaml`,
`wanderlust.multiplicadores_paes`), entre el 1 de enero y el 31 de
diciembre de 2026.

DIFERENCIA CRÍTICA DE PERIODO: NO se usa `periodo_liquidacion` ni el ciclo
16→15 que usa el resto del proyecto (rappel/comisión/objetivo, ver
`engine.insights.primeras_altas_por_periodo`). Para PAE, una póliza cuenta
en cuanto su `fecha_efecto` cae dentro del año natural 2026, sin importar
cuándo se factura. `periodo_liquidacion` solo se usa aquí para llegar al
primer recibo de cada póliza (misma función reutilizada que en el resto del
proyecto, `primeras_altas_por_periodo`, porque es la forma ya existente de
obtener la prima_neta de cada póliza) — nunca para filtrar ni agrupar por
año/mes.

CATEGORÍA POR PRODUCTO: las bases legales de Wanderlust usan sus propias
categorías ("Salud Particulares", "Salud colectivos privados", ...), que no
coinciden 1:1 con las claves de `razon_social` de `config/contrato.yaml`.
`CATEGORIA_SALUD_POR_RAZON_SOCIAL` de abajo es la traducción — ASUNCIÓN no
confirmada por ASISA para "ASISA INTEGRAL"/"ASISA PYMES" -> "Salud
colectivos privados" (son los únicos productos de Salud que no son
individuales en el contrato); revisar si aparece contradicción real.

MULTIRRAMO SALUD+ACCIDENTES: si EIAC POLI trae en `coberturas_wanderlust`
los códigos GS30 (Dental), GS09 (Hospitalización) o GS99 (Accidentes) con
sus primas, se separan de la prima de Salud y se asignan a sus categorías
PAE sin doble conteo. Si faltan esas coberturas, códigos o primas, no hay
base fiable para separarlas y la póliza conserva la clasificación general
de Salud. El campo SUBRAMO de Liquidación solo se ha visto como "VACIO" y
no se ingiere, por lo que tampoco permite completar los casos sin detalle
de coberturas en EIAC POLI.

PUNTO DE PARTIDA (cartera a 1 de enero de 2026): tratado como 0
explícitamente — el agente no tenía producción antes de febrero de 2026
(confirmado en conversaciones previas). Si algún día se descubre cartera
previa real, esta asunción habrá que revisarla (afectaría a qué cuenta como
"variación" en vez de partir de 0).

VENCIMIENTO NATURAL VS. CANCELACIÓN ANTICIPADA — aclaración del agente
sobre el caso real 64171931 (ASISA Travel and You): esa póliza NO fue una
cancelación anticipada, sino un producto de duración fija (viaje de 9 días)
que venció de forma normal al terminar su cobertura contratada
(FechaAnulacion 19/06/2026 en EIAC coincide EXACTAMENTE con la fecha_hasta
del recibo). El PDF de Wanderlust dice que "en caso de anulaciones se
restará toda la prima anual" — eso se refiere a cancelaciones anticipadas
reales, no al fin de cobertura normal de un producto ya diseñado para durar
poco. Por eso `situacion == "B"` YA NO basta por sí sola: solo resta si la
anulación ocurrió ANTES de agotar la cobertura contratada del recibo
(`fecha_baja < fecha_hasta`); si coincide con el fin de cobertura o es
posterior, cuenta como PAE ganado normal, igual que si nunca se hubiera
anulado. Sin ambas fechas (`fecha_baja` de Pólizas, `fecha_hasta` del
primer recibo) no se puede confirmar el vencimiento natural, así que por
defecto SÍ se resta — ver `_es_vencimiento_natural`. Limitación real de
datos: EIAC RECI no trae `fecha_hasta`, pero EIAC POLI puede aportar
`fecha_anulacion` y `fecha_fin_seguro`. Si falta alguna de las dos fechas,
el vencimiento natural no se puede confirmar y la anulación resta por
defecto hasta disponer de datos que lo aclaren.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

import pandas as pd

from engine.config_contrato import ContratoConfig

# razon_social (Salud) -> categoría PAE de las bases legales Wanderlust.
# Ver docstring del módulo sobre la asunción de INTEGRAL/PYMES.
CATEGORIA_SALUD_POR_RAZON_SOCIAL = {
    "ASISA PARTICULARES": "Salud Particulares",
    "ASISA RED SANITARIA": "Salud Particulares",  # mismo grupo comercial, ver contrato.yaml
    "ASISA INTEGRAL": "Salud colectivos privados",
    "ASISA PYMES": "Salud colectivos privados",
    "ASISA TRAVEL AND YOU": "ASISA Travel",
    "ACCIDENTES": "ASISA Accidentes",
    "DECESOS": "ASISA Decesos",
    "DENTAL": "ASISA Dental",
    "HOSPITALIZACION": "ASISA Hospitalización",
    "MASCOTAS": "ASISA Mascotas",
}

CATEGORIA_VIDA = "ASISA Vida"


def redondear_euros(valor: float) -> float:
    """Redondeo monetario reproducible, con medio céntimo hacia arriba."""
    return float(Decimal(str(round(float(valor), 6))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _categoria(razon_social: str, contrato: ContratoConfig) -> str | None:
    """Categoría PAE de una póliza a partir de su razon_social, o None si no
    se reconoce (ni Vida ni ninguna clave de `CATEGORIA_SALUD_POR_RAZON_SOCIAL`)."""
    if razon_social == "AV ACCIDENTES SENIOR":
        return "ASISA Accidentes"
    if razon_social in contrato.comisiones_vida:
        return CATEGORIA_VIDA
    return CATEGORIA_SALUD_POR_RAZON_SOCIAL.get(razon_social)


def _primer_recibo_con_fecha_hasta(df_facturacion: pd.DataFrame) -> pd.DataFrame:
    """Igual que `primeras_altas_por_periodo` (mismo criterio de "primer
    recibo": ordenado por poliza/periodo_liquidacion/fecha_desde), pero
    conserva también `fecha_hasta` — el fin de la cobertura contratada de
    ese recibo, necesario para `_es_vencimiento_natural` y que la función
    compartida no expone (ver su contrato de 3 columnas)."""
    columnas = ["poliza", "periodo_liquidacion", "prima_neta", "fecha_hasta"]
    if df_facturacion.empty:
        return pd.DataFrame(columns=columnas)
    df = df_facturacion.copy()
    if "fecha_hasta" not in df.columns:
        df["fecha_hasta"] = None
    ordenado = df.sort_values(["poliza", "periodo_liquidacion", "fecha_desde"])
    primeras = ordenado.groupby("poliza", as_index=False).first()
    return primeras[columnas]


def _es_vencimiento_natural(fecha_baja, fecha_hasta) -> bool:
    """True si la anulación coincide con (o es posterior a) el fin de la
    cobertura contratada del recibo — vencimiento normal de un producto de
    duración fija (p.ej. Travel), no una cancelación anticipada real (ver
    caso real 64171931 en el docstring del módulo).

    Sin ambas fechas no se puede confirmar -> False por defecto (se resta,
    como exige el PDF de Wanderlust para cualquier anulación no confirmada
    como vencimiento natural)."""
    if fecha_baja is None or fecha_hasta is None or pd.isna(fecha_baja) or pd.isna(fecha_hasta):
        return False
    return pd.Timestamp(fecha_baja) >= pd.Timestamp(fecha_hasta)


@dataclass
class DesgloseCategoriaPae:
    categoria: str
    pae: float = 0.0
    polizas_alta: int = 0
    polizas_baja: int = 0


@dataclass
class ResultadoPae:
    anio: int
    objetivo: float | None
    por_categoria: dict[str, DesgloseCategoriaPae] = field(default_factory=dict)
    # Pólizas con fecha_efecto en el año pero razon_social no reconocida
    # (ni Vida ni ninguna categoría de Salud conocida) — no suman al total.
    sin_categoria: int = 0
    pae_efectivo: float = 0.0
    pae_futuro: float = 0.0
    altas_efectivas: int = 0
    bajas_efectivas: int = 0

    @property
    def pae_total(self) -> float:
        return redondear_euros(sum(redondear_euros(d.pae) for d in self.por_categoria.values()))

    @property
    def porcentaje(self) -> float | None:
        if not self.objetivo:
            return None
        return round(self.pae_total / self.objetivo * 100, 1)


def calcular_pae_anual(
    df_polizas: pd.DataFrame,
    df_facturacion: pd.DataFrame,
    contrato: ContratoConfig,
    anio: int,
    objetivo: float | None = None,
    df_eiac_polizas: pd.DataFrame | None = None,
    fecha_corte=None,
) -> ResultadoPae:
    """PAE acumulado del año `anio`, desglosado por categoría de producto.

    Usa `fecha_efecto` de `df_polizas` para decidir si una póliza cuenta
    (NO `periodo_liquidacion`, ver docstring del módulo). La prima_neta de
    cada póliza se obtiene de su primer recibo en `df_facturacion`
    (`_primer_recibo_con_fecha_hasta`, mismo criterio de "primer recibo" que
    `engine.insights.primeras_altas_por_periodo` usa en el resto del
    proyecto) — solo se usa para conseguir el importe y `fecha_hasta`, no
    para filtrar por periodo.

    Pólizas con `situacion == "B"` (anulada/baja) RESTAN su PAE completo del
    acumulado de su categoría, en vez de simplemente no sumar — SALVO que la
    anulación coincida con (o sea posterior a) el fin de la cobertura
    contratada del recibo (`fecha_baja >= fecha_hasta`), en cuyo caso es un
    vencimiento natural (producto de duración fija, p.ej. Travel) y cuenta
    como PAE ganado normal — ver `_es_vencimiento_natural` y el caso real
    64171931 en el docstring del módulo.

    Con `fecha_corte`, `pae_efectivo` refleja las altas y anulaciones que ya
    habían ocurrido a esa fecha; `pae_futuro` recoge la variación posterior
    necesaria para llegar al PAE total, sin cambiar el total anual.
    """
    objetivo_final = objetivo if objetivo is not None else contrato.wanderlust_objetivo_paes
    resultado = ResultadoPae(anio=anio, objetivo=objetivo_final)
    if df_polizas.empty and (df_eiac_polizas is None or df_eiac_polizas.empty):
        return resultado

    primeras = _primer_recibo_con_fecha_hasta(df_facturacion)
    recibos = primeras.set_index("poliza").to_dict("index") if not primeras.empty else {}
    eiac_por_poliza = {}
    if df_eiac_polizas is not None and not df_eiac_polizas.empty:
        for _, eiac in df_eiac_polizas.iterrows():
            numero = str(eiac.get("numero_poliza", ""))
            if numero and numero != "nan":
                eiac_por_poliza[numero] = eiac

    columnas = ["poliza", "razon_social", "forma_pago", "fecha_efecto"]
    columnas += [c for c in ("situacion", "fecha_baja") if c in df_polizas.columns]
    filas = df_polizas[columnas].to_dict("records") if not df_polizas.empty else []
    existentes = {str(f["poliza"]) for f in filas}
    if df_eiac_polizas is not None:
        for _, eiac in df_eiac_polizas.iterrows():
            numero = str(eiac.get("numero_poliza", ""))
            if not numero or numero == "nan" or numero in existentes:
                continue
            ramo = str(eiac.get("ramo_entidad") or "").upper()
            razon = "ASISA TRAVEL AND YOU" if ramo == "RAVI" else (
                "ASISA VIDA TRANQUILIDAD" if ramo == "VIDA" else "ASISA PARTICULARES"
            )
            filas.append({
                "poliza": numero, "razon_social": razon, "forma_pago": None,
                "fecha_efecto": eiac.get("fecha_efecto_inicial"),
                "situacion": "B" if eiac.get("situacion_poliza") in {"EX", "BJ"} else "A",
                "fecha_baja": eiac.get("fecha_anulacion"),
            })
    fusion = pd.DataFrame(filas)
    if fusion.empty:
        return resultado
    fusion = fusion.dropna(subset=["fecha_efecto"])
    fusion = fusion[pd.to_datetime(fusion["fecha_efecto"]).dt.year == anio]

    multiplicadores = contrato.wanderlust_multiplicadores
    codigos_embebidos = {
        "GS30": ("ASISA Dental", 1.5),
        "GS09": ("ASISA Hospitalización", 4.0),
        "GS99": ("ASISA Accidentes", 4.0),
    }

    def _coberturas(eiac):
        if eiac is None:
            return []
        valor = eiac.get("coberturas_wanderlust")
        if not valor or pd.isna(valor):
            return []
        try:
            return json.loads(valor) if isinstance(valor, str) else valor
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    corte = pd.Timestamp(fecha_corte) if fecha_corte is not None else None
    for _, fila in fusion.iterrows():
        categoria = _categoria(fila["razon_social"], contrato)
        multiplicador = multiplicadores.get(categoria) if categoria else None
        if multiplicador is None:
            resultado.sin_categoria += 1
            continue

        eiac = eiac_por_poliza.get(str(fila["poliza"]))
        recibo = recibos.get(str(fila["poliza"]), {})
        forma_pago = str(fila.get("forma_pago") or "").upper()
        prima_fact = recibo.get("prima_neta")
        prima_fact_anual = None
        if prima_fact is not None and not pd.isna(prima_fact):
            prima_fact_anual = float(prima_fact) * (1.0 if forma_pago in {"A", "U"} else 12.0)
        es_travel = categoria == "ASISA Travel"
        prima_poli = None
        if eiac is not None:
            prima_poli = eiac.get("prima_neta_poliza") if es_travel else eiac.get("prima_neta_anualizada_poli")
            if prima_poli is None or pd.isna(prima_poli):
                prima_poli = eiac.get("prima_neta_poliza")
        prima_anual = float(prima_poli) if prima_poli is not None and not pd.isna(prima_poli) else prima_fact_anual
        if prima_anual is None:
            resultado.sin_categoria += 1
            continue
        coberturas = _coberturas(eiac)
        es_salud = categoria in {"Salud Particulares", "Salud colectivos privados"}
        embebidas = []
        if es_salud:
            for cobertura in coberturas:
                codigo = str(cobertura.get("id_cobertura") or "").upper()
                if codigo in codigos_embebidos and cobertura.get("prima_neta") is not None:
                    embebidas.append((codigo, float(cobertura["prima_neta"])))

        desglose = resultado.por_categoria.setdefault(categoria, DesgloseCategoriaPae(categoria))
        pae_salud = 0.0
        pae_embebidas = 0.0
        if embebidas and es_salud:
            prima_salud = max(prima_anual - sum(prima for _, prima in embebidas), 0.0)
            pae_salud = prima_salud * multiplicador
            for codigo, prima in embebidas:
                _, multiplicador_extra = codigos_embebidos[codigo]
                pae_embebidas += prima * multiplicador_extra
            pae_poliza = pae_salud + pae_embebidas
        else:
            pae_poliza = prima_anual * multiplicador
        es_anulada = fila.get("situacion") == "B"
        if eiac is not None and eiac.get("situacion_poliza") in {"EX", "BJ", "AN"}:
            es_anulada = True
        fecha_baja = fila.get("fecha_baja")
        if (fecha_baja is None or pd.isna(fecha_baja)) and eiac is not None:
            fecha_baja = eiac.get("fecha_anulacion")
        fecha_hasta = recibo.get("fecha_hasta")
        if pd.isna(fecha_hasta):
            fecha_hasta = None
        if fecha_hasta is None and eiac is not None:
            fecha_hasta = eiac.get("fecha_fin_seguro")
        efecto = pd.Timestamp(fila["fecha_efecto"])
        anulacion_real = es_anulada and not _es_vencimiento_natural(fecha_baja, fecha_hasta)
        # Una anulación con efecto el mismo día (o antes) del alta no llegó
        # a ser producción: se excluye, no se convierte en PAE negativo.
        anulacion_antes_de_producir = (
            anulacion_real and fecha_baja is not None and not pd.isna(fecha_baja)
            and pd.Timestamp(fecha_baja) <= efecto
        )
        signo = 0.0 if anulacion_antes_de_producir else (-1.0 if anulacion_real else 1.0)
        if embebidas and es_salud:
            desglose.pae += signo * pae_salud
            for codigo, prima in embebidas:
                categoria_extra, multiplicador_extra = codigos_embebidos[codigo]
                extra = resultado.por_categoria.setdefault(categoria_extra, DesgloseCategoriaPae(categoria_extra))
                extra.pae += signo * prima * multiplicador_extra
        else:
            desglose.pae += signo * pae_poliza
        if signo < 0:
            desglose.polizas_baja += 1
        elif signo > 0:
            desglose.polizas_alta += 1

        if corte is not None:
            if efecto <= corte:
                baja_posterior = (
                    anulacion_real and fecha_baja is not None and not pd.isna(fecha_baja)
                    and pd.Timestamp(fecha_baja) > corte
                )
                signo_corte = 1.0 if baja_posterior else signo
                resultado.pae_efectivo += signo_corte * pae_poliza
                resultado.pae_futuro += (signo - signo_corte) * pae_poliza
                if signo_corte > 0:
                    resultado.altas_efectivas += 1
                elif signo_corte < 0:
                    resultado.bajas_efectivas += 1
            else:
                resultado.pae_futuro += signo * pae_poliza

    resultado.pae_efectivo = redondear_euros(resultado.pae_efectivo)
    resultado.pae_futuro = redondear_euros(resultado.pae_futuro)

    return resultado
