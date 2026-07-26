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

MULTIRRAMO SALUD+ACCIDENTES: el agente a veces vende Accidentes dentro de
una póliza de Salud (multirramo). Investigado julio 2026: el campo SUBRAMO
de Liquidación (fichero CSV oficial) solo se ha visto con el valor "VACIO"
en los datos disponibles, y ni siquiera se ingiere hoy en
`ingestion.liquidacion` (no hay columna `subramo` en la tabla `liquidacion`
ni en ningún otro sitio de la base de datos). Tampoco hay señal equivalente
en los ficheros EIAC (`ingestion.eiac_xml` expone `descripcion_ramo` /
`ramo_entidad`, pero ninguno distingue "Salud con Accidentes incluido" de
"Salud sin Accidentes" — ver `engine.eiac_integracion`). CONCLUSIÓN: con los
datos actuales no hay forma de separar este caso; estas pólizas se
clasifican solo por su `razon_social` de Salud, así que el Accidentes
incluido en una multirramo NO se contabiliza aparte en "ASISA Accidentes"
todavía. Pendiente de confirmar con más datos reales (o con SUBRAMO si algún
día trae un valor real, no solo "VACIO").

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
datos: las pólizas provisionales de EIAC nunca traen `fecha_baja` (el
upsert de `engine.eiac_integracion` la deja siempre en None) ni sus recibos
traen `fecha_hasta` (no existe esa columna en `eiac_recibos`), así que esta
distinción hoy solo puede confirmarse con datos ya presentes en el CSV
oficial — cualquier anulación que solo se vea por EIAC sigue restando por
defecto hasta que llegue el CSV oficial que lo confirme o lo desmienta.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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


def _categoria(razon_social: str, contrato: ContratoConfig) -> str | None:
    """Categoría PAE de una póliza a partir de su razon_social, o None si no
    se reconoce (ni Vida ni ninguna clave de `CATEGORIA_SALUD_POR_RAZON_SOCIAL`)."""
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

    @property
    def pae_total(self) -> float:
        return round(sum(d.pae for d in self.por_categoria.values()), 2)

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
    """
    objetivo_final = objetivo if objetivo is not None else contrato.wanderlust_objetivo_paes
    resultado = ResultadoPae(anio=anio, objetivo=objetivo_final)
    if df_polizas.empty or df_facturacion.empty:
        return resultado

    primeras = _primer_recibo_con_fecha_hasta(df_facturacion)

    columnas = ["poliza", "razon_social", "forma_pago", "fecha_efecto"]
    if "situacion" in df_polizas.columns:
        columnas.append("situacion")
    if "fecha_baja" in df_polizas.columns:
        columnas.append("fecha_baja")
    fusion = primeras.merge(df_polizas[columnas], on="poliza", how="inner")
    if fusion.empty:
        return resultado

    fusion = fusion.dropna(subset=["fecha_efecto"])
    if fusion.empty:
        return resultado
    fusion = fusion[pd.to_datetime(fusion["fecha_efecto"]).dt.year == anio]
    if fusion.empty:
        return resultado

    multiplicadores = contrato.wanderlust_multiplicadores

    for _, fila in fusion.iterrows():
        categoria = _categoria(fila["razon_social"], contrato)
        multiplicador = multiplicadores.get(categoria) if categoria else None
        if multiplicador is None:
            resultado.sin_categoria += 1
            continue

        prima_anual = fila["prima_neta"] if fila["forma_pago"] == "A" else fila["prima_neta"] * 12
        pae_poliza = prima_anual * multiplicador

        desglose = resultado.por_categoria.setdefault(categoria, DesgloseCategoriaPae(categoria))
        es_anulada = fila.get("situacion") == "B"
        if es_anulada and not _es_vencimiento_natural(fila.get("fecha_baja"), fila.get("fecha_hasta")):
            desglose.pae -= pae_poliza
            desglose.polizas_baja += 1
        else:
            desglose.pae += pae_poliza
            desglose.polizas_alta += 1

    for desglose in resultado.por_categoria.values():
        desglose.pae = round(desglose.pae, 2)

    return resultado
