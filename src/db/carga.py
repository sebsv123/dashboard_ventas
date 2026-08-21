"""Carga de DataFrames normalizados a SQLite, sin duplicar filas ya importadas."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pandas as pd

from engine.config_contrato import ContratoConfig
from engine.insights import construir_produccion_polizas


@dataclass(frozen=True)
class ResultadoCargaPolizasProvisionalesEiac:
    """Resultado de una carga EIAC, separado de los metadatos del UPSERT."""

    nuevas: int
    actualizadas: int
    sin_cambios: int
    ignoradas_oficiales: int
    total_provisionales: int


def _fecha_a_texto(valor):
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    return valor.isoformat()


def cargar_facturacion(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    filas_insertadas = 0
    cur = conn.cursor()
    for _, r in df.iterrows():
        cur.execute(
            """
            INSERT OR IGNORE INTO facturacion
                (poliza, cliente_codigo, cartera, producto_nombre, fecha_desde,
                 fecha_hasta, prima_neta, prima_total, periodo_liquidacion,
                 duracion_recibo_meses)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["poliza"], r["cliente_codigo"], r["cartera"], r["producto_nombre"],
                _fecha_a_texto(r["fecha_desde"]), _fecha_a_texto(r["fecha_hasta"]),
                r["prima_neta"], r["prima_total"], r["periodo_liquidacion"],
                r["duracion_recibo_meses"],
            ),
        )
        filas_insertadas += cur.rowcount
    conn.commit()
    return filas_insertadas


