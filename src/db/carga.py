"""Carga de DataFrames normalizados a SQLite, sin duplicar filas ya importadas."""

from __future__ import annotations

import sqlite3

import pandas as pd

from engine.config_contrato import ContratoConfig
from engine.insights import construir_produccion_polizas


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
    """Upsert por póliza: siempre nos quedamos con el dato más reciente conocido."""
    filas_insertadas = 0
    cur = conn.cursor()
    for _, r in df.iterrows():
        cur.execute(
            """
            INSERT INTO polizas
                (poliza, cliente_codigo, razon_social, producto_base, producto_codigo,
                 fecha_emision, fecha_efecto, fecha_baja, forma_pago, situacion,
                 provincia_tomador, delegacion, nombre_tomador)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                nombre_tomador=excluded.nombre_tomador
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
