"""Planificación reproducible y aplicación transaccional del backfill POLI.

El plan omite los nombres de asegurados: los payloads completos se recuperan
del XML verificado por SHA256 al aplicar. Ningún modo reimporta pólizas.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import xml.etree.ElementTree as ET

import pandas as pd

from engine.calibracion import corregir_periodo_liquidacion_vida_mensual
from engine.config_contrato import cargar_contrato
from engine.eiac_integracion import integrar_eiac
from engine.wanderlust import calcular_pae_anual, redondear_euros
from ingestion.eiac_xml import parsear_eiac_polizas

HISTORICAL_SNAPSHOT_SHA = "ab0ac2554652c7ce2e4e971d9a64ab376ddf0c0521a38fbdeb3c4286a6571b8b"
SNAPSHOT_SHA = "2e9a493999e608b525a428c2980ea5a32b4759ceef29de80ba84a6089c97a61f"
CURRENT_CUTOFF = "2026-09-28"
TARGETS = ("prima_neta_anualizada_poli", "fecha_fin_seguro", "coberturas_wanderlust")
STATUSES = ("SAFE_EXACT", "SAFE_EQUIVALENT", "AMBIGUOUS", "INCOMPATIBLE", "NO_XML", "ALREADY_ENRICHED")
SAFE = STATUSES[:2]
PROTECTED = (
    "cliente_codigo", "numero_poliza", "situacion_poliza", "clase_poliza",
    "fecha_efecto_inicial", "fecha_emision", "descripcion_riesgo", "descripcion_ramo",
    "codigo_entidad_interno", "ramo_entidad", "fecha_anulacion", "motivo_anulacion",
    "prima_neta_poliza",
)
DATES = {"fecha_efecto_inicial", "fecha_emision", "fecha_anulacion", "fecha_fin_seguro"}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def blank(value):
    return value is None or (isinstance(value, str) and not value.strip()) or (
        not isinstance(value, (list, dict)) and bool(pd.isna(value))
    )


def scalar(value):
    if blank(value):
        return None
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    return value.item() if hasattr(value, "item") else value


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def date_value(value):
    return None if blank(value) else pd.Timestamp(value).isoformat()


def coverage_value(value):
    if blank(value):
        return None
    items = json.loads(value) if isinstance(value, str) else value
    return encoded(sorted(items, key=encoded))


def coverage_hash(value):
    return hashlib.sha256((coverage_value(value) or "").encode()).hexdigest()


def equivalent(field, left, right):
    if blank(left) or blank(right):
        return blank(left) and blank(right)
    if field in DATES:
        return date_value(left) == date_value(right)
    if field in ("prima_neta_poliza", "prima_neta_anualizada_poli"):
        return abs(Decimal(str(left)) - Decimal(str(right))) <= Decimal("0.000001")
    if field == "coberturas_wanderlust":
        return coverage_value(left) == coverage_value(right)
    return str(left).strip() == str(right).strip()


def contradictions(row, version):
    return [field for field in PROTECTED if not equivalent(field, row.get(field), version.get(field))]


def payload(version):
    annual = version.get(TARGETS[0])
    return (
        None if blank(annual) else str(Decimal(str(annual)).normalize()),
        date_value(version.get(TARGETS[1])), coverage_value(version.get(TARGETS[2])),
    )


def fingerprint(row):
    return hashlib.sha256(encoded({k: scalar(v) for k, v in dict(row).items()}).encode()).hexdigest()


def inventory(xml_dir):
    versions = []
    for path in sorted(Path(xml_dir).rglob("EIAC-ENV-POLI*.xml")):
        digest = sha256(path)
        root = ET.parse(path).getroot()
        ns = {"e": root.tag.split("}")[0][1:]}

        def text(xpath):
            node = root.find("/".join("e:" + part for part in xpath.split("/")), ns)
            return node.text.strip() if node is not None and node.text else None

        if text("Cabecera/Receptor/CodigoInterno") != "M10603":
            continue
        for index, (_, row) in enumerate(parsear_eiac_polizas(path).iterrows(), 1):
            versions.append({
                **row.to_dict(), "xml_source": str(path.resolve()), "xml_sha256": digest,
                "xml_created": text("Cabecera/FechaCreacion"),
                "xml_lot": text("Cabecera/DatosLote/IdLote"), "xml_record": index,
            })
        if sha256(path) != digest:
            raise ValueError("XML changed while building inventory")
    return versions


def rank(version):
    # Deliberadamente no infiere orden contractual dentro de un mismo día/lote.
    return pd.Timestamp(version["xml_created"]) if version.get("xml_created") else pd.Timestamp.min


def classify(eiac, versions):
    by_id = defaultdict(list)
    for version in versions:
        by_id[version["id_poliza"]].append(version)
    decisions = []
    for index, row in eiac.iterrows():
        candidates = by_id[row.id_poliza]
        compatible = [v for v in candidates if not contradictions(row, v)]
        selected, updates, detail = None, {}, None
        if any(not blank(row.get(field)) for field in TARGETS):
            status = "ALREADY_ENRICHED"
            matches = bool(compatible) and all(
                all(blank(row.get(k)) or equivalent(k, row[k], v.get(k)) for k in TARGETS)
                for v in compatible
            )
            detail = "ALREADY_ENRICHED_MATCH" if matches else "ALREADY_ENRICHED_CONFLICT"
            reason = detail
        elif not candidates:
            status, reason = "NO_XML", "no_versions"
        elif not compatible:
            status, reason = "INCOMPATIBLE", "all_versions_contradict_protected_fields"
        elif len({payload(v) for v in compatible}) > 1:
            status, reason = "AMBIGUOUS", "different_compatible_payloads"
        elif any(rank(v) >= max(rank(c) for c in compatible) for v in candidates if contradictions(row, v)):
            status, reason = "AMBIGUOUS", "same_day_or_later_protected_conflict"
        else:
            status = "SAFE_EXACT" if len(compatible) == 1 else "SAFE_EQUIVALENT"
            reason = "unique_compatible_payload_no_later_conflict"
            selected = sorted(compatible, key=lambda v: (rank(v), v["xml_source"], v["xml_record"]))[-1]
            updates = {k: selected[k] for k in TARGETS if blank(row.get(k)) and not blank(selected.get(k))}
        decisions.append({
            "index": index, "row": row, "status": status, "detail": detail,
            "reason": reason, "source": selected, "updates": updates,
            "candidates": candidates,
        })
    return decisions


def simulate(eiac, decisions):
    copy = eiac.copy(deep=True)
    for decision in decisions:
        if decision["status"] in SAFE:
            for field, value in decision["updates"].items():
                if field not in TARGETS or not blank(copy.at[decision["index"], field]):
                    raise ValueError("Forbidden field or nonempty destination")
                copy.at[decision["index"], field] = value
    return copy


def readonly(db):
    conn = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    return conn


def frames(conn):
    return {
        "polizas": pd.read_sql("SELECT * FROM polizas", conn, parse_dates=["fecha_emision", "fecha_efecto", "fecha_baja"]),
        "facturacion": pd.read_sql("SELECT * FROM facturacion", conn, parse_dates=["fecha_desde", "fecha_hasta"]),
        "eiac_polizas": pd.read_sql("SELECT * FROM eiac_polizas", conn, parse_dates=["fecha_efecto_inicial", "fecha_emision"]),
        "eiac_recibos": pd.read_sql("SELECT * FROM eiac_recibos", conn, parse_dates=["fecha_efecto_inicial", "fecha_emision"]),
    }


def pipeline(data, eiac, contrato):
    pol, fac, rec = (data[k] for k in ("polizas", "facturacion", "eiac_recibos"))
    integration = integrar_eiac(eiac, rec, pol, fac)
    combined_pol = pd.concat([pol, integration.polizas_provisionales], ignore_index=True).drop_duplicates("poliza", keep="last") if not integration.polizas_provisionales.empty else pol
    combined_fac = pd.concat([fac, integration.facturacion_eiac], ignore_index=True) if not integration.facturacion_eiac.empty else fac
    return combined_pol, corregir_periodo_liquidacion_vida_mensual(combined_fac, combined_pol, contrato)


def calculate(data, eiac, contrato, cutoff):
    pol, fac = pipeline(data, eiac, contrato)
    return calcular_pae_anual(pol, fac, contrato, 2026, df_eiac_polizas=eiac, fecha_corte=cutoff)


def metrics(data, eiac, contrato):
    result = calculate(data, eiac, contrato, CURRENT_CUTOFF)
    august = calculate(data, eiac, contrato, "2026-08-31")
    return {
        "pae_total": result.pae_total, "pae_efectivo": result.pae_efectivo,
        "pae_futuro": result.pae_futuro, "pae_31_08": august.pae_efectivo,
        "altas": sum(v.polizas_alta for v in result.por_categoria.values()),
        "bajas": sum(v.polizas_baja for v in result.por_categoria.values()),
        "sin_categoria": result.sin_categoria,
        "categories": {k: redondear_euros(v.pae) for k, v in result.por_categoria.items()},
    }


def policy_pae(pol, fac, eiac, contrato, number):
    result = calcular_pae_anual(pol[pol.poliza == number], fac[fac.poliza == number], contrato, 2026, df_eiac_polizas=eiac[eiac.numero_poliza == number])
    return sum(v.pae for v in result.por_categoria.values())


def build_manifest(data, versions, contrato, db_sha, config_path, xml_dir):
    eiac = data["eiac_polizas"]
    decisions = classify(eiac, versions)
    after = simulate(eiac, decisions)
    baseline, simulated = metrics(data, eiac, contrato), metrics(data, after, contrato)
    before_pol, before_fac = pipeline(data, eiac, contrato)
    after_pol, after_fac = pipeline(data, after, contrato)
    proposals, excluded = [], []
    for decision in decisions:
        row = decision["row"]
        public = {
            "id_poliza": row.id_poliza, "numero_poliza": row.numero_poliza,
            "status": decision["status"], "reason": decision["reason"],
            "row_sha256": fingerprint(row),
        }
        if not decision["updates"]:
            excluded.append({**public, "detail": decision["detail"]})
            continue
        source = decision["source"]
        pae_before = policy_pae(before_pol, before_fac, eiac, contrato, row.numero_poliza)
        pae_after = policy_pae(after_pol, after_fac, after, contrato, row.numero_poliza)
        delta = pae_after - pae_before
        current, proposed = {}, {}
        for field, value in decision["updates"].items():
            current[field] = scalar(row[field])
            proposed[field] = {"sha256": coverage_hash(value), "count": len(json.loads(value))} if field == "coberturas_wanderlust" else scalar(value)
        proposals.append({
            **public, "xml_source": source["xml_source"], "xml_sha256": source["xml_sha256"],
            "xml_created": source["xml_created"], "xml_lot": source["xml_lot"],
            "xml_record": source["xml_record"], "current_values": current,
            "proposed_values": proposed, "fields": list(decision["updates"]),
            "pae_before": round(pae_before, 6), "pae_after": round(pae_after, 6),
            "delta_pae": round(delta, 6),
            "coverage_sha256": coverage_hash(source["coberturas_wanderlust"]),
        })
    total_delta = round(simulated["pae_total"] - baseline["pae_total"], 2)
    individual = round(sum(row["delta_pae"] for row in proposals), 6)
    residual = round(total_delta - individual, 6)
    if abs(residual) > 0.01:
        raise ValueError("Individual PAE deltas do not reconcile")
    counts = Counter(row["status"] for row in decisions)
    cells = Counter(field for row in proposals for field in row["fields"])
    manifest = {
        "version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_sha256": db_sha, "config_path": str(Path(config_path).resolve()),
        "config_sha256": sha256(config_path), "xml_dir": str(Path(xml_dir).resolve()),
        "sources": {v["xml_source"]: v["xml_sha256"] for v in versions},
        "cutoff": CURRENT_CUTOFF, "historical_cutoff": "2026-08-31", "year": 2026,
        "eiac_total": len(eiac), "status_counts": {s: counts[s] for s in STATUSES},
        "expected_rows": len(proposals), "expected_cells": sum(cells.values()),
        "cells_by_field": {k: cells[k] for k in TARGETS},
        "baseline": baseline, "simulated": simulated, "delta_pae": total_delta,
        "individual_delta_sum": individual, "reconciliation_residual": residual,
        "updates": proposals, "excluded": excluded,
    }
    return manifest, decisions


def dry_run(db, xml_dir, config_path, expected_sha=SNAPSHOT_SHA):
    digest = sha256(db)
    if digest != expected_sha:
        raise ValueError("Snapshot SHA256 changed; stop, do not adapt expectations")
    with closing(readonly(db)) as conn:
        data = frames(conn)
    config_digest = sha256(config_path)
    versions = inventory(xml_dir)
    manifest, _ = build_manifest(data, versions, cargar_contrato(config_path), digest, config_path, xml_dir)
    if sha256(config_path) != config_digest or any(sha256(path) != value for path, value in manifest["sources"].items()):
        raise ValueError("XML/configuration changed during dry-run")
    if sha256(db) != digest:
        raise ValueError("Database changed during dry-run")
    return manifest


def update_sql(field):
    if field not in TARGETS:
        raise ValueError("Field is not in backfill whitelist")
    return f"UPDATE eiac_polizas SET {field} = ? WHERE id_poliza = ? AND {field} IS NULL"


def write_cells(conn, decisions):
    counts = Counter()
    for decision in decisions:
        if decision["status"] not in SAFE:
            if decision["updates"]:
                raise ValueError("Excluded row has updates")
            continue
        for field, value in decision["updates"].items():
            cursor = conn.execute(update_sql(field), (scalar(value), decision["row"].id_poliza))
            if cursor.rowcount != 1:
                raise ValueError("Unexpected rowcount; rollback required")
            counts[field] += cursor.rowcount
    return {k: counts[k] for k in TARGETS}


def table_fingerprints(conn):
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    result = {}
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        cursor = conn.execute(f"SELECT * FROM {quoted}")
        columns = [x[0] for x in cursor.description]
        result[name] = sorted(fingerprint(dict(zip(columns, row))) for row in cursor)
    return result


def comparable(manifest):
    return {k: v for k, v in manifest.items() if k != "generated_at"}


def apply_manifest(db, manifest, backup_path, export_path):
    """Solo se invoca explícitamente; cualquier error anterior al COMMIT revierte.

    La exportación se prepara dentro de la transacción. Un fallo de escritura
    del artefacto posterior al COMMIT se informa como fallo posterior al commit;
    nunca se promete un rollback ya imposible.
    """
    db, backup_path, export_path = map(Path, (db, backup_path, export_path))
    if manifest["expected_rows"] != 97 or manifest["expected_cells"] != 195:
        raise ValueError("Apply is restricted to the approved 97-row/195-cell plan")
    fresh = dry_run(db, manifest["xml_dir"], manifest["config_path"], manifest["db_sha256"])
    if comparable(fresh) != comparable(manifest):
        raise ValueError("Manifest or sources changed")
    if backup_path.resolve() == db.resolve() or export_path.resolve() in {db.resolve(), backup_path.resolve()}:
        raise ValueError("Backup/export must not overwrite database or each other")
    if backup_path.exists() or export_path.exists():
        raise ValueError("Backup/export already exists")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    export_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(readonly(db)) as ro:
        if ro.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise ValueError("Binary backup requires quiescent DELETE journal mode")
    if any(Path(str(db) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise ValueError("Pending journal/WAL; stop writers before applying")
    content = db.read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest["db_sha256"]:
        raise ValueError("Snapshot changed before backup")
    with backup_path.open("xb") as file:
        file.write(content)
    backup_path.chmod(0o600)
    if sha256(backup_path) != manifest["db_sha256"]:
        raise ValueError("Backup SHA256 mismatch")
    committed = False
    with closing(sqlite3.connect(db, timeout=0)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            if sha256(db) != manifest["db_sha256"]:
                raise ValueError("Snapshot changed before transaction")
            if conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                raise ValueError("Unexpected trigger")
            data = frames(conn)
            before = table_fingerprints(conn)
            contrato = cargar_contrato(manifest["config_path"])
            verified, decisions = build_manifest(data, inventory(manifest["xml_dir"]), contrato, manifest["db_sha256"], manifest["config_path"], manifest["xml_dir"])
            if comparable(verified) != comparable(manifest):
                raise ValueError("Transaction snapshot contradicts manifest")
            expected = simulate(data["eiac_polizas"], decisions)
            counts = write_cells(conn, decisions)
            if counts != manifest["cells_by_field"] or conn.total_changes != 195:
                raise ValueError("Unexpected cell counts")
            actual = frames(conn)
            # Full rows, including every forbidden field and excluded row.
            expected_hashes = sorted(fingerprint(row) for _, row in expected.iterrows())
            actual_hashes = sorted(fingerprint(row) for _, row in actual["eiac_polizas"].iterrows())
            if expected_hashes != actual_hashes:
                raise ValueError("Unexpected row/field mutation")
            after = table_fingerprints(conn)
            if any(before[name] != after[name] for name in before if name != "eiac_polizas"):
                raise ValueError("Another table changed")
            if metrics(actual, actual["eiac_polizas"], contrato) != manifest["simulated"]:
                raise ValueError("PAE validation failed")
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or conn.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Integrity validation failed")
            from dashboard.exportacion import construir_excel_completo
            excel = construir_excel_completo(
                df_polizas=actual["polizas"], df_facturacion=actual["facturacion"],
                df_eiac_polizas=actual["eiac_polizas"], df_eiac_recibos=actual["eiac_recibos"],
                df_liquidacion=pd.read_sql("SELECT * FROM liquidacion", conn, parse_dates=["fecha_desde", "fecha_hasta"]),
                df_factura_pdf=pd.read_sql("SELECT * FROM factura_pdf", conn),
                contrato=contrato, hoy=date.fromisoformat(manifest["cutoff"]),
            )
            if sha256(manifest["config_path"]) != manifest["config_sha256"] or any(sha256(path) != digest for path, digest in manifest["sources"].items()):
                raise ValueError("Sources changed before COMMIT")
            conn.execute("COMMIT")
            committed = True
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    try:
        with export_path.open("xb") as file:
            file.write(excel)
        export_path.chmod(0o600)
    except Exception as error:
        raise RuntimeError(f"DATABASE COMMITTED={committed}; export failed; backup={backup_path}") from error
    return {"backup_sha256": sha256(backup_path), "db_sha256_after": sha256(db), "export": str(export_path)}


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--db", type=Path, default=root / "data/asisa.db")
    parser.add_argument("--xml-dir", type=Path, default=root.parent)
    parser.add_argument("--config", type=Path, default=root / "config/contrato.yaml")
    parser.add_argument("--manifest", type=Path, default=root / "data/processed/eiac_wanderlust_backfill_manifest_2026-09-28.json")
    parser.add_argument("--expected-sha", default=SNAPSHOT_SHA)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--export", type=Path)
    args = parser.parse_args()
    if args.dry_run:
        manifest = dry_run(args.db, args.xml_dir, args.config, args.expected_sha)
        if args.manifest.resolve() == args.db.resolve() or args.manifest.suffix != ".json":
            parser.error("Manifest must be a JSON artifact, separate from database")
        if args.manifest.exists():
            parser.error("Manifest already exists; choose a new artifact path")
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        with args.manifest.open("x", encoding="utf-8") as file:
            json.dump(manifest, file, ensure_ascii=False, indent=2)
        args.manifest.chmod(0o600)
        print(encoded({k: v for k, v in manifest.items() if k not in {"updates", "excluded", "sources"}}))
    else:
        if not args.backup or not args.export:
            parser.error("Apply requires explicit --backup and --export")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if manifest["db_sha256"] != args.expected_sha:
            parser.error("Manifest snapshot differs from --expected-sha")
        print(encoded(apply_manifest(args.db, manifest, args.backup, args.export)))
