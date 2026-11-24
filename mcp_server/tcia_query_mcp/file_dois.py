"""Exact, snapshot-local file-locator to dataset DOI resolution.

Locators are metadata keys only: this module never opens a supplied URL.
"""
from __future__ import annotations

import sqlite3
import re
from collections import defaultdict
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from .models import FileDOIsRequest
from .service import InvalidRequestError, ServiceUnavailableError

MAX_MATCHES = 20


def _doi(value: str | None) -> str:
    value = str(value or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
            break
    return value.lower() if value.startswith("10.") and "/" in value else ""


def _keys(kind: str, value: str) -> tuple[str, ...]:
    if value != value.strip() or any(ord(c) < 32 for c in value):
        raise InvalidRequestError("File locators must not contain whitespace padding or control characters")
    if kind == "image_url":
        try:
            url = urlsplit(value)
            if (url.scheme not in {"http", "https"} or not url.hostname
                or url.username or url.password or url.port is not None
                or url.query or url.fragment):
                raise ValueError
        except ValueError:
            raise InvalidRequestError("Supply a public file URL without credentials, port, query or fragment") from None
        # Only the already-supported public PathDB file inventory is resolved.
        # HTTP/HTTPS are the two representations emitted by public_asset_download.
        if url.hostname.lower() != "pathdb.cancerimagingarchive.net":
            return ()
        return tuple(urlunsplit((scheme, url.hostname.lower(), url.path, "", "")) for scheme in ("http", "https"))
    if kind == "drs_uri":
        try:
            url = urlsplit(value)
        except ValueError:
            raise InvalidRequestError("Supply a valid public DRS URI") from None
        if (url.scheme != "drs" or not url.netloc or not url.path.strip("/")
            or url.username or url.password or url.query or url.fragment):
            raise InvalidRequestError("Supply a DRS URI without credentials, query or fragment")
    elif kind == "series_instance_uid":
        if len(value) > 64 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value) is None:
            raise InvalidRequestError("Supply a valid DICOM series UID")
    elif any(c.isspace() for c in value) or "://" in value or "?" in value or "#" in value:
        raise InvalidRequestError("Supply a public file identifier, not a URL")
    return (value,)


def resolve(service, locators):
    try:
        request = FileDOIsRequest.model_validate({"locators": locators})
    except ValidationError:
        raise InvalidRequestError("Supply 1 to 200 typed file locators with unique IDs") from None
    if len({item.id for item in request.locators}) != len(request.locators):
        raise InvalidRequestError("File locator IDs must be unique within a request")
    keys = {item.id: _keys(item.kind, item.value) for item in request.locators}
    try:
        snapshot_generation = service._file_generation(service.snapshot_db)
    except OSError:
        raise ServiceUnavailableError("Dataset publication metadata is unavailable") from None
    bundle = (service.bundle_info().get("v2_bundle") or {})
    try:
        with service._connect_snapshot() as conn:
            datasets = defaultdict(list)
            for row in conn.execute("SELECT dataset_type,short_title,title,doi,link FROM agent_datasets WHERE COALESCE(hidden,0)=0"):
                datasets[(row["dataset_type"].casefold(), row["short_title"].casefold())].append(dict(row))
    except (sqlite3.Error, OSError) as exc:
        raise ServiceUnavailableError("Dataset publication metadata is unavailable") from exc

    groups = defaultdict(list)
    for item in request.locators:
        groups[item.kind].append(item)
    candidates = defaultdict(list)
    failures = {}
    generations = {}
    for kind, items in groups.items():
        values = sorted({key for item in items for key in keys[item.id]})
        if not values:
            continue
        placeholders = ",".join("?" for _ in values)
        path = service.public_non_dicom_db if kind == "image_url" else service.controlled_db
        try:
            before = service._file_generation(path)
            if kind == "image_url":
                from scripts.tcia_locator_indexes import PATHDB_URL_PREDICATE
                sql = ("SELECT DISTINCT l.access_url AS locator,a.dataset_type,a.short_title,'' AS doi "
                       "FROM public_non_dicom_locations l INDEXED BY idx_pnd_pathdb_file_url "
                       "JOIN public_non_dicom_assets a ON a.asset_id=l.asset_id "
                       f"WHERE {PATHDB_URL_PREDICATE} AND l.access_url IN ({placeholders}) "
                       "AND a.source_system='tcia_pathdb' AND a.asset_granularity='file'")
                connect = service._connect_public_non_dicom
                source = "public_non_dicom_metadata.sqlite"
            else:
                column, index = {
                    "drs_uri": ("drs_uri", "idx_controlled_files_drs"),
                    "file_id": ("file_id", "idx_controlled_files_file_id"),
                    "series_instance_uid": ("series_instance_uid", "idx_controlled_files_series"),
                }[kind]
                sql = (f"SELECT DISTINCT {column} AS locator,dataset_type,short_title,doi "
                       f"FROM controlled_files INDEXED BY {index} WHERE {column} IN ({placeholders})")
                connect = service._connect_controlled
                source = "controlled_access_metadata.sqlite"
            with connect() as conn:
                rows = conn.execute(sql, values).fetchall()
            if before != service._file_generation(path):
                raise ServiceUnavailableError("File metadata changed during resolution; retry")
            generations[path] = before
            by_key = defaultdict(list)
            for row in rows:
                identities = datasets.get((row["dataset_type"].casefold(), row["short_title"].casefold()), [])
                valid = [dataset for dataset in identities if _doi(dataset["doi"])]
                if row["doi"] and valid and _doi(row["doi"]) not in {_doi(dataset["doi"]) for dataset in valid}:
                    raise ServiceUnavailableError("File and dataset DOI metadata disagree")
                for dataset in valid:
                    by_key[row["locator"]].append({
                        "dataset_type": dataset["dataset_type"], "short_title": dataset["short_title"],
                        "title": dataset["title"], "doi": _doi(dataset["doi"]), "tcia_page": dataset["link"],
                        "source_artifact": source, "match_basis": kind,
                    })
            for item in items:
                unique = {}
                for key in keys[item.id]:
                    for match in by_key[key]:
                        unique[(match["dataset_type"], match["short_title"], match["doi"])] = match
                candidates[item.id] = sorted(unique.values(), key=lambda x: (x["dataset_type"],x["short_title"],x["doi"]))
        except (sqlite3.Error, OSError, ServiceUnavailableError):
            failures[kind] = "Required file metadata/index is unavailable or inconsistent; install the resolver-enabled research_detail artifacts"

    results = []
    for item in request.locators:
        matches = candidates[item.id]
        status = "unavailable" if item.kind in failures else "matched" if len(matches)==1 else "ambiguous" if matches else "unmatched"
        results.append({"id":item.id,"kind":item.kind,"value":item.value,"status":status,
                        "matches":matches[:MAX_MATCHES],"truncated":len(matches)>MAX_MATCHES,
                        "detail":failures.get(item.kind)})
    generations[service.snapshot_db] = snapshot_generation
    try:
        changed = any(service._file_generation(path) != generation for path, generation in generations.items())
    except OSError:
        changed = True
    if changed:
        raise ServiceUnavailableError("Metadata changed during resolution; retry")
    return {"results":results,"count":len(results),"release_fingerprint":bundle.get("release_fingerprint"),
            "generated_at_utc":bundle.get("generated_at_utc")}
