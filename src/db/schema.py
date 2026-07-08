"""Esquema SQLite del proyecto.

Diseño: cada tabla de "hechos brutos" (facturacion, polizas, liquidacion,
factura_pdf) guarda exactamente lo que viene del fichero de origen, con una
clave de import para poder re-importar sin duplicar. Las tablas derivadas
(comisiones_normalizadas, rappel_mensual) las calcula el motor y se pueden
regenerar en cualquier momento a partir de las brutas.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS facturacion (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poliza TEXT NOT NULL,
    cliente_codigo TEXT,
    cartera TEXT,
    producto_nombre TEXT,
    fecha_desde TEXT,
    fecha_hasta TEXT,
    prima_neta REAL,
    prima_total REAL,
    periodo_liquidacion TEXT,
    duracion_recibo_meses REAL,
    UNIQUE(poliza, fecha_desde, fecha_hasta, periodo_liquidacion)
);

CREATE TABLE IF NOT EXISTS polizas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poliza TEXT NOT NULL,
    cliente_codigo TEXT,
    razon_social TEXT,
    producto_base TEXT,
    producto_codigo TEXT,
    fecha_emision TEXT,
    fecha_efecto TEXT,
    fecha_baja TEXT,
    forma_pago TEXT,
    situacion TEXT,
    provincia_tomador TEXT,
    delegacion TEXT,
    nombre_tomador TEXT,
    fecha_import TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(poliza)
);

CREATE TABLE IF NOT EXISTS liquidacion (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poliza TEXT NOT NULL,
    razon_social TEXT,
    fecha_desde TEXT,
    fecha_hasta TEXT,
    prima_neta REAL,
    situacion_recibo TEXT,
    comision REAL,
    comision_pct REAL,
    indicador_comision TEXT,
    accion TEXT,
    periodo_liquidacion TEXT,
    concepto_factura TEXT,
    es_extorno INTEGER,
    UNIQUE(poliza, fecha_desde, comision, accion, periodo_liquidacion)
);

CREATE TABLE IF NOT EXISTS factura_pdf (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entidad_cif TEXT,
    entidad_nombre TEXT,
    numero_factura TEXT UNIQUE,
    fecha_factura TEXT,
    periodo TEXT,
    rappel REAL,
    total_liquidacion REAL,
    total_factura REAL,
    irpf REAL,
    base_factura REAL
);

-- Vista/tabla derivada: alertas de pólizas que deberían haberse cobrado
-- y no aparecen en liquidacion dentro del margen configurado.
CREATE TABLE IF NOT EXISTS alertas_reconciliacion (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poliza TEXT NOT NULL,
    tipo TEXT,               -- 'sin_cobrar_esperado', 'diferencia_estimado_vs_real'
    detalle TEXT,
    fecha_deteccion TEXT DEFAULT CURRENT_TIMESTAMP,
    resuelta INTEGER DEFAULT 0,
    resolucion_nota TEXT
);

-- Snapshot mensual derivado, recalculado cada vez que se importan datos
-- nuevos (ver db/carga.recalcular_resumen_mensual). Evita tener que
-- recalcular todo el histórico desde cero cada vez que se abre el
-- dashboard, y es la base sobre la que crecen los insights históricos
-- a medida que se sube más histórico.
CREATE TABLE IF NOT EXISTS resumen_mensual (
    periodo TEXT PRIMARY KEY,        -- "AAAA-MM", según fecha_efecto
    produccion_total REAL,           -- prima anualizada de pólizas nuevas del mes
    polizas_nuevas INTEGER,
    rappel_real REAL,                -- NULL si el mes aún no tiene factura_pdf
    comision_neta_real REAL,         -- NULL si el mes aún no tiene factura_pdf
    fecha_actualizacion TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def conectar(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: Streamlit ejecuta la subida de ficheros y los
    # reruns en hilos distintos al que crea la conexión cacheada; sqlite3 lo
    # bloquea por defecto aunque en nuestro caso (una sola persona, escrituras
    # secuenciales) es seguro desactivarlo.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def inicializar_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()
