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
    -- 'ASISA_CSV' (por defecto) o 'EIAC': una fila 'EIAC' es PROVISIONAL,
    -- creada solo porque todavía no había llegado el CSV oficial de esa
    -- póliza — ver engine.eiac_integracion. `cargar_polizas` (CSV oficial)
    -- siempre pone origen='ASISA_CSV' al upsertar, incluso si ya existía
    -- como provisional, para que el CSV oficial "confirme" la fila.
    origen TEXT DEFAULT 'ASISA_CSV',
    nota_origen TEXT,
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

-- Canal EIAC (estándar TIREA, ficheros XML "EIAC-ENV-POLI-*" /
-- "EIAC-ENV-RECI-*"). Tablas propias, separadas de polizas/facturacion a
-- propósito: `id_poliza` aquí usa el espacio de numeración TIREA
-- ("codigo_cliente-numero_poliza"), que NO es el mismo que la columna
-- "POLIZA" de los CSV de ASISA — cruzarlos por igualdad rompería en
-- silencio el motor de rappel/comisiones. Ver docstring de
-- `ingestion.eiac_xml` para el detalle completo de esta decisión.
CREATE TABLE IF NOT EXISTS eiac_polizas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    id_poliza TEXT NOT NULL,
    cliente_codigo TEXT,
    numero_poliza TEXT,
    situacion_poliza TEXT,
    clase_poliza TEXT,
    fecha_efecto_inicial TEXT,
    fecha_emision TEXT,
    descripcion_riesgo TEXT,
    descripcion_ramo TEXT,            -- p.ej. "Asistencia sanitaria" -- señal de Salud/Vida
    codigo_entidad_interno TEXT,      -- p.ej. "Asisa" -- segunda señal de Salud/Vida
    fecha_import TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(id_poliza)
);

CREATE TABLE IF NOT EXISTS eiac_recibos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    id_poliza TEXT NOT NULL,
    prima_total REAL,
    prima_neta REAL,
    situacion_recibo TEXT,           -- ya deduplicado por ingestion.eiac_xml (CO > PE)
    fecha_efecto_inicial TEXT,
    clase_forma_pago TEXT,
    pista_forma_pago TEXT,           -- heurística, no un hecho confirmado
    -- UNIQUE por (id_poliza, fecha_efecto_inicial), NO por prima_total: en
    -- los ficheros reales el importe fluctúa unos céntimos entre intentos
    -- del MISMO recibo (recálculos de recargos), así que no es parte de
    -- la identidad del recibo. db.carga.cargar_eiac_recibos hace upsert
    -- respetando la prioridad CO > PE también entre ficheros subidos en
    -- momentos distintos (un PE posterior nunca degrada un CO ya guardado).
    UNIQUE(id_poliza, fecha_efecto_inicial)
);

-- Un asegurado por fila (una póliza familiar puede tener varios). El
-- NumeroOrden=1 ya se guarda como `descripcion_riesgo` en `eiac_polizas`
-- para lo que ya usa la UI; esta tabla es el detalle completo, para no
-- perder al resto de asegurados. Ver `ingestion.eiac_xml.
-- parsear_eiac_polizas_riesgos`.
CREATE TABLE IF NOT EXISTS eiac_polizas_riesgos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    id_poliza TEXT NOT NULL,
    numero_orden TEXT,
    descripcion_riesgo TEXT,
    fecha_inicio TEXT,
    id_riesgo_eiac TEXT,
    UNIQUE(id_poliza, numero_orden)
);
"""


def conectar(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: Streamlit ejecuta la subida de ficheros y los
    # reruns en hilos distintos al que crea la conexión cacheada; sqlite3 lo
    # bloquea por defecto aunque en nuestro caso (una sola persona, escrituras
    # secuenciales) es seguro desactivarlo.
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON")
    # busy_timeout: si otra conexión (p.ej. dos pestañas del dashboard
    # abiertas a la vez, o dos lanzamientos del icono de escritorio sin
    # cerrar el anterior) tiene la BD bloqueada un instante, espera hasta
    # 10s reintentando en vez de fallar al momento con "database is
    # locked" — el `timeout` del connect() de arriba cubre la apertura,
    # este PRAGMA cubre cada sentencia SQL individual después de conectar.
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _asegurar_columna(conn: sqlite3.Connection, tabla: str, columna: str, definicion: str) -> None:
    """Añade `columna` a `tabla` si no existe todavía.

    `CREATE TABLE IF NOT EXISTS` no modifica una tabla ya existente en una
    BD antigua (p.ej. `data/asisa.db` de antes de que existieran
    `origen`/`nota_origen`) — hace falta un ALTER TABLE explícito para que
    las bases de datos ya creadas se pongan al día.
    """
    columnas_actuales = {fila[1] for fila in conn.execute(f"PRAGMA table_info({tabla})")}
    if columna not in columnas_actuales:
        conn.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}")


def _migrar_eiac_recibos_unique(conn: sqlite3.Connection) -> None:
    """`eiac_recibos` cambió su UNIQUE de (id_poliza, prima_total,
    fecha_efecto_inicial) a (id_poliza, fecha_efecto_inicial) — ver
    comentario en `SCHEMA_SQL`. `CREATE TABLE IF NOT EXISTS` no toca una
    tabla que ya existe con la restricción antigua, así que si la tabla
    está vacía (no hay pérdida posible) se recrea; si ya tiene filas con
    el esquema antiguo, se deja tal cual (caso no esperado en un proyecto
    de un único usuario, pero mejor no borrar datos en silencio).
    """
    fila = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='eiac_recibos'"
    ).fetchone()
    if fila is None or fila[0] is None:
        return
    if "UNIQUE(id_poliza, fecha_efecto_inicial)" in fila[0]:
        return
    total = conn.execute("SELECT COUNT(*) FROM eiac_recibos").fetchone()[0]
    if total == 0:
        conn.execute("DROP TABLE eiac_recibos")


def inicializar_schema(conn: sqlite3.Connection) -> None:
    _migrar_eiac_recibos_unique(conn)
    conn.executescript(SCHEMA_SQL)
    _asegurar_columna(conn, "polizas", "origen", "TEXT DEFAULT 'ASISA_CSV'")
    _asegurar_columna(conn, "polizas", "nota_origen", "TEXT")
    _asegurar_columna(conn, "eiac_polizas", "descripcion_ramo", "TEXT")
    _asegurar_columna(conn, "eiac_polizas", "codigo_entidad_interno", "TEXT")
    conn.commit()
