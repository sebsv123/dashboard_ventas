"""Parser de ficheros EIAC (estándar TIREA) en XML.

Dos tipos de fichero, identificables por el nombre:
  - "EIAC-ENV-POLI-*": maestro de pólizas.
  - "EIAC-ENV-RECI-*": recibos.

Namespace: http://www.tirea.es/EIAC/ProcesosEIAC. Encoding real del
fichero: ISO-8859-15 (distinto del latin-1 de los CSV de ASISA, aunque en
la práctica solo difieren en símbolos como el €).

ESTRUCTURA XML — confirmada contra 8 ficheros reales de Sebastián (no ya
una inferencia, como decía una versión anterior de este docstring). El
elemento raíz es `<ProcesosEIAC>` (con el namespace de arriba), y los
registros están en `<Objetos><Poliza>...</Poliza></Objetos>` /
`<Objetos><Recibo>...</Recibo></Objetos>` — pero como `_iter_registros`
busca por todos los descendientes (`root.iter(...)`), el anidamiento
hasta llegar ahí es irrelevante. Lo que SÍ importa, y rompió en producción
la primera vez (`_texto()` solo miraba hijos DIRECTOS), es dónde vive
cada campo DENTRO de `<Poliza>`/`<Recibo>`:

  <Poliza>
    <SituacionPoliza>          (hijo directo)
    <ClasePoliza>               (hijo directo — código de TRANSACCIÓN:
                                 NP=nueva póliza, SU=suplemento, AN=anulación,
                                 no el ramo/producto; ver aviso más abajo)
    <DatosPoliza>
      <IdPoliza>                (codigo_cliente-numero_poliza)
    <Fechas>
      <FechaEfectoInicial>      (con hora: "2026-07-01T00:00:00")
      <FechaEmision>
    <DatosRiesgos>
      <Riesgo>                  (puede haber VARIOS — pólizas familiares)
        <NumeroOrden>
        <DescripcionRiesgo>

  <Recibo>
    <DatosPoliza>
      <IdPoliza>
    <DatosRecibo>
      <SituacionRecibo>
      <Fechas>
        <FechaEfectoInicial>
      <GestionCobro>
        <DatosFormaPago>
          <ClaseFormaPago>
      <DatosImportes>
        <Importes>
          <PrimaTotal>
          <PrimaNeta>

AVISO sobre ClasePoliza: en los ficheros reales, `ClasePoliza` es un
código de TRANSACCIÓN (NP/SU/AN/...), NO indica el ramo del producto
(salud/vida) como se asumió al principio. La distinción salud/vida SÍ
está disponible, pero en otros dos campos, ambos dentro de `<DatosPoliza>`:

  <DatosPoliza>
    <CodigoEntidad>
      <CodigoInterno>Asisa</CodigoInterno>   (confirmado real: "Asisa" en
                                               pólizas de Salud; el valor
                                               para Vida aún no se ha visto
                                               en ningún fichero real)
    <DatosRamo>
      <DescripcionRamo>Asistencia sanitaria</DescripcionRamo>  (confirmado
                                               real, señal fuerte de Salud)

Ver `engine.eiac_integracion._es_salud_por_ramo` para cómo se usan.

RAMO TRAVEL — caso real (póliza 64171931, ASISA Travel and You, Daniella
Valentina Salloum): dentro de `<DatosRamo>` hay un tercer campo,
`RamoEntidad`, que para esta póliza vale "RAVI" (RamoDGS=2131,
DescripcionRamo="Asistencia en viaje") — frente a `RamoEntidad="RASA"`
(RamoDGS=231, "Asistencia sanitaria") en Salud normal. Como
"ASISA TRAVEL AND YOU" es el único producto de viaje en
`config/contrato.yaml` (20%/0%, distinto del 25%/20% de Particulares),
`RamoEntidad="RAVI"` es una señal inequívoca — ver
`engine.eiac_integracion._es_travel_por_ramo`.

ANULACIÓN — mismo caso real: un fichero POLI posterior trajo
`ClasePoliza=AN`, `SituacionPoliza=EX` y, DENTRO de `<Poliza>` pero como
HERMANO de `<Fechas>` (no anidado dentro), un bloque `<DatosAnulacion>`:

  <Poliza>
    <DatosAnulacion>
      <FechaAnulacion>       (con hora, igual que el resto de fechas EIAC)
      <MotivoAnulacion>      (p.ej. "NI")
    <Fechas>
      ...

Confirmado con el fichero real: `DatosAnulacion/FechaAnulacion` y
`DatosAnulacion/MotivoAnulacion`.

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

Duplicados en Recibos: los ficheros reales traen varias líneas para el
mismo recibo (un "intento" de cobro por línea, incluida la resolución
final) — a veces DIEZ o más intentos antes del definitivo. IMPORTANTE:
el importe (PrimaTotal) puede variar unos céntimos entre intentos del
MISMO recibo (recálculos de recargos/DGS), así que la identidad de un
recibo para deduplicar es (id_poliza, fecha_efecto_inicial), NO
(id_poliza, prima_total) como se asumió al principio — usar el importe
como parte de la clave dejaba intentos con centimos distintos como filas
"distintas" en vez de colapsarlos, y el resultado dependía del orden de
inserción (visto con datos reales: 64201679, 64226440, 64261922, todos
con algún intento PE con un importe ligeramente distinto al CO final).
`parsear_eiac_recibos` se queda con una sola fila por (id_poliza,
fecha_efecto_inicial), dando prioridad a SituacionRecibo "CO" (cobrado)
sobre "PE" (pendiente) si ambas existen para el mismo recibo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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
    "descripcion_ramo", "codigo_entidad_interno", "ramo_entidad",
    "fecha_anulacion", "motivo_anulacion", "prima_neta_poliza",
]

COLUMNAS_POLIZAS_RIESGOS = [
    "id_poliza", "numero_orden", "descripcion_riesgo", "fecha_inicio", "id_riesgo_eiac",
]

COLUMNAS_RECIBOS = [
    "id_poliza", "prima_total", "prima_neta", "situacion_recibo",
    "fecha_efecto_inicial", "clase_forma_pago", "pista_forma_pago",
]


def _elemento(elemento: ET.Element, ruta: str) -> ET.Element | None:
    """Busca `ruta` ("A/B/C") como cadena de hijos directos, con el
    namespace EIAC en cada segmento. A propósito NO es una búsqueda
    recursiva (".//"): dos campos distintos pueden compartir nombre de
    tag en profundidades distintas (p.ej. "FechaEfectoInicial" aparece
    tanto en `<Poliza><Fechas>` como en `<Recibo><DatosRecibo><Fechas>`),
    así que hace falta la ruta exacta para no coger el equivocado.
    """
    xpath = "/".join(f"e:{parte}" for parte in ruta.split("/"))
    return elemento.find(xpath, _NS)


def _texto(elemento: ET.Element, ruta: str) -> str | None:
    hijo = _elemento(elemento, ruta)
    if hijo is None or hijo.text is None:
        return None
    texto = hijo.text.strip()
    return texto or None


def _fecha(elemento: ET.Element, ruta: str) -> date | None:
    texto = _texto(elemento, ruta)
    if not texto:
        return None
    try:
        # Los ficheros reales traen fecha+hora ("2026-07-01T00:00:00"), no
        # solo fecha -- date.fromisoformat() no acepta eso y fallaba en
        # silencio (ValueError capturado más abajo) devolviendo None
        # incluso con el XPath correcto. datetime.fromisoformat() acepta
        # ambos formatos (con y sin hora).
        return datetime.fromisoformat(texto).date()
    except ValueError:
        return None


def _decimal(elemento: ET.Element, ruta: str) -> float | None:
    texto = _texto(elemento, ruta)
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


def _riesgo_principal(poliza: ET.Element) -> str | None:
    """DescripcionRiesgo del riesgo con NumeroOrden=1 (el "principal" de la
    póliza) — puede haber varios `<Riesgo>` en una póliza familiar; el
    resto se conserva en `parsear_eiac_polizas_riesgos`, no se pierde."""
    riesgos = poliza.findall("e:DatosRiesgos/e:Riesgo", _NS)
    for riesgo in riesgos:
        if _texto(riesgo, "NumeroOrden") == "1":
            return _texto(riesgo, "DescripcionRiesgo")
    if riesgos:
        return _texto(riesgos[0], "DescripcionRiesgo")
    return None


def parsear_eiac_polizas(path: str | Path) -> pd.DataFrame:
    """Parsea un fichero "EIAC-ENV-POLI-*.xml" a un DataFrame, una fila por póliza.

    `descripcion_riesgo` es solo el riesgo NumeroOrden=1 (el principal) —
    usa `parsear_eiac_polizas_riesgos` para el resto de asegurados de
    pólizas familiares.

    `prima_neta_poliza`: la prima neta TOTAL de la póliza (suma de todas
    sus coberturas), de `<Poliza><DatosImportes><Importes><PrimaNeta>` —
    hermano directo de `<DatosRiesgos>`, NO la de `<Recibo>` (otro nodo
    completamente distinto, ver módulo). Verificado con datos reales que
    es exactamente la suma de las `PrimaNeta` de cada `<Cobertura>` dentro
    de `<DatosRiesgos>` (caso real: póliza 64529498, 450,60+0,60+113,40+
    26,40+4,80 = 595,80€, que es lo que trae este nodo). Si el fichero no
    trae este nodo, queda `None` — nunca se inventa ni se suma a mano
    desde las coberturas (mismo criterio de "no inventar" del resto del
    parser).
    """
    tree = ET.parse(path)
    root = tree.getroot()

    filas = []
    for poliza in _iter_registros(root, "Poliza"):
        id_poliza = _texto(poliza, "DatosPoliza/IdPoliza")
        cliente_codigo, numero_poliza = _partir_id_poliza(id_poliza)
        filas.append(
            {
                "id_poliza": id_poliza,
                "cliente_codigo": cliente_codigo,
                "numero_poliza": numero_poliza,
                "situacion_poliza": _texto(poliza, "SituacionPoliza"),
                "clase_poliza": _texto(poliza, "ClasePoliza"),
                "fecha_efecto_inicial": _fecha(poliza, "Fechas/FechaEfectoInicial"),
                "fecha_emision": _fecha(poliza, "Fechas/FechaEmision"),
                "descripcion_riesgo": _riesgo_principal(poliza),
                "descripcion_ramo": _texto(poliza, "DatosPoliza/DatosRamo/DescripcionRamo"),
                "codigo_entidad_interno": _texto(poliza, "DatosPoliza/CodigoEntidad/CodigoInterno"),
                "ramo_entidad": _texto(poliza, "DatosPoliza/DatosRamo/RamoEntidad"),
                "fecha_anulacion": _fecha(poliza, "DatosAnulacion/FechaAnulacion"),
                "motivo_anulacion": _texto(poliza, "DatosAnulacion/MotivoAnulacion"),
                "prima_neta_poliza": _decimal(poliza, "DatosImportes/Importes/PrimaNeta"),
            }
        )

    if not filas:
        raise ValueError(
            f"No se encontró ningún registro <Poliza> en {path} — "
            f"¿es realmente un fichero EIAC-ENV-POLI?"
        )
    return pd.DataFrame(filas, columns=COLUMNAS_POLIZAS)


def parsear_eiac_polizas_riesgos(path: str | Path) -> pd.DataFrame:
    """Parsea TODOS los `<Riesgo>` de cada póliza (una fila por asegurado),
    incluidas las pólizas familiares con más de uno — complemento de
    `parsear_eiac_polizas`, que solo se queda con el NumeroOrden=1."""
    tree = ET.parse(path)
    root = tree.getroot()

    filas = []
    for poliza in _iter_registros(root, "Poliza"):
        id_poliza = _texto(poliza, "DatosPoliza/IdPoliza")
        for riesgo in poliza.findall("e:DatosRiesgos/e:Riesgo", _NS):
            filas.append(
                {
                    "id_poliza": id_poliza,
                    "numero_orden": _texto(riesgo, "NumeroOrden"),
                    "descripcion_riesgo": _texto(riesgo, "DescripcionRiesgo"),
                    "fecha_inicio": _fecha(riesgo, "FechaInicio"),
                    "id_riesgo_eiac": _texto(riesgo, "IdRiesgo"),
                }
            )
    return pd.DataFrame(filas, columns=COLUMNAS_POLIZAS_RIESGOS)


def _deduplicar_recibos(df: pd.DataFrame) -> pd.DataFrame:
    """Colapsa "intentos" repetidos del mismo recibo (misma póliza + misma
    fecha de efecto) a una sola fila, dando prioridad a CO sobre PE — ver
    docstring del módulo (el importe NO forma parte de la identidad del
    recibo: fluctúa unos céntimos entre intentos reales)."""
    if df.empty:
        return df
    df = df.copy()
    df["_prioridad"] = df["situacion_recibo"].map(_PRIORIDAD_SITUACION_RECIBO).fillna(-1)
    df = df.sort_values("_prioridad", kind="stable")
    df = df.drop_duplicates(subset=["id_poliza", "fecha_efecto_inicial"], keep="last")
    return df.drop(columns="_prioridad").sort_index().reset_index(drop=True)


def parsear_eiac_recibos(path: str | Path) -> pd.DataFrame:
    """Parsea un fichero "EIAC-ENV-RECI-*.xml" a un DataFrame, una fila por
    recibo YA deduplicado (ver `_deduplicar_recibos`)."""
    tree = ET.parse(path)
    root = tree.getroot()

    filas = []
    for recibo in _iter_registros(root, "Recibo"):
        clase_forma_pago = _texto(recibo, "DatosRecibo/GestionCobro/DatosFormaPago/ClaseFormaPago")
        filas.append(
            {
                "id_poliza": _texto(recibo, "DatosPoliza/IdPoliza"),
                "prima_total": _decimal(recibo, "DatosRecibo/DatosImportes/Importes/PrimaTotal"),
                "prima_neta": _decimal(recibo, "DatosRecibo/DatosImportes/Importes/PrimaNeta"),
                "situacion_recibo": _texto(recibo, "DatosRecibo/SituacionRecibo"),
                "fecha_efecto_inicial": _fecha(recibo, "DatosRecibo/Fechas/FechaEfectoInicial"),
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
