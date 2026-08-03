"""Pruebas de humo del dashboard completo con datos PARCIALES.

Reproducen el bug real reportado: subir solo Facturación (sin Pólizas
todavía) hacía reventar la pestaña Pólizas con StreamlitAPIException
porque un multiselect tenía default=["A"] fijo. Estas pruebas ejecutan
el script de Streamlit entero (las 5 pestañas se renderizan en la misma
pasada) contra bases de datos con un único fichero cargado, y confirman
que ninguna combinación revienta — a lo sumo debe mostrar menos
información de la ideal, nunca una excepción.
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from db.carga import (
    cargar_eiac_polizas,
    cargar_eiac_recibos,
    cargar_facturacion,
    cargar_factura_pdf,
    cargar_liquidacion,
    cargar_polizas,
)
from db.schema import inicializar_schema
from ingestion.eiac_xml import parsear_eiac_polizas, parsear_eiac_recibos
from ingestion.facturacion import parsear_facturacion
from ingestion.liquidacion import parsear_liquidacion
from ingestion.polizas import parsear_polizas


def _valor_a_float(texto: str) -> float:
    return float(texto.replace(" €", "").replace(",", ""))

APP_PATH = Path(__file__).parent.parent / "src" / "dashboard" / "app.py"
FIXTURES = Path(__file__).parent / "fixtures"


class _FacturaPdfFalsa:
    """Sustituto mínimo de FacturaEntidad para no depender de un PDF real."""

    def __init__(
        self,
        entidad_cif="A08169294",
        entidad_nombre="ASISA",
        numero_factura="F-2026-06",
        periodo="2026-06",
        rappel=900.0,
        total_factura=1500.0,
        irpf=-100.0,
        base_factura=1600.0,
    ):
        self.entidad_cif = entidad_cif
        self.entidad_nombre = entidad_nombre
        self.numero_factura = numero_factura
        self.fecha_factura = "2026-07-01"
        self.periodo = periodo
        self.rappel = rappel
        self.totales = {
            "total_liquidacion": 1000.0,
            "total_factura": total_factura,
            # OJO: en las facturas PDF reales, irpf se guarda en NEGATIVO
            # (base_factura + irpf = total_factura) — ver engine/fiscal.py.
            "irpf": irpf,
            "base_factura": base_factura,
        }


def _sumar_meses(anio: int, mes: int, n: int) -> tuple[int, int]:
    total = anio * 12 + (mes - 1) + n
    return total // 12, total % 12 + 1


def _eiac_polizas_xml(id_poliza: str, fecha_efecto_iso: str) -> str:
    # Estructura real confirmada (ver ingestion.eiac_xml): IdPoliza vive en
    # DatosPoliza, las fechas en Fechas -- sin bloques de datos personales,
    # que el parser no lee.
    return f"""<?xml version="1.0" encoding="ISO-8859-15"?>
<ProcesosEIAC xmlns="http://www.tirea.es/EIAC/ProcesosEIAC">
  <Objetos>
    <Poliza>
      <SituacionPoliza>EV</SituacionPoliza>
      <ClasePoliza>NP</ClasePoliza>
      <DatosPoliza>
        <IdPoliza>{id_poliza}</IdPoliza>
      </DatosPoliza>
      <Fechas>
        <FechaEfectoInicial>{fecha_efecto_iso}T00:00:00</FechaEfectoInicial>
      </Fechas>
    </Poliza>
  </Objetos>
</ProcesosEIAC>
"""


def _eiac_recibos_xml(id_poliza: str, fecha_efecto_iso: str, prima_total: str, prima_neta: str) -> str:
    return f"""<?xml version="1.0" encoding="ISO-8859-15"?>
<ProcesosEIAC xmlns="http://www.tirea.es/EIAC/ProcesosEIAC">
  <Objetos>
    <Recibo>
      <DatosPoliza>
        <IdPoliza>{id_poliza}</IdPoliza>
      </DatosPoliza>
      <DatosRecibo>
        <SituacionRecibo>CO</SituacionRecibo>
        <Fechas>
          <FechaEfectoInicial>{fecha_efecto_iso}T00:00:00</FechaEfectoInicial>
        </Fechas>
        <GestionCobro>
          <DatosFormaPago>
            <ClaseFormaPago>CC</ClaseFormaPago>
          </DatosFormaPago>
        </GestionCobro>
        <DatosImportes>
          <Importes>
            <PrimaTotal>{prima_total}</PrimaTotal>
            <PrimaNeta>{prima_neta}</PrimaNeta>
          </Importes>
        </DatosImportes>
      </DatosRecibo>
    </Recibo>
  </Objetos>
