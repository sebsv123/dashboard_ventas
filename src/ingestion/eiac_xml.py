"""Parser de ficheros EIAC (estándar TIREA) en XML.

Dos tipos de fichero, identificables por el nombre:
  - "EIAC-ENV-POLI-*": maestro de pólizas.
  - "EIAC-ENV-RECI-*": recibos.

Namespace: http://www.tirea.es/EIAC/ProcesosEIAC. Encoding real del
fichero: ISO-8859-15 (distinto del latin-1 de los CSV de ASISA, aunque en
la práctica solo difieren en símbolos como el €).

NOTA sobre la estructura XML exacta: el nombre y anidamiento de los
elementos de este parser está inferido de la lista de campos que dio
Sebastián (IdPoliza, SituacionPoliza, ClasePoliza, FechaEfectoInicial,
FechaEmision, DescripcionRiesgo para pólizas; IdPoliza, PrimaTotal,
PrimaNeta, SituacionRecibo, FechaEfectoInicial, ClaseFormaPago para
recibos), no de un fichero EIAC real todavía. Si el primer fichero real
no encaja exactamente, hay que ajustar los XPath de `_iter_registros`,
no la forma de los DataFrames de salida (esa parte sí está pactada).

DECISIÓN DE DISEÑO — por qué se guarda en tablas propias, NO directamente
en `polizas`/`facturacion`: `IdPoliza` aquí tiene el formato
"codigo_cliente-numero_poliza" (estándar TIREA), un espacio de numeración
distinto al de la columna "POLIZA" de los CSV de ASISA. Guardar EIAC
directamente ahí (mezclando ambos espacios de numeración en la misma
columna) rompería en silencio el motor de rappel/comisiones. Por eso EIAC
vive en sus propias tablas (`eiac_polizas`, `eiac_recibos`).

ACTUALIZACIÓN: la correspondencia entre ambos espacios de numeración SÍ
está confirmada (ver `extraer_numero_poliza_asisa`: la parte tras el
último guión de IdPoliza es el número de póliza ASISA) — `engine.
eiac_integracion` usa esa correspondencia para cruzar `eiac_recibos`/
`eiac_polizas` con `polizas`/`facturacion` sin mezclar las tablas brutas
entre sí. Ver el docstring de ese módulo para el detalle.

Duplicados en Recibos: los ficheros reales de ejemplo traen varias líneas
para el mismo recibo (un "intento" de cobro por línea, incluida la
resolución final) — antes de sumar nada, `parsear_eiac_recibos` se queda
con una sola fila por póliza+importe, dando prioridad a SituacionRecibo
"CO" (cobrado) sobre "PE" (pendiente) si ambas existen para el mismo
recibo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd

NS_EIAC = "http://www.tirea.es/EIAC/ProcesosEIAC"
_NS = {"e": NS_EIAC}
ENCODING_EIAC = "ISO-8859-15"

# SituacionRecibo: "CO" (cobrado) es el estado definitivo; "PE" (pendiente)
# es un intento que puede o no acabar en cobro. Cualquier otro código
# desconocido se trata como de menor prioridad que ambos (nunca reemplaza
# un CO/PE ya visto, para no perder información silenciosamente).
_PRIORIDAD_SITUACION_RECIBO = {"PE": 0, "CO": 1}

# ClaseFormaPago: pista (NO un hecho confirmado) de si el recibo es de un
# pago domiciliado (probablemente mensual) o con tarjeta (probablemente
# prepago anual) — ver aviso en `_pista_forma_pago`.
_DOMICILIADO = {"CC", "PC"}
_TARJETA = {"TA"}

COLUMNAS_POLIZAS = [
    "id_poliza", "cliente_codigo", "numero_poliza", "situacion_poliza",
    "clase_poliza", "fecha_efecto_inicial", "fecha_emision", "descripcion_riesgo",
]

COLUMNAS_RECIBOS = [
    "id_poliza", "prima_total", "prima_neta", "situacion_recibo",
    "fecha_efecto_inicial", "clase_forma_pago", "pista_forma_pago",
]


def _texto(elemento: ET.Element, tag: str) -> str | None:
    hijo = elemento.find(f"e:{tag}", _NS)
    if hijo is None or hijo.text is None:
        return None
    texto = hijo.text.strip()
    return texto or None


def _fecha(elemento: ET.Element, tag: str) -> date | None:
    texto = _texto(elemento, tag)
    if not texto:
        return None
    try:
        return date.fromisoformat(texto)
    except ValueError:
        return None


def _decimal(elemento: ET.Element, tag: str) -> float | None:
    texto = _texto(elemento, tag)
    if not texto:
        return None
    try:
        return float(texto)
    except ValueError:
        return None


def _partir_id_poliza(id_poliza: str | None) -> tuple[str | None, str | None]:
    """"codigo_cliente-numero_poliza" -> (codigo_cliente, numero_poliza).

    Divide por el ÚLTIMO guión (no el primero): `codigo_cliente` podría
    llevar guiones propios, y la correspondencia confirmada con ASISA es
    siempre sobre la parte final — ver `extraer_numero_poliza_asisa`.
    """
    if not id_poliza or "-" not in id_poliza:
        return None, None
    cliente_codigo, _, numero_poliza = id_poliza.rpartition("-")
    return cliente_codigo or None, numero_poliza or None


@dataclass
class NumeroPolizaExtraido:
    id_poliza_eiac: str
    numero_poliza: str | None
    reconocido: bool
    motivo: str | None = None


_LONGITUD_MIN_NUMERO_POLIZA = 7
_LONGITUD_MAX_NUMERO_POLIZA = 8


def extraer_numero_poliza_asisa(id_poliza_eiac: str) -> NumeroPolizaExtraido:
    """Extrae el número de póliza ASISA de un IdPoliza EIAC.

    Correspondencia CONFIRMADA por Sebastián con 5 casos reales cruzados a
    mano (p.ej. "24848-64276918" -> "64276918"): la parte tras el ÚLTIMO
    guión de IdPoliza coincide siempre con el número de póliza que usan
    Facturación/Pólizas/Liquidación de ASISA. Esto NO es una inferencia
    dudosa — es una regla ya confirmada — pero se valida el formato
    (numérico puro, 7-8 dígitos, como el resto de números de póliza del
    proyecto) para no fallar en silencio ante un caso raro: la misma red
    de seguridad de "confianza" que se usa en el resto del motor, no
    desconfianza del mapeo en sí.

    OJO — el caso real 64201679/64174100 muestra que, al menos una vez, el
    número que aparece aquí no fue el mismo que acabó siendo el definitivo
    en el CSV oficial de Pólizas (posible renumeración/renovación). Un
    formato válido (numérico, 7-8 dígitos) no garantiza que sea el número
    definitivo, solo que tiene la forma correcta — por eso el código que
    usa este resultado para crear pólizas provisionales nunca debe
    sobreescribir con esto una póliza ya confirmada por el CSV oficial.
    """
    if not id_poliza_eiac or "-" not in id_poliza_eiac:
        return NumeroPolizaExtraido(id_poliza_eiac, None, False, "IdPoliza sin guión: formato inesperado.")
    numero = id_poliza_eiac.rsplit("-", 1)[-1].strip()
    if not numero.isdigit() or not (_LONGITUD_MIN_NUMERO_POLIZA <= len(numero) <= _LONGITUD_MAX_NUMERO_POLIZA):
        return NumeroPolizaExtraido(
            id_poliza_eiac,
            numero or None,
            False,
            f"'{numero}' no es puramente numérico de {_LONGITUD_MIN_NUMERO_POLIZA}-"
            f"{_LONGITUD_MAX_NUMERO_POLIZA} dígitos — revisar manualmente.",
        )
    return NumeroPolizaExtraido(id_poliza_eiac, numero, True)


def _pista_forma_pago(clase_forma_pago: str | None) -> str | None:
    """Traduce ClaseFormaPago a una PISTA de mensual/anual — nunca un hecho
    confirmado (a diferencia de `forma_pago` en el CSV de Pólizas de ASISA,
    que sí viene declarado explícitamente por ASISA)."""
    if clase_forma_pago in _DOMICILIADO:
        return "posible_mensual"
    if clase_forma_pago in _TARJETA:
        return "posible_prepago_anual"
    return None


def _iter_registros(root: ET.Element, tag: str):
    return root.iter(f"{{{NS_EIAC}}}{tag}")


def parsear_eiac_polizas(path: str | Path) -> pd.DataFrame:
    """Parsea un fichero "EIAC-ENV-POLI-*.xml" a un DataFrame, una fila por póliza."""
    tree = ET.parse(path)
    root = tree.getroot()

    filas = []
    for poliza in _iter_registros(root, "Poliza"):
        id_poliza = _texto(poliza, "IdPoliza")
        cliente_codigo, numero_poliza = _partir_id_poliza(id_poliza)
        filas.append(
            {
                "id_poliza": id_poliza,
                "cliente_codigo": cliente_codigo,
                "numero_poliza": numero_poliza,
                "situacion_poliza": _texto(poliza, "SituacionPoliza"),
                "clase_poliza": _texto(poliza, "ClasePoliza"),
                "fecha_efecto_inicial": _fecha(poliza, "FechaEfectoInicial"),
                "fecha_emision": _fecha(poliza, "FechaEmision"),
                "descripcion_riesgo": _texto(poliza, "DescripcionRiesgo"),
            }
        )

    if not filas:
        raise ValueError(
            f"No se encontró ningún registro <Poliza> en {path} — "
            f"¿es realmente un fichero EIAC-ENV-POLI?"
        )
    return pd.DataFrame(filas, columns=COLUMNAS_POLIZAS)


def _deduplicar_recibos(df: pd.DataFrame) -> pd.DataFrame:
    """Colapsa "intentos" repetidos del mismo recibo (misma póliza + mismo
    importe) a una sola fila, dando prioridad a CO sobre PE — ver docstring
    del módulo."""
    if df.empty:
        return df
    df = df.copy()
    df["_prioridad"] = df["situacion_recibo"].map(_PRIORIDAD_SITUACION_RECIBO).fillna(-1)
    df = df.sort_values("_prioridad", kind="stable")
    df = df.drop_duplicates(subset=["id_poliza", "prima_total"], keep="last")
    return df.drop(columns="_prioridad").sort_index().reset_index(drop=True)


def parsear_eiac_recibos(path: str | Path) -> pd.DataFrame:
    """Parsea un fichero "EIAC-ENV-RECI-*.xml" a un DataFrame, una fila por
    recibo YA deduplicado (ver `_deduplicar_recibos`)."""
    tree = ET.parse(path)
    root = tree.getroot()

    filas = []
    for recibo in _iter_registros(root, "Recibo"):
        clase_forma_pago = _texto(recibo, "ClaseFormaPago")
        filas.append(
            {
                "id_poliza": _texto(recibo, "IdPoliza"),
                "prima_total": _decimal(recibo, "PrimaTotal"),
                "prima_neta": _decimal(recibo, "PrimaNeta"),
                "situacion_recibo": _texto(recibo, "SituacionRecibo"),
                "fecha_efecto_inicial": _fecha(recibo, "FechaEfectoInicial"),
                "clase_forma_pago": clase_forma_pago,
                "pista_forma_pago": _pista_forma_pago(clase_forma_pago),
            }
        )

    if not filas:
        raise ValueError(
            f"No se encontró ningún registro <Recibo> en {path} — "
            f"¿es realmente un fichero EIAC-ENV-RECI?"
        )
    df = pd.DataFrame(filas, columns=COLUMNAS_RECIBOS)
    return _deduplicar_recibos(df)


def detectar_tipo_eiac(nombre_fichero: str) -> str:
    """Devuelve "polizas" o "recibos" a partir del nombre del fichero
    ("EIAC-ENV-POLI-*" / "EIAC-ENV-RECI-*"). Lanza ValueError si no
    reconoce ninguno de los dos patrones."""
    nombre = nombre_fichero.upper()
    if "POLI" in nombre:
        return "polizas"
    if "RECI" in nombre:
        return "recibos"
    raise ValueError(
        f"No se reconoce el tipo de fichero EIAC a partir del nombre '{nombre_fichero}' "
        "(se esperaba 'EIAC-ENV-POLI-*' o 'EIAC-ENV-RECI-*')."
    )
