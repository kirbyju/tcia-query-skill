#!/usr/bin/env python3
"""Resolve Analysis Result DICOM download manifests to collection-scoped patients.

Viewer links are navigation, not patient crosswalks. The WordPress download
record owns the manifest; IDC enriches its exact series selection. This module
does not fetch images, interpret shared-cart names, or infer patient aliases.
"""
from __future__ import annotations

import hashlib
import csv
import io
import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.parse import urlencode
from urllib.request import Request, urlopen


POLICY_VERSION = 1


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def name_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def fetch_nbia_series(uids: list[str], *, timeout: int, max_bytes: int) -> tuple[list[dict[str, str]], list[str]]:
    """Query only public metadata for the exact UID list; never transfer DICOM."""
    rows, hashes = [], []
    for offset in range(0, len(uids), 1000):
        selection = uids[offset:offset + 1000]
        request = Request(
            "https://nbia.cancerimagingarchive.net/nbia-api/services/v4/getSeriesMetadata",
            data=urlencode({"list": ",".join(selection)}).encode(),
            headers={"User-Agent": "tcia-dicom-source-scope/1"},
        )
        with urlopen(request, timeout=timeout) as response:
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("NBIA metadata response exceeds byte limit")
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        required = {"SeriesInstanceUID", "Collection", "PatientID"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError("NBIA metadata response lacks required identifiers")
        for row in reader:
            if row["SeriesInstanceUID"] not in selection:
                raise ValueError("NBIA returned a series outside the manifest selection")
            rows.append({key: row[key] for key in required})
        hashes.append(hashlib.sha256(data).hexdigest())
    return rows, hashes


def parse_tcia_manifest(data: bytes) -> list[str]:
    lines = data.decode("utf-8-sig").splitlines()
    markers = [i for i, line in enumerate(lines) if line.strip() == "ListOfSeriesToDownload="]
    if len(markers) != 1:
        raise ValueError("manifest must contain one ListOfSeriesToDownload marker")
    uids = [line.strip() for line in lines[markers[0] + 1:] if line.strip()]
    if not uids or any(
        len(uid) > 64 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", uid)
        for uid in uids
    ):
        raise ValueError("manifest has an empty or invalid DICOM series selection")
    return sorted(set(uids))


def download_candidates(snapshot_db: Path) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    with closing(sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        columns = {r[1] for r in conn.execute("PRAGMA table_info(agent_current_downloads)")}
        required = {"download_id", "short_title", "parent_source", "hidden", "download_url", "search_url"}
        if not required.issubset(columns):
            return [], {}
        collections: dict[str, list[str]] = defaultdict(list)
        for row in conn.execute(
            "SELECT short_title FROM agent_datasets WHERE source='collections' AND hidden=0"
        ):
            collections[name_key(row[0])].append(row[0])
        candidates = []
        for raw in conn.execute(
            "SELECT * FROM agent_current_downloads WHERE parent_source='analysis-results' "
            "AND hidden=0 ORDER BY short_title,download_id"
        ):
            row = dict(raw)
            if row.get("controlled_access"):
                continue
            search = urlsplit(str(row.get("search_url") or ""))
            manifest = urlsplit(str(row.get("download_url") or ""))
            idc_routed = search.hostname == "portal.imaging.datacommons.cancer.gov"
            is_manifest = manifest.path.casefold().endswith(".tcia")
            is_dicom = "dicom" in str(row.get("file_types") or "").casefold()
            if is_manifest or (idc_routed and is_dicom):
                row["idc_routed"] = idc_routed
                candidates.append(row)
        return candidates, dict(collections)


def resolve_scopes(
    snapshot_db: Path, *, index_frame: Any, idc_version: str,
    fetch_manifest: Callable[[str], bytes],
    fetch_missing_series: Callable[[list[str]], tuple[list[dict[str, str]], list[str]]] | None = None,
) -> dict[str, Any]:
    """Resolve exact UID sets; incomplete scopes cannot authorize inheritance.

    The caller decides whether unresolved legacy NBIA downloads require a new
    release review. Unresolved IDC-routed downloads are always build failures.
    No per-patient identifiers or raw URLs appear in failure diagnostics.
    """
    candidates, collections = download_candidates(snapshot_db)
    report: dict[str, Any] = {
        "policy_version": POLICY_VERSION, "idc_version": idc_version,
        "downloads": [], "relationships": [], "blocked": False,
    }
    requested: set[str] = set()
    selections = []
    for download in candidates:
        diagnostic = {
            "short_title": download["short_title"], "download_id": str(download["download_id"]),
            "idc_routed": download["idc_routed"], "status": "pending",
        }
        report["downloads"].append(diagnostic)
        try:
            url = str(download["download_url"] or "")
            parsed = urlsplit(url)
            # Only public TCIA-hosted manifest metadata is harvested here.
            if (parsed.scheme != "https" or parsed.username or parsed.password
                    or parsed.hostname not in {"www.cancerimagingarchive.net", "cancerimagingarchive.net"}
                    or not parsed.path.casefold().endswith(".tcia")):
                raise ValueError("unsupported public TCIA manifest route")
            data = fetch_manifest(url)
            uids = parse_tcia_manifest(data)
            diagnostic.update(
                manifest_sha256=hashlib.sha256(data).hexdigest(),
                series_set_sha256=hashlib.sha256(canonical_json(uids).encode()).hexdigest(),
                requested_series=len(uids),
            )
            selections.append((download, diagnostic, uids))
            requested.update(uids)
        except Exception:
            # Fetch exceptions may contain credential-bearing redirect URLs.
            diagnostic["status"] = "manifest_unavailable_or_invalid"
            report["blocked"] |= bool(download["idc_routed"])
    columns = {"SeriesInstanceUID", "collection_id", "PatientID"}
    if not columns.issubset(set(index_frame.columns)):
        raise ValueError("IDC index lacks series, collection, or patient identifiers")
    rows = index_frame.loc[
        index_frame["SeriesInstanceUID"].isin(requested), sorted(columns)
    ].to_dict(orient="records")
    matches: dict[str, set[tuple[str, str]]] = defaultdict(set)
    idc_series = {str(row["SeriesInstanceUID"]) for row in rows}
    for row in rows:
        collection, patient = row["collection_id"], row["PatientID"]
        if not isinstance(collection, str) or not isinstance(patient, str) or not patient.strip():
            continue
        titles = collections.get(name_key(collection), [])
        if len(titles) == 1:
            matches[str(row["SeriesInstanceUID"])].add((titles[0], patient.strip()))
    nbia_hashes: list[str] = []
    missing = sorted({uid for _, _, uids in selections for uid in uids if uid not in idc_series})
    if missing and fetch_missing_series is not None:
        try:
            fallback_rows, nbia_hashes = fetch_missing_series(missing)
            if not nbia_hashes or any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in nbia_hashes):
                raise ValueError("NBIA metadata evidence hashes are missing or malformed")
            if any(row["SeriesInstanceUID"] not in missing for row in fallback_rows):
                raise ValueError("NBIA returned an out-of-scope series")
            fallback_matches: dict[str, set[tuple[str, str]]] = defaultdict(set)
            for row in fallback_rows:
                titles = collections.get(name_key(row["Collection"]), [])
                patient = row["PatientID"].strip()
                if len(titles) == 1 and patient:
                    fallback_matches[row["SeriesInstanceUID"]].add((titles[0], patient))
            matches.update(fallback_matches)
        except Exception:
            report["nbia_fallback_status"] = "unavailable_or_invalid"
            nbia_hashes = []
    relationships: dict[tuple[str, str], dict[str, Any]] = {}
    if nbia_hashes:
        report["nbia_metadata_sha256"] = nbia_hashes
    for download, diagnostic, uids in selections:
        unresolved = [uid for uid in uids if len(matches.get(uid, set())) != 1]
        diagnostic.update(resolved_series=len(uids) - len(unresolved), unresolved_series=len(unresolved))
        absent_from_idc = sum(uid not in idc_series for uid in uids)
        diagnostic["idc_missing_series"] = absent_from_idc
        if absent_from_idc and nbia_hashes:
            # Bind only stable identity fields into fact provenance. Raw
            # response hashes remain in the component's audit metadata;
            # response ordering and unrelated NBIA fields cannot rekey facts.
            diagnostic["nbia_mapping_sha256"] = hashlib.sha256(canonical_json([
                [uid, sorted(matches.get(uid, set()))]
                for uid in uids if uid not in idc_series
            ]).encode()).hexdigest()
        if unresolved:
            diagnostic["status"] = "incomplete_or_ambiguous_series_mapping"
            report["blocked"] |= bool(download["idc_routed"])
            continue
        diagnostic["status"] = "resolved"
        patients_by_source: dict[str, set[str]] = defaultdict(set)
        for uid in uids:
            source, patient = next(iter(matches[uid]))
            patients_by_source[source].add(patient)
        diagnostic["source_collections"] = sorted(patients_by_source)
        for source, patients in patients_by_source.items():
            key = (str(download["short_title"]), source)
            relation = relationships.setdefault(key, {
                "target_short_title": key[0], "source_short_title": source,
                "participant_ids": set(), "downloads": [],
            })
            relation["participant_ids"].update(patients)
            relation["downloads"].append({
                "download_id": diagnostic["download_id"],
                "manifest_sha256": diagnostic["manifest_sha256"],
                "series_set_sha256": diagnostic["series_set_sha256"],
                "requested_series": len(uids), "source_participants": len(patients),
                "idc_missing_series": absent_from_idc,
                **({"nbia_mapping_sha256": diagnostic["nbia_mapping_sha256"]}
                   if "nbia_mapping_sha256" in diagnostic else {}),
            })
    incomplete_targets = {d["short_title"] for d in report["downloads"] if d["status"] != "resolved"}
    if any(d["idc_routed"] and d["short_title"] in incomplete_targets for d in report["downloads"]):
        report["blocked"] = True
    patient_sources: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (target, source), relation in relationships.items():
        for patient in relation["participant_ids"]:
            patient_sources[(target, " ".join(patient.split()).casefold())].add(source)
    ambiguous_targets = {
        target for (target, _), sources in patient_sources.items() if len(sources) > 1
    }
    if ambiguous_targets:
        report["blocked"] = True
        for diagnostic in report["downloads"]:
            if diagnostic["short_title"] in ambiguous_targets:
                diagnostic["status"] = "ambiguous_patient_namespace"
    for key in sorted(relationships):
        if key[0] in ambiguous_targets or key[0] in incomplete_targets:
            continue
        relation = relationships[key]
        relation["participant_ids"] = sorted(relation["participant_ids"])
        report["relationships"].append(relation)
    return report


def diagnostics_only(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key != "relationships"}
