"""Shared participant-to-public-image planning over TCIA identity and provider metadata."""

from collections import defaultdict
import math
from time import monotonic

from .idc_metadata import IdcMetadataClient, MetadataUnavailable
from .service import InvalidRequestError, ServiceUnavailableError

MAX_ITEMS = 10000


def _doi(value):
    value = str(value or "").strip()
    if value.startswith(("https://doi.org/", "http://doi.org/", "https://dx.doi.org/")):
        value = value.split("doi.org/", 1)[1]
    return value.lower() if value.startswith("10.") and "/" in value else None


def _kind(value):
    return str(value).casefold().replace("_", " ")


def plan(service, participant_keys, *, modality, file_format):
    if (
        not isinstance(participant_keys, list)
        or not 1 <= len(participant_keys) <= 100
        or any(
            not isinstance(k, str) or not k.strip() or len(k) > 300
            for k in participant_keys
        )
    ):
        raise InvalidRequestError("Supply between 1 and 100 TCIA participant keys")
    if not isinstance(modality, str) or not modality.strip() or len(modality) > 80:
        raise InvalidRequestError("modality must be a nonempty string")
    if not isinstance(file_format, str) or file_format.casefold() not in {
        "dicom",
        "non-dicom",
    }:
        raise InvalidRequestError("file_format must be DICOM or non-DICOM")
    deadline = monotonic() + 60
    dicom = file_format.casefold() == "dicom"
    paths = [service.snapshot_db, service.participant_db]
    if not dicom:
        paths.append(service.public_non_dicom_db)

    def generations():
        try:
            return [service._file_generation(p) for p in paths]
        except OSError:
            raise ServiceUnavailableError(
                "Required participant planning metadata is unavailable"
            ) from None

    before = generations()
    fingerprint = (service.bundle_info().get("v2_bundle") or {}).get(
        "release_fingerprint"
    )
    people = {}
    for key in sorted(set(participant_keys)):
        detail = service.get_participant(participant_key=key)
        people[key] = detail
    rows, issues, providers = [], [], {}

    def issue(person, reason):
        issues.append(
            {k: person[k] for k in ("participant_key", "short_title")}
            | {"participant_id": person["display_participant_id"], "reason": reason}
        )

    if dicom:
        groups = defaultdict(dict)
        dataset_cache = {}
        for key, detail in people.items():
            person = detail["participant"]
            ids = [
                r
                for r in detail["identifiers"]
                if r.get("managed_system") == "crdc_idc"
                and isinstance(r.get("raw_identifier"), str)
                and r["raw_identifier"].strip()
            ]
            if person["short_title"] not in dataset_cache:
                dataset_cache[person["short_title"]] = service.get_dataset(
                    person["short_title"]
                )["datasets"]
            datasets = dataset_cache[person["short_title"]]
            matches = [
                d
                for d in datasets
                if _kind(d["dataset_type"]) == _kind(person["dataset_type"])
            ]
            doi = _doi(matches[0].get("doi")) if len(matches) == 1 else None
            if not ids or not doi:
                issue(person, "no_verified_public_dicom_identity_or_dataset_doi")
                continue
            groups[(person["dataset_type"], person["short_title"], doi)][key] = ids
        if service._idc_metadata is None:
            service._idc_metadata = IdcMetadataClient()
        for (kind, title, doi), members in groups.items():
            mapping = defaultdict(set)
            collections = set()
            for key, identifiers in members.items():
                for r in identifiers:
                    mapping[r["raw_identifier"]].add(key)
                    provenance = r.get("provenance_json") or {}
                    collections.update(
                        v
                        for v in provenance.get("source_collection_ids", [])
                        if isinstance(v, str)
                    )
            ambiguous = {
                key for owners in mapping.values() if len(owners) != 1 for key in owners
            }
            for key in ambiguous:
                issue(people[key]["participant"], "ambiguous_provider_identifier")
            mapping = {
                raw: next(iter(keys))
                for raw, keys in mapping.items()
                if len(keys) == 1 and not keys.intersection(ambiguous)
            }
            if not mapping:
                continue
            try:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise MetadataUnavailable(
                        "Participant image planning timed out; split the selection"
                    )
                result = service._idc_metadata.resolve(
                    doi,
                    list(mapping),
                    modality,
                    collection_ids=sorted(collections),
                    time_budget=remaining,
                )
            except MetadataUnavailable as exc:
                raise ServiceUnavailableError(str(exc)) from None
            version = result["version"]
            if "IDC" in providers and providers["IDC"] != version:
                raise ServiceUnavailableError(
                    "IDC version changed between participant groups; retry the plan"
                )
            providers["IDC"] = version
            for item in result["rows"]:
                # Validate injected adapters as well as the public HTTP implementation.
                if (
                    item.get("PatientID") not in mapping
                    or _doi(item.get("source_DOI")) != doi
                    or str(item.get("Modality", "")).casefold() != modality.casefold()
                    or (collections and item.get("collection_id") not in collections)
                ):
                    raise ServiceUnavailableError(
                        "Provider returned out-of-scope series"
                    )
                key = mapping[item["PatientID"]]
                person = people[key]["participant"]
                size = item.get("series_size_MB")
                size = (
                    int(float(size) * 1_000_000)
                    if isinstance(size, (int, float))
                    and math.isfinite(size)
                    and size >= 0
                    else None
                )
                rows.append(
                    {
                        "participant_key": key,
                        "participant_id": person["display_participant_id"],
                        "dataset_type": kind,
                        "short_title": title,
                        "provider": "IDC",
                        "provider_version": version["idc_index_data_version"],
                        "source_doi": item["source_DOI"],
                        "source_patient_id": item["PatientID"],
                        "collection_id": item["collection_id"],
                        "series_instance_uid": item["SeriesInstanceUID"],
                        "study_instance_uid": item["StudyInstanceUID"],
                        "modality": item["Modality"],
                        "file_format": "DICOM",
                        "asset_granularity": "series",
                        "instance_count": item["instanceCount"],
                        "size_bytes": size,
                        "license_label": item["license_short_name"],
                        "access_level": "open",
                        "download_method": "dicom_manifest",
                        "download_id": item["SeriesInstanceUID"],
                        "python_supported": True,
                        "download_representation": "public_dicom_series",
                    }
                )
    else:
        for key, detail in people.items():
            person = detail["participant"]
            cursor = None
            seen = set()
            while True:
                if monotonic() >= deadline:
                    raise ServiceUnavailableError(
                        "Participant image planning timed out; split the selection"
                    )
                page = service.find_public_non_dicom_assets(
                    short_titles=[person["short_title"]],
                    participant_id=person["display_participant_id"],
                    modalities=[modality],
                    limit=500,
                    cursor=cursor,
                )
                for asset in page["assets"]:
                    if (
                        _kind(asset.get("dataset_type"))
                        != _kind(person["dataset_type"])
                        or asset.get("short_title") != person["short_title"]
                        or str(asset.get("modality", "")).casefold()
                        != modality.casefold()
                        or str(asset.get("file_format", "")).casefold() == "dicom"
                    ):
                        continue
                    if (
                        asset.get("subject_id") != person["display_participant_id"]
                        or asset.get("subject_id_namespace")
                        != "tcia_dataset:" + person["short_title"]
                    ):
                        issue(person, "unverified_participant_namespace")
                        continue
                    if asset.get("access_level") not in {
                        "open",
                        "public",
                        "open_noncommercial",
                    }:
                        issue(person, "controlled_or_unresolved_access")
                        continue
                    supported = (
                        asset.get("asset_granularity") == "file"
                        and bool(asset.get("imageUrl"))
                        and asset.get("access_level")
                        in {"open", "public", "open_noncommercial"}
                    )
                    rows.append(
                        {
                            **asset,
                            "participant_key": key,
                            "participant_id": person["display_participant_id"],
                            "provider": "TCIA",
                            "provider_version": fingerprint,
                            "python_supported": supported,
                            "file_name": asset.get("download_file_name")
                            or asset.get("file_name"),
                            "download_id": asset["asset_id"],
                            "download_method": "direct_file",
                        }
                    )
                    if not supported:
                        issue(person, "no_explicit_public_file_download_route")
                if not page["has_more"]:
                    break
                cursor = page.get("next_cursor")
                if not cursor or cursor in seen:
                    raise ServiceUnavailableError(
                        "Public file inventory pagination is incomplete"
                    )
                seen.add(cursor)
                if len(rows) > MAX_ITEMS:
                    raise InvalidRequestError(
                        "Selection exceeds the plan limit; split the participants"
                    )
    unique = {}
    for row in rows:
        token = (
            row["participant_key"],
            row.get("series_instance_uid") or row.get("asset_id"),
        )
        if token in unique and unique[token] != row:
            raise ServiceUnavailableError("Provider returned conflicting image records")
        unique[token] = row
    rows = sorted(
        unique.values(),
        key=lambda r: (
            r["participant_key"],
            r.get("series_instance_uid") or r["asset_id"],
        ),
    )
    if len(rows) > MAX_ITEMS:
        raise InvalidRequestError(
            "Selection exceeds the plan limit; split the participants"
        )
    if before != generations() or fingerprint != (
        service.bundle_info().get("v2_bundle") or {}
    ).get("release_fingerprint"):
        raise ServiceUnavailableError(
            "TCIA metadata changed during planning; retry the plan"
        )
    affected = {r["participant_key"] for r in issues}
    coverage = []
    for key, detail in people.items():
        p = detail["participant"]
        matches = [r for r in rows if r["participant_key"] == key]
        coverage.append(
            {
                "participant_key": key,
                "dataset_type": p["dataset_type"],
                "short_title": p["short_title"],
                "participant_id": p["display_participant_id"],
                "matching_items": len(matches),
                "downloadable_items": sum(r["python_supported"] for r in matches),
                "state": (
                    "unresolved"
                    if key in affected
                    else "matched" if matches else "no_matches"
                ),
            }
        )
    return {
        "plan_version": 1,
        "rows": rows,
        "coverage": coverage,
        "unresolved": issues,
        "providers": providers,
        "scope": {
            "modality": modality,
            "file_format": "DICOM" if dicom else "non-DICOM",
            "requested_participants": len(people),
            "selected_items": len(rows),
            "metadata_snapshot": fingerprint,
            "access_levels": ["open"],
            "scope_note": "Exact dataset-scoped public selections; controlled data is excluded.",
        },
        "complete": not issues,
    }