def cargar_polizas(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """Upsert por póliza: siempre nos quedamos con el dato más reciente conocido.

    Pone `origen='ASISA_CSV'` y limpia `nota_origen` siempre, incluso si la
    póliza ya existía como fila PROVISIONAL de EIAC (ver
    `cargar_polizas_provisionales_eiac`) — el CSV oficial de ASISA siempre
    "confirma" y sustituye a lo provisional.
    """
    filas_insertadas = 0
    cur = conn.cursor()
    for _, r in df.iterrows():
        cur.execute(
            """
            INSERT INTO polizas
                (poliza, cliente_codigo, razon_social, producto_base, producto_codigo,
                 fecha_emision, fecha_efecto, fecha_baja, forma_pago, situacion,
                 provincia_tomador, delegacion, nombre_tomador, origen, nota_origen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ASISA_CSV', NULL)
            ON CONFLICT(poliza) DO UPDATE SET
                razon_social=excluded.razon_social,
                producto_base=excluded.producto_base,
                producto_codigo=excluded.producto_codigo,
                fecha_emision=excluded.fecha_emision,
                fecha_efecto=excluded.fecha_efecto,
                fecha_baja=excluded.fecha_baja,
                forma_pago=excluded.forma_pago,
                situacion=excluded.situacion,
                provincia_tomador=excluded.provincia_tomador,
                delegacion=excluded.delegacion,
                nombre_tomador=excluded.nombre_tomador,
                origen='ASISA_CSV',
                nota_origen=NULL
            """,
            (
                r["poliza"], r["cliente_codigo"], r["razon_social"], r["producto_base"],
                r["producto_codigo"], _fecha_a_texto(r["fecha_emision"]),
                _fecha_a_texto(r["fecha_efecto"]), _fecha_a_texto(r["fecha_baja"]),
                r["forma_pago"], r["situacion"], r["provincia_tomador"],
                r["delegacion"], r["nombre_tomador"],
            ),
        )
        filas_insertadas += 1
    conn.commit()
    return filas_insertadas


def _normalizar_valor_negocio(valor):
    """Convierte nulos y fechas de pandas a una representación comparable."""
    if valor is None or pd.isna(valor):
        return None
    if isinstance(valor, pd.Timestamp):
        return valor.date().isoformat()
    if hasattr(valor, "isoformat"):
        return valor.isoformat()
    return valor


def cargar_polizas_provisionales_eiac(
    conn: sqlite3.Connection, df: pd.DataFrame
) -> ResultadoCargaPolizasProvisionalesEiac:
    """Carga EIAC sin alterar una póliza oficial y clasifica cada resultado.

    Solo los campos de negocio participan en la comparación. Así, una
    relectura idéntica no se presenta como actualización por metadatos de
    importación ni por diferencias de representación de ``NaN``/``NaT``.
    """
    columnas = (
        "cliente_codigo", "razon_social", "producto_base", "producto_codigo",
        "fecha_emision", "fecha_efecto", "fecha_baja", "forma_pago", "situacion",
        "provincia_tomador", "delegacion", "nombre_tomador", "nota_origen",
    )
    nuevas = actualizadas = sin_cambios = ignoradas_oficiales = 0
    cur = conn.cursor()
    for _, r in df.iterrows():
        valores = tuple(_normalizar_valor_negocio(r[c]) for c in columnas)
        existente = cur.execute(
            f"SELECT origen, {', '.join(columnas)} FROM polizas WHERE poliza = ?", (r["poliza"],)
        ).fetchone()
        if existente is None:
            cur.execute(
                f"INSERT INTO polizas (poliza, {', '.join(columnas)}, origen) "
                f"VALUES (?, {', '.join('?' for _ in columnas)}, 'EIAC')",
                (r["poliza"], *valores),
            )
            nuevas += 1
        elif existente[0] != "EIAC":
            ignoradas_oficiales += 1
        elif tuple(_normalizar_valor_negocio(v) for v in existente[1:]) == valores:
            sin_cambios += 1
        else:
            cur.execute(
                f"UPDATE polizas SET {', '.join(f'{c} = ?' for c in columnas)} WHERE poliza = ?",
                (*valores, r["poliza"]),
            )
            actualizadas += 1
    conn.commit()
    total_provisionales = cur.execute(
        "SELECT COUNT(*) FROM polizas WHERE origen = 'EIAC'"
    ).fetchone()[0]
    return ResultadoCargaPolizasProvisionalesEiac(
        nuevas, actualizadas, sin_cambios, ignoradas_oficiales, total_provisionales
    )


def cargar_liquidacion(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    filas_insertadas = 0
    cur = conn.cursor()
    for _, r in df.iterrows():
        cur.execute(
            """
            INSERT OR IGNORE INTO liquidacion
                (poliza, razon_social, fecha_desde, fecha_hasta, prima_neta,
                 situacion_recibo, comision, comision_pct, indicador_comision,
                 accion, periodo_liquidacion, concepto_factura, es_extorno)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["poliza"], r["razon_social"], _fecha_a_texto(r["fecha_desde"]),
                _fecha_a_texto(r["fecha_hasta"]), r["prima_neta"], r["situacion_recibo"],
                r["comision"], r["comision_pct"], r["indicador_comision"], r["accion"],
                r["periodo_liquidacion"], r["concepto_factura"], int(r["es_extorno"]),
            ),
        )
        filas_insertadas += cur.rowcount
    conn.commit()
    return filas_insertadas


def cargar_eiac_polizas(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """Upsert por id_poliza (maestro TIREA), igual criterio que `cargar_polizas`."""
    filas_insertadas = 0
    cur = conn.cursor()
    for _, r in df.iterrows():
        cur.execute(
            """
            INSERT INTO eiac_polizas
                (id_poliza, cliente_codigo, numero_poliza, situacion_poliza,
                 clase_poliza, fecha_efecto_inicial, fecha_emision, descripcion_riesgo,
                 descripcion_ramo, codigo_entidad_interno, ramo_entidad,
                 fecha_anulacion, motivo_anulacion, prima_neta_poliza)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id_poliza) DO UPDATE SET
                cliente_codigo=excluded.cliente_codigo,
                numero_poliza=excluded.numero_poliza,
                situacion_poliza=excluded.situacion_poliza,
                clase_poliza=excluded.clase_poliza,
                fecha_efecto_inicial=excluded.fecha_efecto_inicial,
                fecha_emision=excluded.fecha_emision,
                descripcion_riesgo=excluded.descripcion_riesgo,
                descripcion_ramo=excluded.descripcion_ramo,
                codigo_entidad_interno=excluded.codigo_entidad_interno,
                ramo_entidad=excluded.ramo_entidad,
                fecha_anulacion=excluded.fecha_anulacion,
                motivo_anulacion=excluded.motivo_anulacion,
                prima_neta_poliza=COALESCE(excluded.prima_neta_poliza, eiac_polizas.prima_neta_poliza)
            """,
            (
                r["id_poliza"], r["cliente_codigo"], r["numero_poliza"], r["situacion_poliza"],
                r["clase_poliza"], _fecha_a_texto(r["fecha_efecto_inicial"]),
                _fecha_a_texto(r["fecha_emision"]), r["descripcion_riesgo"],
                r["descripcion_ramo"], r["codigo_entidad_interno"], r["ramo_entidad"],
                _fecha_a_texto(r["fecha_anulacion"]), r["motivo_anulacion"],
                r.get("prima_neta_poliza"),
            ),
        )
        filas_insertadas += 1
    conn.commit()
    return filas_insertadas


def cargar_eiac_recibos(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """Los recibos ya llegan deduplicados (CO > PE) DENTRO de un mismo
    fichero desde `ingestion.eiac_xml`, pero Sebastián sube ficheros EIAC
    en lotes separados a lo largo del tiempo (confirmado: los 8 ficheros
    reales tienen timestamps de hasta una semana de diferencia) — así que
    aquí hace falta la MISMA prioridad CO > PE también ENTRE lotes: un
    recibo que llegó "PE" en un fichero de hace 3 días y ahora aparece
    "CO" en el fichero de hoy debe actualizarse; pero un recibo que ya
    está "CO" nunca debe degradarse a "PE" por un reintento tardío de un
    fichero posterior (la cláusula WHERE del UPSERT es la que lo impide).
    """
    filas_actualizadas = 0
    cur = conn.cursor()
    for _, r in df.iterrows():
        cur.execute(
            """
            INSERT INTO eiac_recibos
                (id_poliza, prima_total, prima_neta, situacion_recibo,
                 fecha_efecto_inicial, fecha_emision, clase_forma_pago, pista_forma_pago,
                 ramo_entidad, descripcion_ramo, codigo_entidad_interno)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id_poliza, fecha_efecto_inicial) DO UPDATE SET
                prima_total=excluded.prima_total,
                prima_neta=excluded.prima_neta,
                situacion_recibo=excluded.situacion_recibo,
                fecha_emision=excluded.fecha_emision,
                clase_forma_pago=excluded.clase_forma_pago,
                pista_forma_pago=excluded.pista_forma_pago,
                ramo_entidad=excluded.ramo_entidad,
                descripcion_ramo=excluded.descripcion_ramo,
                codigo_entidad_interno=excluded.codigo_entidad_interno
            WHERE
                (CASE situacion_recibo WHEN 'CO' THEN 1 WHEN 'PE' THEN 0 ELSE -1 END)
                <= (CASE excluded.situacion_recibo WHEN 'CO' THEN 1 WHEN 'PE' THEN 0 ELSE -1 END)
            """,
            (
                r["id_poliza"], r["prima_total"], r["prima_neta"], r["situacion_recibo"],
                _fecha_a_texto(r["fecha_efecto_inicial"]), _fecha_a_texto(r.get("fecha_emision")),
                r["clase_forma_pago"], r["pista_forma_pago"], r.get("ramo_entidad"),
                r.get("descripcion_ramo"), r.get("codigo_entidad_interno"),
            ),
        )
        filas_actualizadas += cur.rowcount
    conn.commit()
    return filas_actualizadas


def cargar_eiac_polizas_riesgos(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """Upsert por (id_poliza, numero_orden) — mismo criterio "última versión
    conocida gana" que `cargar_eiac_polizas`. Ver
    `ingestion.eiac_xml.parsear_eiac_polizas_riesgos`."""
    filas_insertadas = 0
    cur = conn.cursor()
    for _, r in df.iterrows():
        cur.execute(
            """
            INSERT INTO eiac_polizas_riesgos
                (id_poliza, numero_orden, descripcion_riesgo, fecha_inicio, id_riesgo_eiac)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id_poliza, numero_orden) DO UPDATE SET
                descripcion_riesgo=excluded.descripcion_riesgo,
                fecha_inicio=excluded.fecha_inicio,
                id_riesgo_eiac=excluded.id_riesgo_eiac
            """,
            (
                r["id_poliza"], r["numero_orden"], r["descripcion_riesgo"],
                _fecha_a_texto(r["fecha_inicio"]), r["id_riesgo_eiac"],
            ),
        )
        filas_insertadas += 1
    conn.commit()
    return filas_insertadas


def cargar_factura_pdf(conn: sqlite3.Connection, facturas: list) -> int:
    filas_insertadas = 0
    cur = conn.cursor()
    for f in facturas:
        cur.execute(
            """
            INSERT OR IGNORE INTO factura_pdf
                (entidad_cif, entidad_nombre, numero_factura, fecha_factura, periodo,
                 rappel, total_liquidacion, total_factura, irpf, base_factura)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f.entidad_cif, f.entidad_nombre, f.numero_factura, f.fecha_factura,
                f.periodo, f.rappel, f.totales.get("total_liquidacion", 0.0),
                f.totales.get("total_factura", 0.0), f.totales.get("irpf", 0.0),
                f.totales.get("base_factura", 0.0),
            ),
        )
        filas_insertadas += cur.rowcount
    conn.commit()
    return filas_insertadas