</ProcesosEIAC>
"""


def _nueva_db(tmp_path, nombre) -> Path:
    db_path = tmp_path / nombre
    conn = sqlite3.connect(db_path)
    inicializar_schema(conn)
    return db_path, conn


def _correr_app(db_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DB_PATH", str(db_path))
    # st.cache_resource/st.cache_data son cachés GLOBALES de proceso,
    # indexadas por el código fuente de la función cacheada, no por sus
    # argumentos ni por qué AppTest las ejecutó. get_conn()/cargar_datos()
    # no reciben la ruta de BD como argumento (leen el global DB_PATH), así
    # que sin este clear() un test posterior heredaría la conexión/los
    # DataFrames del test anterior aunque apunte a una BD distinta.
    st.cache_resource.clear()
    st.cache_data.clear()
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    return at


def test_dashboard_no_revienta_con_solo_facturacion(tmp_path, monkeypatch):
    # El escenario real que reportó el bug: Facturación subida, Pólizas no.
    db_path, conn = _nueva_db(tmp_path, "solo_facturacion.db")
    cargar_facturacion(conn, parsear_facturacion(FIXTURES / "facturacion_sample.csv"))
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []


def test_dashboard_no_revienta_con_solo_polizas(tmp_path, monkeypatch):
    db_path, conn = _nueva_db(tmp_path, "solo_polizas.db")
    cargar_polizas(conn, parsear_polizas(FIXTURES / "polizas_sample.csv"))
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []


def test_dashboard_no_revienta_con_solo_liquidacion(tmp_path, monkeypatch):
    db_path, conn = _nueva_db(tmp_path, "solo_liquidacion.db")
    cargar_liquidacion(conn, parsear_liquidacion(FIXTURES / "liquidacion_sample.csv"))
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []


def test_dashboard_no_revienta_con_solo_factura_pdf(tmp_path, monkeypatch):
    db_path, conn = _nueva_db(tmp_path, "solo_factura_pdf.db")
    cargar_factura_pdf(conn, [_FacturaPdfFalsa()])
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []


def test_rappel_cuenta_poliza_de_fin_de_mes_en_su_periodo_real(tmp_path, monkeypatch):
    """Caso real confirmado: pólizas con fecha_efecto a fin de mes cuyo
    PRIMER recibo de Facturación (el que fija el periodo real de devengo
    16->15 de ASISA) cae en el mes SIGUIENTE al calendario de fecha_efecto.
    La pestaña Rappel debe contarlas en el mes real (periodo_liquidacion),
    no en el mes calendario de fecha_efecto — igual que las pólizas reales
    64201679/64174100 (fecha_efecto 30/06/2026, periodo_liquidacion
    "2026-07") que motivaron este fix.

    Las fechas se generan relativas a `date.today()` (no hardcodeadas a
    2026) para que esta prueba no caduque cuando pase julio de 2026: la
    pestaña Rappel filtra por el mes calendario de HOY, así que el caso de
    prueba debe construirse siempre alrededor de "hoy", sea cuando sea.
    """
    hoy = date.today()
    periodo_actual = f"{hoy.year:04d}-{hoy.month:02d}"
    if hoy.month == 1:
        anio_ant, mes_ant = hoy.year - 1, 12
    else:
        anio_ant, mes_ant = hoy.year, hoy.month - 1
    ultimo_dia_mes_ant = calendar.monthrange(anio_ant, mes_ant)[1]
    fecha_efecto_fin_mes_ant = f"{ultimo_dia_mes_ant:02d}/{mes_ant:02d}/{anio_ant:04d}"

    polizas_csv = tmp_path / "polizas_fin_de_mes.csv"
    polizas_csv.write_text(
        "AGENTE;ORDEN NIF;NOMBRE AGENTE;CLIENTE;RAZON SOCIAL;POLIZA;ORDEN;PRODUCTO BASE;"
        "PRODUCTO;FECHA GRAB;FECHA ALTA;FECHA BAJA;FORMA PAGO;SITUACION POLIZA;"
        "INDICADOR DE FACTURACION;NIF TOMADOR;NOMBRE TOMADOR;PRIMER APELLIDO TOMADOR;"
        "SEGUNDO APELLIDO TOMADOR;DIRECCION TOMADOR;C  POSTAL TOMADOR;POBLACION TOMADOR;"
        "PROVINCIA TOMADOR;TELEFONO TOMADOR;F  NACIMIENTO TOMADOR;NIF ASEGURADO;"
        "NOMBRE ASEGURADO;PRIMER APELLIDO ASEGURADO;SEGUNDO APELLIDO ASEGURADO;"
        "DIRECCION ASEGURADO;C  POSTAL ASEGURADO;POBLACION ASEGURADO;PROVINCIA ASEGURADO;"
        "TELEFONO ASEGURADO;F  NACIMIENTOASEGURADO;DELEGACION;DESCRIPCION;PER  LIQUIDACION;"
        "SUBAGENTE\n"
        f"00000000X;0;AGENTE PRUEBA;90099;ASISA PARTICULARES;64201679;0;"
        f"ASISTENCIA SANITARIA;101049;{fecha_efecto_fin_mes_ant};{fecha_efecto_fin_mes_ant};"
        f"01/01/1900;M;A;S;X0000099A;NOMBRE;APELLIDO1;APELLIDO2;Calle Real 1;28000;MADRID;"
        f"Madrid;+34600000099;01/01/1990;X0000099A;NOMBRE;APELLIDO1;APELLIDO2;Calle Real 1;"
        f"28000;MADRID;Madrid;+34600000099;01/01/1990;2800;MADRID;{mes_ant:04d};\n",
        encoding="utf-8",
    )

    facturacion_csv = tmp_path / "facturacion_fin_de_mes.csv"
    facturacion_csv.write_text(
        "CLIENTE;CARTERA;OPERACION;POLIZA;NOMBRE CLIENTE;FECHA DESDE;FECHA HASTA;"
        "PRIMA NETA;PRIMA TOTAL;PER. LIQUIDACION\n"
        f"90099;ASISTENCIA SANITARIA;CARTERA;64201679;ASISA PARTICULARES;"
        f"{fecha_efecto_fin_mes_ant};{fecha_efecto_fin_mes_ant};40,00;40,10;{periodo_actual}\n",
        encoding="utf-8",
    )

    db_path, conn = _nueva_db(tmp_path, "fin_de_mes.db")
    cargar_polizas(conn, parsear_polizas(polizas_csv))
    cargar_facturacion(conn, parsear_facturacion(facturacion_csv))
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []

    metricas = {m.label: m.value for m in at.metric}
    # 40€ de prima mensual anualizada = 480€ — debe aparecer como
    # producción del mes EN CURSO (periodo_liquidacion), aunque fecha_efecto
    # caiga en el mes calendario anterior.
    assert metricas["Producción nueva Salud"] == "480.00 €"


def test_vista_rapida_avisa_si_mes_siguiente_no_tiene_datos_y_suma_bien_el_actual(
    tmp_path, monkeypatch
):
    """Bloque "📅 Vista rápida" visible antes de las pestañas: el mes actual
    (con datos) debe sumar la producción real; el mes siguiente (sin
    ningún recibo todavía) debe mostrar el aviso de "todavía no hay
    datos", nunca un 0€ que parezca "no has vendido nada". Fechas
    generadas relativas a date.today() para que no caduque con el tiempo.
    """
    hoy = date.today()
    periodo_actual = f"{hoy.year:04d}-{hoy.month:02d}"
    if hoy.month == 12:
        periodo_siguiente = f"{hoy.year + 1:04d}-01"
    else:
        periodo_siguiente = f"{hoy.year:04d}-{hoy.month + 1:02d}"
    fecha_efecto_mes_actual = f"01/{hoy.month:02d}/{hoy.year:04d}"

    polizas_csv = tmp_path / "polizas_vista_rapida.csv"
    polizas_csv.write_text(
        "AGENTE;ORDEN NIF;NOMBRE AGENTE;CLIENTE;RAZON SOCIAL;POLIZA;ORDEN;PRODUCTO BASE;"
        "PRODUCTO;FECHA GRAB;FECHA ALTA;FECHA BAJA;FORMA PAGO;SITUACION POLIZA;"
        "INDICADOR DE FACTURACION;NIF TOMADOR;NOMBRE TOMADOR;PRIMER APELLIDO TOMADOR;"
        "SEGUNDO APELLIDO TOMADOR;DIRECCION TOMADOR;C  POSTAL TOMADOR;POBLACION TOMADOR;"
        "PROVINCIA TOMADOR;TELEFONO TOMADOR;F  NACIMIENTO TOMADOR;NIF ASEGURADO;"
        "NOMBRE ASEGURADO;PRIMER APELLIDO ASEGURADO;SEGUNDO APELLIDO ASEGURADO;"
        "DIRECCION ASEGURADO;C  POSTAL ASEGURADO;POBLACION ASEGURADO;PROVINCIA ASEGURADO;"
        "TELEFONO ASEGURADO;F  NACIMIENTOASEGURADO;DELEGACION;DESCRIPCION;PER  LIQUIDACION;"
        "SUBAGENTE\n"
        f"00000000X;0;AGENTE PRUEBA;90200;ASISA PARTICULARES;64300001;0;"
        f"ASISTENCIA SANITARIA;101049;{fecha_efecto_mes_actual};{fecha_efecto_mes_actual};"
        f"01/01/1900;M;A;S;X0000200A;NOMBRE;APELLIDO1;APELLIDO2;Calle Real 1;28000;MADRID;"
        f"Madrid;+34600000200;01/01/1990;X0000200A;NOMBRE;APELLIDO1;APELLIDO2;Calle Real 1;"
        f"28000;MADRID;Madrid;+34600000200;01/01/1990;2800;MADRID;{periodo_actual};\n",
        encoding="utf-8",
    )
    facturacion_csv = tmp_path / "facturacion_vista_rapida.csv"
    facturacion_csv.write_text(
        "CLIENTE;CARTERA;OPERACION;POLIZA;NOMBRE CLIENTE;FECHA DESDE;FECHA HASTA;"
        "PRIMA NETA;PRIMA TOTAL;PER. LIQUIDACION\n"
        f"90200;ASISTENCIA SANITARIA;CARTERA;64300001;ASISA PARTICULARES;"
        f"{fecha_efecto_mes_actual};{fecha_efecto_mes_actual};40,00;40,10;{periodo_actual}\n",
        encoding="utf-8",
    )

    db_path, conn = _nueva_db(tmp_path, "vista_rapida.db")
    cargar_polizas(conn, parsear_polizas(polizas_csv))
    cargar_facturacion(conn, parsear_facturacion(facturacion_csv))
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []

    # Mes siguiente: sin ningún dato -> aviso, NUNCA un 0€ desnudo.
    assert any(
        f"Todavía no hay datos de {periodo_siguiente}" in i.value for i in at.info
    )

    # Mes actual: sí hay datos -> suma correctamente (40€ x12 = 480€).
    metricas = {m.label: m.value for m in at.metric}
    assert metricas["Producción nueva Salud"] == "480.00 €"
    # Este caso solo tiene Salud: Vista rápida debe seguir renderizando la
    # métrica Vida con cero y sin AttributeError en el objeto del motor.
    assert metricas["Producción nueva Vida"] == "0.00 €"


def test_resumen_no_mezcla_rappel_de_salud_y_vida(tmp_path, monkeypatch):
    """Caso real confirmado (junio 2026): ASISA Salud rappel=1.200€,
    factura=2.082,89€; ASISA Vida rappel=0€ (nunca aplica), factura=109,45€,
    mismas periodo "2026-06". Antes del fix, el Resumen mostraba
    df_factura_pdf.sort_values("periodo").iloc[-1]: con ambas filas
    empatadas en periodo, el desempate dependía del orden de inserción y
    podía coger la fila de Vida donde tocaba mostrar la de Salud (o al
    revés) — "a veces sale bien, a veces mal". Ahora deben aparecer
    siempre las dos, con su nombre, sin mezclarse.
    """
    db_path, conn = _nueva_db(tmp_path, "resumen_salud_y_vida.db")
    # Hace falta al menos Facturación o Pólizas para pasar la pantalla de
    # "todavía no hay datos cargados" y llegar a renderizar el Resumen.
    cargar_facturacion(conn, parsear_facturacion(FIXTURES / "facturacion_sample.csv"))
    cargar_factura_pdf(
        conn,
        [
            _FacturaPdfFalsa(
                entidad_cif="A08169294",  # ASISA salud
                numero_factura="F-SALUD-2026-06",
                periodo="2026-06",
                rappel=1200.0,
                total_factura=2082.89,
            ),
            _FacturaPdfFalsa(
                entidad_cif="A87425070",  # ASISA VIDA
                numero_factura="F-VIDA-2026-06",
                periodo="2026-06",
                rappel=0.0,
                total_factura=109.45,
            ),
        ],
    )
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []

    metricas = {m.label: m.value for m in at.metric}
    etiqueta_salud = next(k for k in metricas if "ASISA, Asistencia" in k)
    etiqueta_vida = next(k for k in metricas if "ASISA VIDA" in k)

    assert metricas[etiqueta_salud] == "2082.89 €"
    assert metricas[etiqueta_vida] == "109.45 €"
    # Los helps de los metric() llevan el rappel de cada entidad — Vida
    # nunca debe aparecer con el rappel de Salud ni viceversa.
    ayudas = {m.label: m.help for m in at.metric}
    assert "1200.00" in ayudas[etiqueta_salud]
    assert "nunca tiene rappel" in ayudas[etiqueta_vida]


def test_dashboard_no_revienta_con_polizas_y_liquidacion_sin_facturacion(tmp_path, monkeypatch):
    # Pólizas + Liquidación pero SIN Facturación del mes: combinación real
    # (p.ej. Sebastián sube Liquidación mensual antes que la Facturación
    # semanal del mismo periodo).
    db_path, conn = _nueva_db(tmp_path, "polizas_liquidacion_sin_facturacion.db")
    cargar_polizas(conn, parsear_polizas(FIXTURES / "polizas_sample.csv"))
    cargar_liquidacion(conn, parsear_liquidacion(FIXTURES / "liquidacion_sample.csv"))
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []


def test_tab_irpf_suma_retenciones_de_2_meses_y_2_entidades(tmp_path, monkeypatch):
    db_path, conn = _nueva_db(tmp_path, "irpf_2_meses_2_entidades.db")
    cargar_facturacion(conn, parsear_facturacion(FIXTURES / "facturacion_sample.csv"))
    cargar_factura_pdf(
        conn,
        [
            _FacturaPdfFalsa(
                entidad_cif="A08169294", numero_factura="F-S-05", periodo="2026-05",
                irpf=-310.94, base_factura=2072.94, total_factura=1762.00,
            ),
            _FacturaPdfFalsa(
                entidad_cif="A87425070", numero_factura="F-V-05", periodo="2026-05",
                irpf=-1.07, base_factura=7.13, total_factura=6.06,
            ),
            _FacturaPdfFalsa(
                entidad_cif="A08169294", numero_factura="F-S-06", periodo="2026-06",
                irpf=-367.57, base_factura=2450.46, total_factura=2082.89,
            ),
            _FacturaPdfFalsa(
                entidad_cif="A87425070", numero_factura="F-V-06", periodo="2026-06",
                irpf=-19.32, base_factura=128.77, total_factura=109.45,
            ),
        ],
    )
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []

    metricas = {m.label: m.value for m in at.metric}
    etiqueta_total = next(k for k in metricas if k.startswith("Total retenido en"))
    assert metricas[etiqueta_total] == "698.90 €"  # 310.94+1.07+367.57+19.32


def test_vista_rapida_muestra_comision_bruta_bruto_y_neto(tmp_path, monkeypatch):
    """Tarea 1: además de Producción/Rappel, Vista rápida (y Rappel) deben
    mostrar Comisión bruta estimada, Total bruto y Total NETO — reutilizando
    estimar_comision_y_rappel_periodo, no una fórmula aparte. No se
    hardcodea el rappel esperado (depende del tramo, que avanza con el
    tiempo real desde el inicio de contrato) — sí se comprueba la relación
    aritmética entre las 3 cifras nuevas.
    """
    hoy = date.today()
    periodo_actual = f"{hoy.year:04d}-{hoy.month:02d}"
    fecha_efecto_mes_actual = f"01/{hoy.month:02d}/{hoy.year:04d}"

    polizas_csv = tmp_path / "polizas_comision.csv"
    polizas_csv.write_text(
        "AGENTE;ORDEN NIF;NOMBRE AGENTE;CLIENTE;RAZON SOCIAL;POLIZA;ORDEN;PRODUCTO BASE;"
        "PRODUCTO;FECHA GRAB;FECHA ALTA;FECHA BAJA;FORMA PAGO;SITUACION POLIZA;"
        "INDICADOR DE FACTURACION;NIF TOMADOR;NOMBRE TOMADOR;PRIMER APELLIDO TOMADOR;"
        "SEGUNDO APELLIDO TOMADOR;DIRECCION TOMADOR;C  POSTAL TOMADOR;POBLACION TOMADOR;"
        "PROVINCIA TOMADOR;TELEFONO TOMADOR;F  NACIMIENTO TOMADOR;NIF ASEGURADO;"
        "NOMBRE ASEGURADO;PRIMER APELLIDO ASEGURADO;SEGUNDO APELLIDO ASEGURADO;"
        "DIRECCION ASEGURADO;C  POSTAL ASEGURADO;POBLACION ASEGURADO;PROVINCIA ASEGURADO;"
        "TELEFONO ASEGURADO;F  NACIMIENTOASEGURADO;DELEGACION;DESCRIPCION;PER  LIQUIDACION;"
        "SUBAGENTE\n"
        f"00000000X;0;AGENTE PRUEBA;90300;ASISA PARTICULARES;64300002;0;"
        f"ASISTENCIA SANITARIA;101049;{fecha_efecto_mes_actual};{fecha_efecto_mes_actual};"
        f"01/01/1900;M;A;S;X0000300A;NOMBRE;APELLIDO1;APELLIDO2;Calle Real 1;28000;MADRID;"
        f"Madrid;+34600000300;01/01/1990;X0000300A;NOMBRE;APELLIDO1;APELLIDO2;Calle Real 1;"
        f"28000;MADRID;Madrid;+34600000300;01/01/1990;2800;MADRID;{periodo_actual};\n",
        encoding="utf-8",
    )
    facturacion_csv = tmp_path / "facturacion_comision.csv"
    facturacion_csv.write_text(
        "CLIENTE;CARTERA;OPERACION;POLIZA;NOMBRE CLIENTE;FECHA DESDE;FECHA HASTA;"
        "PRIMA NETA;PRIMA TOTAL;PER. LIQUIDACION\n"
        f"90300;ASISTENCIA SANITARIA;CARTERA;64300002;ASISA PARTICULARES;"
        f"{fecha_efecto_mes_actual};{fecha_efecto_mes_actual};40,00;40,10;{periodo_actual}\n",
        encoding="utf-8",
    )

    db_path, conn = _nueva_db(tmp_path, "comision_bruta.db")
    cargar_polizas(conn, parsear_polizas(polizas_csv))
    cargar_facturacion(conn, parsear_facturacion(facturacion_csv))
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []

    metricas = {m.label: m.value for m in at.metric}
    assert _valor_a_float(metricas["Producción nueva Salud"]) == pytest.approx(480.0)

    comision_salud = _valor_a_float(metricas["Comisión Salud estimada"])
    comision_vida = _valor_a_float(metricas["Comisión Vida estimada"])
    rappel = _valor_a_float(metricas["Rappel estimado"])
    total_bruto = _valor_a_float(metricas["Total bruto (comisión + rappel)"])
    total_neto = _valor_a_float(metricas["💰 Total NETO estimado"])

    # ASISA PARTICULARES, primer año: 25% de producción (config/contrato.yaml).
    assert comision_salud == pytest.approx(480.0 * 0.25)
    assert comision_vida == pytest.approx(0.0)
    comision_bruta = comision_salud + comision_vida
    assert total_bruto == pytest.approx(round(comision_bruta + rappel, 2))
    # Retención IRPF actual del YAML: 15%.
    assert total_neto == pytest.approx(round(total_bruto * 0.85, 2))


def test_produccion_confirmada_meses_futuros_aparece_con_datos_eiac(tmp_path, monkeypatch):
    """Tarea 3: un periodo más allá del mes siguiente con producción
    detectada (típicamente EIAC, adelantándose al CSV oficial) debe
    aparecer en un bloque "Producción confirmada — meses futuros" dentro
    de Vista rápida — caso real: póliza con efecto en septiembre.
    """
    hoy = date.today()
    anio_futuro, mes_futuro = _sumar_meses(hoy.year, hoy.month, 4)
    periodo_futuro = f"{anio_futuro:04d}-{mes_futuro:02d}"
    fecha_efecto_futura = f"{anio_futuro:04d}-{mes_futuro:02d}-01"

    eiac_polizas_xml = tmp_path / "EIAC-ENV-POLI-futuro.xml"
    eiac_polizas_xml.write_text(
        _eiac_polizas_xml("24300-70099001", fecha_efecto_futura), encoding="utf-8"
    )
    eiac_recibos_xml = tmp_path / "EIAC-ENV-RECI-futuro.xml"
    eiac_recibos_xml.write_text(
        _eiac_recibos_xml("24300-70099001", fecha_efecto_futura, "50.50", "50.00"), encoding="utf-8"
    )

    db_path, conn = _nueva_db(tmp_path, "meses_futuros.db")
    cargar_eiac_polizas(conn, parsear_eiac_polizas(eiac_polizas_xml))
    cargar_eiac_recibos(conn, parsear_eiac_recibos(eiac_recibos_xml))
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []

    assert any("Producción confirmada" in md.value for md in at.markdown)
    assert any(f"Periodo futuro · {periodo_futuro}" in md.value for md in at.markdown)

    # 50€ recibo, ClaseFormaPago=CC -> pista mensual -> anualizado x12 = 600€.
    assert any(
        m.label == "Producción nueva Salud" and _valor_a_float(m.value) == pytest.approx(600.0)
        for m in at.metric
    )


def test_produccion_confirmada_meses_futuros_no_aparece_sin_datos(tmp_path, monkeypatch):
    """Sin ningún periodo más allá del mes siguiente con datos, el bloque
    de "meses futuros" no debe aparecer — nunca un hueco vacío ni un 0€
    engañoso."""
    db_path, conn = _nueva_db(tmp_path, "sin_meses_futuros.db")
    cargar_facturacion(conn, parsear_facturacion(FIXTURES / "facturacion_sample.csv"))
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []
    assert not any("Producción confirmada" in md.value for md in at.markdown)


def test_objetivo_anual_incluye_produccion_solo_de_eiac(tmp_path, monkeypatch):
    """Tarea 3 (segunda parte): Objetivo anual debe contar también la
    producción que solo está en EIAC del mes en curso — antes usaba
    df_polizas/df_facturacion "oficiales" sin conectar el canal EIAC.
    """
    hoy = date.today()
    fecha_efecto_actual = f"{hoy.year:04d}-{hoy.month:02d}-01"

    eiac_polizas_xml = tmp_path / "EIAC-ENV-POLI-objetivo.xml"
    eiac_polizas_xml.write_text(
        _eiac_polizas_xml("24300-70099002", fecha_efecto_actual), encoding="utf-8"
    )
    eiac_recibos_xml = tmp_path / "EIAC-ENV-RECI-objetivo.xml"
    eiac_recibos_xml.write_text(
        _eiac_recibos_xml("24300-70099002", fecha_efecto_actual, "101.00", "100.00"), encoding="utf-8"
    )

    db_path, conn = _nueva_db(tmp_path, "objetivo_eiac.db")
    cargar_eiac_polizas(conn, parsear_eiac_polizas(eiac_polizas_xml))
    cargar_eiac_recibos(conn, parsear_eiac_recibos(eiac_recibos_xml))
    conn.close()

    at = _correr_app(db_path, monkeypatch)
    assert at.exception == []

    metricas = {m.label: m.value for m in at.metric}
    etiqueta = next(k for k in metricas if k.startswith("Producción acumulada en"))
    # 100€ recibo, ClaseFormaPago=CC -> pista mensual -> anualizado x12 = 1.200€
    # (único dato del año en esta BD aislada, así que el acumulado ES ese importe).
    assert "1,200.00 €" in metricas[etiqueta]
