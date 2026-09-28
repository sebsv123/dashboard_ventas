"""Protecciones del plan; las escrituras de pruebas usan SQLite en memoria."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from engine.config_contrato import cargar_contrato
from engine.wanderlust import calcular_pae_anual
from tooling import eiac_wanderlust_backfill as bf

ROOT = Path(__file__).resolve().parents[1]


def row(**overrides):
    return {
        "id_poliza": "24300-12345678", "numero_poliza": "12345678",
        "situacion_poliza": "EV", "clase_poliza": "NP",
        "fecha_efecto_inicial": "2026-01-01", "fecha_anulacion": None,
        "prima_neta_poliza": 100.0, "prima_neta_anualizada_poli": None,
        "fecha_fin_seguro": None, "coberturas_wanderlust": None,
        **overrides,
    }


def version(dbrow, **overrides):
    return {
        **dbrow, "prima_neta_anualizada_poli": dbrow["prima_neta_poliza"],
        "coberturas_wanderlust": "[]", "xml_created": "2026-01-02",
        "xml_source": "/fixture/POLI.xml", "xml_record": 1,
        **overrides,
    }


@pytest.fixture
def contrato():
    return cargar_contrato(ROOT / "config/contrato.yaml")


def calculate(eiac, contrato, travel=False):
    first = eiac.iloc[0]
    pol = pd.DataFrame([{
        "poliza": first.numero_poliza,
        "razon_social": "ASISA TRAVEL AND YOU" if travel else "ASISA PARTICULARES",
        "forma_pago": "U" if travel else "A", "fecha_efecto": first.fecha_efecto_inicial,
        "situacion": "B" if travel else "A", "fecha_baja": first.fecha_anulacion,
    }])
    return calcular_pae_anual(pol, pd.DataFrame(), contrato, 2026, df_eiac_polizas=eiac)


def test_recuperacion_gs_conserva_prima_y_no_duplica_coberturas(contrato):
    original = pd.DataFrame([row()])
    coverages = json.dumps([
        {"id_riesgo": "1", "id_cobertura": "GS30", "prima_neta": 4},
        {"id_riesgo": "2", "id_cobertura": "GS30", "prima_neta": 6},
        {"id_cobertura": "GS09", "prima_neta": 20},
        {"id_cobertura": "GS99", "prima_neta": 5},
    ])
    decisions = bf.classify(original, [version(row(), coberturas_wanderlust=coverages)])
    enriched = bf.simulate(original, decisions)
    result = calculate(enriched, contrato)
    assert result.por_categoria["Salud Particulares"].pae == 65
    assert result.por_categoria["ASISA Dental"].pae == 15
    assert result.por_categoria["ASISA Hospitalización"].pae == 80
    assert result.por_categoria["ASISA Accidentes"].pae == 20
    assert result.pae_total == 180
    assert sum(v.pae for v in result.por_categoria.values()) == 65 + 15 + 80 + 20
    assert enriched.iloc[0].prima_neta_poliza == 100
    assert original.iloc[0].coberturas_wanderlust is None


def test_travel_recupera_fin_poli_y_reconoce_vencimiento_natural(contrato):
    dbrow = row(situacion_poliza="EX", clase_poliza="AN", fecha_efecto_inicial="2026-01-21", fecha_anulacion="2026-02-09", prima_neta_poliza=29.13)
    original = pd.DataFrame([dbrow])
    decisions = bf.classify(original, [version(dbrow, fecha_fin_seguro="2026-02-09", prima_neta_anualizada_poli=559.6)])
    assert decisions[0]["status"] == "SAFE_EXACT"
    enriched = bf.simulate(original, decisions)
    before = calculate(original, contrato, travel=True).por_categoria["ASISA Travel"].pae
    after = calculate(enriched, contrato, travel=True).por_categoria["ASISA Travel"].pae
    assert before == pytest.approx(-14.565)
    assert after == pytest.approx(14.565)
    assert after - before == pytest.approx(29.13)
    assert enriched.iloc[0].prima_neta_poliza == 29.13


@pytest.mark.parametrize("field,value", [
    ("prima_neta_anualizada_poli", 229.29),
    ("fecha_fin_seguro", "2026-02-09"),
    ("coberturas_wanderlust", '[{"id_cobertura":"GS30","prima_neta":10}]'),
])
@pytest.mark.parametrize("conflict", [False, True])
def test_no_sobrescribe_fila_con_cualquier_campo_enriquecido(field, value, conflict):
    dbrow = row(**{field: value})
    candidate = version(dbrow, **{field: value})
    if conflict:
        candidate[field] = 400 if field == "prima_neta_anualizada_poli" else "2026-03-01" if field == "fecha_fin_seguro" else "[]"
    original = pd.DataFrame([dbrow])
    decisions = bf.classify(original, [candidate])
    assert decisions[0]["status"] == "ALREADY_ENRICHED"
    assert decisions[0]["detail"] == ("ALREADY_ENRICHED_CONFLICT" if conflict else "ALREADY_ENRICHED_MATCH")
    assert decisions[0]["updates"] == {}
    pd.testing.assert_frame_equal(original, bf.simulate(original, decisions))


@pytest.mark.parametrize("kind", ["different_payload", "later_business_conflict", "same_day_business_conflict"])
def test_xml_ambiguo_no_actualiza_ni_altera_pae(kind, contrato):
    dbrow = row()
    first = version(dbrow)
    second = deepcopy(first)
    second["xml_source"] = "/fixture/SECOND.xml"
    if kind == "different_payload":
        second["coberturas_wanderlust"] = '[{"id_cobertura":"GS30","prima_neta":10}]'
    else:
        second["clase_poliza"] = "SU"
        second["xml_created"] = "2026-01-03" if kind.startswith("later") else first["xml_created"]
    original = pd.DataFrame([dbrow])
    decisions = bf.classify(original, [first, second])
    assert decisions[0]["status"] == "AMBIGUOUS"
    assert decisions[0]["updates"] == {}
    enriched = bf.simulate(original, decisions)
    pd.testing.assert_frame_equal(original, enriched)
    assert calculate(enriched, contrato).pae_total == calculate(original, contrato).pae_total
    conn = sqlite3.connect(":memory:")
    try:
        assert bf.write_cells(conn, decisions) == dict.fromkeys(bf.TARGETS, 0)
        assert conn.total_changes == 0
    finally:
        conn.close()


def test_safe_equivalent_no_depende_del_orden_de_coberturas():
    dbrow = row()
    first = version(dbrow, coberturas_wanderlust='[{"id_cobertura":"GS30","prima_neta":10},{"id_cobertura":"GS09","prima_neta":20}]')
    second = version(dbrow, xml_source="/fixture/COPY.xml", coberturas_wanderlust='[{"prima_neta":20,"id_cobertura":"GS09"},{"prima_neta":10,"id_cobertura":"GS30"}]')
    decisions = bf.classify(pd.DataFrame([dbrow]), [first, second])
    assert decisions[0]["status"] == "SAFE_EQUIVALENT"
    assert bf.coverage_hash(first["coberturas_wanderlust"]) == bf.coverage_hash(second["coberturas_wanderlust"])


@pytest.mark.parametrize("field", bf.TARGETS)
def test_sql_whitelist_id_parametrizado_y_null_obligatorio(field):
    sql = bf.update_sql(field)
    assert sql == f"UPDATE eiac_polizas SET {field} = ? WHERE id_poliza = ? AND {field} IS NULL"
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE eiac_polizas (id_poliza TEXT UNIQUE, prima_neta_poliza REAL, prima_neta_anualizada_poli REAL, fecha_fin_seguro TEXT, coberturas_wanderlust TEXT)")
        conn.execute("INSERT INTO eiac_polizas VALUES (?, 100, NULL, NULL, NULL)", ("123",))
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        value = 200 if field == bf.TARGETS[0] else "2026-02-09" if field == bf.TARGETS[1] else "[]"
        assert conn.execute(sql, (value, "123")).rowcount == 1
        assert conn.execute(sql, (value, "123")).rowcount == 0
        assert conn.execute(sql, (value, "missing")).rowcount == 0
        assert conn.execute("SELECT prima_neta_poliza FROM eiac_polizas").fetchone()[0] == 100
        conn.rollback()
        assert conn.execute(f"SELECT {field} FROM eiac_polizas").fetchone()[0] is None
    finally:
        conn.close()


@pytest.mark.parametrize("field", ["prima_neta_poliza", "situacion_poliza", "fecha_import", "id_poliza", "coberturas_wanderlust = NULL; DROP TABLE eiac_polizas; --"])
def test_rechaza_cualquier_columna_fuera_de_whitelist(field):
    with pytest.raises(ValueError, match="whitelist"):
        bf.update_sql(field)


def test_rowcount_inesperado_revierte_celdas_previas():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE eiac_polizas (id_poliza TEXT UNIQUE, prima_neta_anualizada_poli REAL, fecha_fin_seguro TEXT, coberturas_wanderlust TEXT)")
        conn.execute("INSERT INTO eiac_polizas VALUES ('24300-12345678', NULL, '2026-02-09', NULL)")
        conn.commit()
        decisions = [{"status": "SAFE_EXACT", "row": pd.Series(row()), "updates": {bf.TARGETS[0]: 100, bf.TARGETS[1]: "2026-02-09"}}]
        with pytest.raises(ValueError, match="rowcount"):
            with conn:
                bf.write_cells(conn, decisions)
        assert conn.execute("SELECT prima_neta_anualizada_poli FROM eiac_polizas").fetchone()[0] is None
    finally:
        conn.close()


def test_snapshot_guard_rechaza_antes_de_abrir_bd(tmp_path, monkeypatch):
    db = tmp_path / "changed.db"
    db.write_bytes(b"not the snapshot")
    monkeypatch.setattr(bf, "readonly", lambda _: pytest.fail("must not open a changed snapshot"))
    with pytest.raises(ValueError, match="Snapshot SHA256 changed"):
        bf.dry_run(db, tmp_path, ROOT / "config/contrato.yaml")


def test_inventario_rechaza_xml_que_cambia_durante_lectura(tmp_path, monkeypatch):
    path = tmp_path / "EIAC-ENV-POLI-fixture.xml"
    path.write_text('<ProcesosEIAC xmlns="http://www.tirea.es/EIAC/ProcesosEIAC"><Cabecera><Receptor><CodigoInterno>M10603</CodigoInterno></Receptor></Cabecera></ProcesosEIAC>')

    def changed_xml(_):
        path.write_text("changed during parsing")
        return pd.DataFrame([row()])

    monkeypatch.setattr(bf, "parsear_eiac_polizas", changed_xml)
    with pytest.raises(ValueError, match="XML changed"):
        bf.inventory(tmp_path)


def test_conexion_dry_run_es_solo_lectura(tmp_path):
    path = tmp_path / "fixture.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE example (value TEXT)")
    conn = bf.readonly(path)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO example VALUES ('forbidden')")
    finally:
        conn.close()


def test_integracion_snapshot_real_y_casos_de_control():
    db = ROOT / "data/asisa.db"
    if not db.exists():
        pytest.skip("Private snapshot is not distributed with the repository")
    # The private database may be either the approved pre-apply snapshot or the
    # verified post-backfill state.  The normal suite must not pin production
    # to the former binary SHA after a successful apply.
    if bf.sha256(db) != bf.SNAPSHOT_SHA:
        with sqlite3.connect(db) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert conn.execute("SELECT COUNT(*) FROM eiac_polizas").fetchone() == (112,)
            data = bf.frames(conn)
            contrato = cargar_contrato(ROOT / "config/contrato.yaml")
            assert bf.metrics(data, data["eiac_polizas"], contrato) == {
                "pae_total": 82557.41, "pae_efectivo": 65877.25, "pae_futuro": 16680.16,
                "pae_31_08": 55020.11, "altas": 102, "bajas": 0, "sin_categoria": 0,
                "categories": {"Salud Particulares": 67099.84, "ASISA Dental": 3758.63,
                               "ASISA Hospitalización": 2782.56, "ASISA Accidentes": 1044.2,
                               "ASISA Vida": 7795.9, "ASISA Travel": 76.28},
            }
        return
    manifest = bf.dry_run(db, ROOT.parent, ROOT / "config/contrato.yaml")
    assert manifest["cutoff"] == "2026-09-28"
    assert manifest["eiac_total"] == 112
    assert manifest["status_counts"] == {"SAFE_EXACT": 79, "SAFE_EQUIVALENT": 18, "AMBIGUOUS": 11, "INCOMPATIBLE": 0, "NO_XML": 0, "ALREADY_ENRICHED": 4}
    assert manifest["expected_rows"] == 97
    assert manifest["expected_cells"] == 195
    assert manifest["cells_by_field"] == dict(zip(bf.TARGETS, [97, 1, 97]))
    assert manifest["baseline"]["pae_total"] == 79052.67
    assert manifest["baseline"]["pae_efectivo"] == 62530.91
    assert manifest["baseline"]["pae_futuro"] == 16521.76
    assert manifest["baseline"]["pae_31_08"] == 51945.58
    assert manifest["simulated"] == {
        "pae_total": 82557.41, "pae_efectivo": 65877.25, "pae_futuro": 16680.16,
        "pae_31_08": 55020.11, "altas": 102, "bajas": 0, "sin_categoria": 0,
        "categories": {"Salud Particulares": 67099.84, "ASISA Dental": 3758.63,
                       "ASISA Hospitalización": 2782.56, "ASISA Accidentes": 1044.2,
                       "ASISA Vida": 7795.9, "ASISA Travel": 76.28},
    }
    assert manifest["delta_pae"] == 3504.74
    assert abs(manifest["reconciliation_residual"]) <= 0.01
    updates = {row["numero_poliza"]: row for row in manifest["updates"]}
    excluded = {row["numero_poliza"]: row for row in manifest["excluded"]}
    assert updates["63927786"]["status"] == "SAFE_EXACT"
    assert updates["63927786"]["proposed_values"]["fecha_fin_seguro"] == "2026-02-09"
    assert updates["63927786"]["delta_pae"] == 29.13
    assert updates["64918319"]["status"] == "SAFE_EQUIVALENT"
    assert updates["64918319"]["proposed_values"]["prima_neta_anualizada_poli"] == 456
    assert updates["64918319"]["delta_pae"] == 0
    assert excluded["64917599"]["status"] == "ALREADY_ENRICHED"
    assert excluded["64917599"]["detail"] == "ALREADY_ENRICHED_MATCH"
    for number in ("64943457", "64936036", "64937956"):
        assert excluded[number]["status"] == "ALREADY_ENRICHED"
        assert excluded[number]["detail"] == "ALREADY_ENRICHED_MATCH"
    for number in ("64917599", "63965406", "63973744"):
        assert number not in updates
    for number in ("63965406", "63973744"):
        assert excluded[number]["status"] == "AMBIGUOUS"
    # No complete coverage payload, names or risk descriptions in the artifact.
    for update in updates.values():
        assert set(update["proposed_values"]["coberturas_wanderlust"]) == {"sha256", "count"}