def recalcular_resumen_mensual(conn: sqlite3.Connection, contrato: ContratoConfig) -> int:
    """Recalcula la tabla `resumen_mensual` a partir de las tablas brutas.

    Se llama cada vez que se importan datos nuevos. Guarda un snapshot por
    periodo (AAAA-MM de fecha_efecto) con la producción total y el nº de
    pólizas nuevas; si ya hay factura_pdf de ese mes, añade también el
    rappel y la comisión neta reales.
    """
    df_polizas = pd.read_sql(
        "SELECT * FROM polizas", conn, parse_dates=["fecha_efecto"]
    )
    df_facturacion = pd.read_sql("SELECT * FROM facturacion", conn)
    df_factura_pdf = pd.read_sql("SELECT * FROM factura_pdf", conn)

    df_produccion = construir_produccion_polizas(df_polizas, df_facturacion, contrato)
    if df_produccion.empty:
        return 0

    cur = conn.cursor()
    filas = 0
    for periodo, grupo in df_produccion.groupby("periodo"):
        polizas_nuevas = len(grupo)
        produccion_total = round(float(grupo["prima_anual"].sum()), 2)

        facturas_periodo = df_factura_pdf[df_factura_pdf["periodo"] == periodo]
        if facturas_periodo.empty:
            rappel_real = None
            comision_neta_real = None
        else:
            rappel_real = round(float(facturas_periodo["rappel"].sum()), 2)
            comision_neta_real = round(
                float(facturas_periodo["total_factura"].sum()) - rappel_real, 2
            )

        cur.execute(
            """
            INSERT INTO resumen_mensual
                (periodo, produccion_total, polizas_nuevas, rappel_real, comision_neta_real)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(periodo) DO UPDATE SET
                produccion_total=excluded.produccion_total,
                polizas_nuevas=excluded.polizas_nuevas,
                rappel_real=excluded.rappel_real,
                comision_neta_real=excluded.comision_neta_real,
                fecha_actualizacion=CURRENT_TIMESTAMP
            """,
            (periodo, produccion_total, polizas_nuevas, rappel_real, comision_neta_real),
        )
        filas += 1
    conn.commit()
    return filas
