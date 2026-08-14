#!/usr/bin/env python3
"""Discover source-supported participant IDs in public non-DICOM file paths.

The discovery pass is metadata-only. It compares Aspera package paths with
dataset-scoped identifiers already present in official clinical/supporting
tables or another TCIA-managed representation such as PathDB. It also records
WordPress text that describes patient/subject/case naming conventions.

Only exact, delimiter-bounded identifier matches are proposed. Purely numeric
identifiers must match a complete path component or filename stem. Automatic
acceptance is evaluated per download and requires complete file coverage,
zero ambiguous matches, and an authoritative identifier source.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PUBLIC_DB = ROOT / "outputs/metadata_v2_crosswalk_validation/public_non_dicom_metadata.sqlite"
DEFAULT_CLINICAL_DB = ROOT / "cache/clinical_metadata.sqlite"
DEFAULT_SNAPSHOT_DB = ROOT / "cache/tcia_snapshot.sqlite"
DEFAULT_OUT_DIR = ROOT / "outputs/participant_crosswalk_discovery"

SUMMARY_FIELDS = [
    "dataset_type", "short_title", "download_id", "source_system",
    "file_assets", "matched_files", "unmatched_files", "ambiguous_files",
    "matched_participants", "coverage", "identifier_sources",
    "official_supporting_table", "wordpress_naming_evidence",
    "wordpress_evidence_url", "decision", "decision_reason",
]

PROPOSAL_FIELDS = [
    "dataset_type", "short_title", "download_id", "asset_id", "subject_id",
    "raw_subject_id", "subject_id_namespace", "participant_link_status",
    "package_path", "file_name", "file_format", "media_kind",
    "imaging_domain", "modality", "object_role", "size_bytes",
    "source_system", "source_url", "crosswalk_source_url",
    "crosswalk_method", "crosswalk_confidence", "reviewer_note",
    "dicom_series_instance_uid", "raw_values_json", "provenance_json",
    "quality_flag_json",
]


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalized_token(value: str) -> str:
    return value.strip().casefold()


def candidate_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))


def build_candidate_index(candidates: set[str]) -> tuple[dict[str, str], dict[tuple[str, ...], list[str]], int]:
    exact = {normalized_token(item): item for item in candidates if len(normalized_token(item)) >= 3}
    token_index: dict[tuple[str, ...], list[str]] = defaultdict(list)
    max_tokens = 1
    for item in candidates:
        normalized = normalized_token(item)
        if len(normalized) < 3 or normalized.isdigit():
            continue
        tokens = candidate_tokens(item)
        if tokens:
            token_index[tokens].append(item)
            max_tokens = max(max_tokens, len(tokens))
    return exact, token_index, max_tokens


def match_path(
    path: str,
    candidate_index: tuple[dict[str, str], dict[tuple[str, ...], list[str]], int],
) -> list[tuple[str, str]]:
    """Return safe matches without scanning every candidate for every path."""
    exact, token_index, max_tokens = candidate_index

    path_norm = path.casefold().replace("\\", "/")
    components = [item for item in path_norm.split("/") if item]
    stems = [re.sub(r"(?:\.[^.]+)+$", "", item) for item in components]
    found: dict[str, str] = {}
    for component in components:
        if component in exact:
            found[exact[component]] = "exact_path_component"
    for stem in stems:
        if stem in exact:
            found.setdefault(exact[stem], "exact_filename_stem")
    for component in components:
        tokens = candidate_tokens(component)
        for width in range(1, min(max_tokens, len(tokens)) + 1):
            for start in range(0, len(tokens) - width + 1):
                for item in token_index.get(tokens[start:start + width], []):
                    found.setdefault(item, "delimiter_bounded_path_token")
    return list(found.items())


def wordpress_evidence(text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", clean_text(text))
    keywords = re.compile(
        r"(?:file ?name|naming|folder|director|patient id|subject id|case id|per patient|per subject|per case)",
        re.IGNORECASE,
    )
    hits = [sentence for sentence in sentences if keywords.search(sentence)]
    return clean_text(" ".join(hits[:3]))[:1200]


def load_identifiers(public: sqlite3.Connection, clinical: sqlite3.Connection) -> tuple[dict[str, set[str]], dict[str, dict[str, set[str]]], dict[str, str]]:
    identifiers: dict[str, set[str]] = defaultdict(set)
    sources: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    evidence_urls: dict[str, str] = {}

    for row in public.execute(
        """
        SELECT short_title, subject_id, source_system
        FROM public_non_dicom_assets
        WHERE COALESCE(subject_id, '') <> ''
        """
    ):
        title, subject_id, system = row
        identifiers[title].add(subject_id)
        sources[title][subject_id].add(f"linked_{system}")

    official_titles = set()
    for row in clinical.execute(
        """
        SELECT short_title, source_url
        FROM clinical_sources
        WHERE source_kind = 'tcia_clinical_download'
        """
    ):
        official_titles.add(row[0])
        if row[1] and row[0] not in evidence_urls:
            evidence_urls[row[0]] = row[1]

    for title, subject_id in clinical.execute(
        "SELECT short_title, subject_id FROM agent_clinical_all_subjects"
    ):
        if title not in official_titles or not subject_id:
            continue
        identifiers[title].add(subject_id)
        sources[title][subject_id].add("official_clinical_table")

    return identifiers, sources, evidence_urls


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def discover(public_db: Path, clinical_db: Path, snapshot_db: Path, out_dir: Path) -> dict[str, Any]:
    public = sqlite3.connect(public_db)
    clinical = sqlite3.connect(clinical_db)
    snapshot = sqlite3.connect(snapshot_db)
    public.row_factory = clinical.row_factory = snapshot.row_factory = sqlite3.Row
    identifiers, id_sources, evidence_urls = load_identifiers(public, clinical)

    wordpress: dict[str, tuple[str, str]] = {}
    for row in snapshot.execute(
        """
        SELECT short_title, link,
               COALESCE(summary, '') || ' ' || COALESCE(abstract, '') || ' ' || COALESCE(detailed_description, '') AS text
        FROM agent_datasets WHERE hidden = 0
        """
    ):
        wordpress[row["short_title"]] = (row["link"], wordpress_evidence(row["text"]))

    groups: dict[tuple[str, str, str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in public.execute(
        """
        SELECT * FROM public_non_dicom_assets
        WHERE asset_granularity = 'file'
          AND source_system = 'tcia_aspera'
          AND participant_link_status IN ('dataset_only', 'unavailable')
        ORDER BY lower(short_title), download_id, package_path
        """
    ):
        key = (row["dataset_type"], row["short_title"], str(row["download_id"] or ""), row["source_system"])
        groups[key].append(row)

    summaries: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    candidate_audit: list[dict[str, Any]] = []

    for key, assets in groups.items():
        dataset_type, title, download_id, source_system = key
        candidates = identifiers.get(title, set())
        candidate_index = build_candidate_index(candidates)
        matched: list[tuple[sqlite3.Row, str, str]] = []
        unmatched = ambiguous = 0
        source_labels: set[str] = set()
        for asset in assets:
            path = str(asset["package_path"] or asset["file_name"] or "")
            matches = match_path(path, candidate_index)
            # Prefer the longest identifier only when shorter matches are strict substrings.
            if len(matches) > 1:
                matches.sort(key=lambda item: len(item[0]), reverse=True)
                longest = matches[0][0].casefold()
                if all(item[0].casefold() in longest for item in matches[1:]):
                    matches = matches[:1]
            if len(matches) == 1:
                subject_id, match_method = matches[0]
                matched.append((asset, subject_id, match_method))
                source_labels.update(id_sources[title][subject_id])
                candidate_audit.append({
                    "asset_id": asset["asset_id"], "short_title": title,
                    "download_id": download_id, "package_path": path,
                    "subject_id": subject_id, "match_method": match_method,
                    "identifier_sources": ";".join(sorted(id_sources[title][subject_id])),
                })
            elif matches:
                ambiguous += 1
            else:
                unmatched += 1

        file_count = len(assets)
        coverage = len(matched) / file_count if file_count else 0.0
        wp_url, wp_text = wordpress.get(title, ("", ""))
        has_official_table = any("official_clinical_table" in id_sources[title][sid] for _, sid, _ in matched)
        has_linked_representation = any(label.startswith("linked_") for label in source_labels)
        numeric_substring = any(sid.isdigit() and method not in {"exact_path_component", "exact_filename_stem"} for _, sid, method in matched)
        accepted = (
            file_count > 0 and coverage == 1.0 and ambiguous == 0 and not numeric_substring
            and (has_official_table or has_linked_representation)
        )
        if accepted:
            decision = "auto_accept"
            reason = "All file assets matched one dataset-scoped identifier with no ambiguity."
        elif matched:
            decision = "review_candidates"
            reason = "Some paths matched, but complete unambiguous download coverage was not established."
        else:
            decision = "no_match"
            reason = "No safe exact or delimiter-bounded identifier match was found."

        summaries.append({
            "dataset_type": dataset_type, "short_title": title,
            "download_id": download_id, "source_system": source_system,
            "file_assets": file_count, "matched_files": len(matched),
            "unmatched_files": unmatched, "ambiguous_files": ambiguous,
            "matched_participants": len({sid for _, sid, _ in matched}),
            "coverage": round(coverage, 6),
            "identifier_sources": ";".join(sorted(source_labels)),
            "official_supporting_table": "yes" if has_official_table else "no",
            "wordpress_naming_evidence": wp_text,
            "wordpress_evidence_url": wp_url,
            "decision": decision, "decision_reason": reason,
        })

        if not accepted:
            continue
        for asset, subject_id, match_method in matched:
            per_id_sources = sorted(id_sources[title][subject_id])
            crosswalk_url = evidence_urls.get(title) or wp_url
            proposals.append({
                "dataset_type": dataset_type, "short_title": title,
                "download_id": download_id, "asset_id": asset["asset_id"],
                "subject_id": subject_id, "raw_subject_id": subject_id,
                "subject_id_namespace": f"tcia_dataset:{title}",
                "participant_link_status": "automated_source_crosswalk",
                "package_path": asset["package_path"] or "",
                "file_name": asset["file_name"] or "",
                "file_format": asset["file_format"], "media_kind": asset["media_kind"],
                "imaging_domain": asset["imaging_domain"], "modality": asset["modality"] or "",
                "object_role": asset["object_role"], "size_bytes": asset["size_bytes"] or "",
                "source_system": asset["source_system"], "source_url": asset["source_url"] or "",
                "crosswalk_source_url": crosswalk_url,
                "crosswalk_method": f"automated_{match_method}_to_dataset_identifier",
                "crosswalk_confidence": "high",
                "reviewer_note": "Automated metadata-only match; complete download coverage and zero ambiguous paths.",
                "dicom_series_instance_uid": "",
                "raw_values_json": json.dumps({"source_asset_id": asset["asset_id"]}, sort_keys=True),
                "provenance_json": json.dumps({
                    "discovery_method": match_method,
                    "identifier_sources": per_id_sources,
                    "wordpress_naming_evidence": wp_text,
                }, sort_keys=True),
                "quality_flag_json": "{}",
            })

    summaries.sort(key=lambda row: (row["decision"] != "auto_accept", -int(row["file_assets"]), row["short_title"].casefold()))
    proposals.sort(key=lambda row: (row["short_title"].casefold(), row["download_id"], row["package_path"]))
    candidate_audit.sort(key=lambda row: (row["short_title"].casefold(), row["download_id"], row["package_path"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "dataset_summary.csv", SUMMARY_FIELDS, summaries)
    write_csv(out_dir / "auto_crosswalk_proposals.csv", PROPOSAL_FIELDS, proposals)
    write_csv(
        out_dir / "candidate_match_audit.csv",
        ["asset_id", "short_title", "download_id", "package_path", "subject_id", "match_method", "identifier_sources"],
        candidate_audit,
    )
    result = {
        "schema_version": 1,
        "public_metadata_db": str(public_db),
        "clinical_metadata_db": str(clinical_db),
        "snapshot_db": str(snapshot_db),
        "downloads_checked": len(summaries),
        "datasets_checked": len({row["short_title"] for row in summaries}),
        "auto_accepted_downloads": sum(row["decision"] == "auto_accept" for row in summaries),
        "auto_accepted_datasets": len({row["short_title"] for row in summaries if row["decision"] == "auto_accept"}),
        "proposal_rows": len(proposals),
        "candidate_rows": len(candidate_audit),
    }
    (out_dir / "manifest.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    public.close(); clinical.close(); snapshot.close()
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--public-db", type=Path, default=DEFAULT_PUBLIC_DB)
    result.add_argument("--clinical-db", type=Path, default=DEFAULT_CLINICAL_DB)
    result.add_argument("--snapshot-db", type=Path, default=DEFAULT_SNAPSHOT_DB)
    result.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    return result


if __name__ == "__main__":
    args = parser().parse_args()
    print(json.dumps(discover(args.public_db, args.clinical_db, args.snapshot_db, args.out_dir), indent=2))
