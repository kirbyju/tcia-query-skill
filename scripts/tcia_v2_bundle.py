#!/usr/bin/env python3
"""Assemble and validate the complete TCIA metadata V2 release bundle."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Any

try:
    from tcia_snapshot import WEB_EXPORT_ASSETS, export_web_artifacts, validate_snapshot_file
except ModuleNotFoundError:  # Imported as scripts.tcia_v2_bundle by tests or another module.
    from scripts.tcia_snapshot import WEB_EXPORT_ASSETS, export_web_artifacts, validate_snapshot_file


BUNDLE_SCHEMA_VERSION = 2
BUNDLE_ARTIFACT = "tcia_metadata_v2_bundle"
BUNDLE_MANIFEST_ASSET = "tcia_metadata_v2_bundle_manifest.json"
DEFAULT_REPOSITORY = "kirbyju/tcia-query-skill"
DEFAULT_SOURCE_TAG = "tcia-snapshot-latest"
DEFAULT_RELEASE_TAG = "tcia-metadata-v2-latest"
PREVIEW_RELEASE_TAG = "tcia-metadata-v2-preview"
DEFAULT_INSTALL_DIR = Path(__file__).resolve().parents[1] / "cache" / DEFAULT_RELEASE_TAG
INSTALL_STATE_ASSET = "tcia_metadata_v2_install.json"
FULL_RELEASE_CONTRACT = "full"
STREAMLINED_RELEASE_CONTRACT = "streamlined"
STREAMLINED_CANDIDATE_CONTRACT = "streamlined_candidate"
RELEASE_CONTRACTS = (
    FULL_RELEASE_CONTRACT,
    STREAMLINED_RELEASE_CONTRACT,
    STREAMLINED_CANDIDATE_CONTRACT,
)

COMPONENTS = {
    "snapshot": {
        "database": "tcia_snapshot.sqlite.gz",
        "manifest": "tcia_snapshot_manifest.json",
        "category": "core",
        "default_download": True,
    },
    "nifti": {
        "database": "nifti_metadata.sqlite.gz",
        "manifest": "nifti_metadata_manifest.json",
        "category": "research_detail",
        "default_download": False,
    },
    "pathology": {
        "database": "pathology_metadata.sqlite.gz",
        "manifest": "pathology_metadata_manifest.json",
        "category": "research_detail",
        "default_download": False,
    },
    "controlled_access": {
        "database": "controlled_access_metadata.sqlite.gz",
        "manifest": "controlled_access_metadata_manifest.json",
        "category": "research_detail",
        "default_download": False,
    },
    "clinical": {
        "database": "clinical_metadata.sqlite.gz",
        "manifest": "clinical_metadata_manifest.json",
        "category": "research_detail",
        "default_download": False,
    },
    "public_non_dicom": {
        "database": "public_non_dicom_metadata.sqlite.gz",
        "manifest": "public_non_dicom_metadata_manifest.json",
        "category": "research_detail",
        "default_download": False,
    },
    "participant_inventory": {
        "database": "participant_inventory.sqlite.gz",
        "manifest": "participant_inventory_manifest.json",
        "category": "v2_core",
        "default_download": True,
    },
    "public_non_dicom_audit": {
        "database": "public_non_dicom_audit.sqlite.gz",
        "manifest": "public_non_dicom_audit_manifest.json",
        "category": "audit_support",
        "default_download": False,
    },
    "participant_inventory_audit": {
        "database": "participant_inventory_audit.sqlite.gz",
        "manifest": "participant_inventory_audit_manifest.json",
        "category": "audit_support",
        "default_download": False,
    },
}

EXTRA_ASSETS = {
    "clinical_qc_manual_review.csv": {
        "category": "audit_support",
        "default_download": False,
        "source": "source_release_copy",
    },
}

SOURCE_COMPONENTS = ("snapshot", "nifti", "pathology", "controlled_access", "clinical")
STREAMLINED_COMPONENTS = (
    "snapshot",
    "controlled_access",
    "clinical",
    "public_non_dicom",
    "participant_inventory",
    "public_non_dicom_audit",
    "participant_inventory_audit",
)
STREAMLINED_WEB_EXPORTS = {
    "agent_datasets.jsonl.gz",
    "agent_current_downloads.jsonl.gz",
}
PROFILE_ORDER = ("research_core", "research_detail", "audit_support", "compatibility_exports")
PROFILE_DEPENDENCIES = {
    "research_core": (),
    "research_detail": ("research_core",),
    "audit_support": ("research_core", "research_detail"),
    "compatibility_exports": ("research_core",),
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url) as response:
        return response.read()


def download_to_path(
    url: str,
    destination: Path,
    details: dict[str, Any],
    asset: str,
) -> None:
    """Stream one release asset to disk while validating size and SHA-256."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    downloaded_bytes = 0
    with urllib.request.urlopen(url) as response, destination.open("wb") as handle:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            handle.write(chunk)
            digest.update(chunk)
            downloaded_bytes += len(chunk)
    if downloaded_bytes != details.get("bytes"):
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded V2 asset byte-size mismatch: {asset}")
    if digest.hexdigest() != details.get("sha256"):
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded V2 asset SHA-256 mismatch: {asset}")


def decompress_gzip_to_path(
    compressed: Path,
    destination: Path,
    expected_sha256: str,
    asset: str,
) -> None:
    """Stream a gzip payload to disk while validating decompressed SHA-256."""
    digest = hashlib.sha256()
    try:
        with gzip.open(compressed, "rb") as source, destination.open("wb") as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target.write(chunk)
                digest.update(chunk)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"Cannot decompress V2 database {asset}: {exc}") from exc
    if not expected_sha256 or digest.hexdigest() != expected_sha256:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded V2 SQLite SHA-256 mismatch: {asset}")


def release_asset_url(repository: str, tag: str, asset: str) -> str:
    return f"https://github.com/{repository}/releases/download/{tag}/{asset}"


def database_assets() -> dict[str, str]:
    return {
        str(details["database"]): str(details["manifest"])
        for details in COMPONENTS.values()
    }


def installed_asset_name(asset: str) -> str:
    if asset in database_assets() and asset.endswith(".gz"):
        return asset[:-3]
    return asset


def component_names_for_contract(release_contract: str) -> tuple[str, ...]:
    if release_contract == FULL_RELEASE_CONTRACT:
        return tuple(COMPONENTS)
    if release_contract in (
        STREAMLINED_RELEASE_CONTRACT,
        STREAMLINED_CANDIDATE_CONTRACT,
    ):
        return STREAMLINED_COMPONENTS
    raise ValueError(f"Unknown V2 release contract: {release_contract}")


def expected_payload_assets(release_contract: str = FULL_RELEASE_CONTRACT) -> list[str]:
    if release_contract in (
        STREAMLINED_RELEASE_CONTRACT,
        STREAMLINED_CANDIDATE_CONTRACT,
    ):
        assets = set(STREAMLINED_WEB_EXPORTS)
        for name in STREAMLINED_COMPONENTS:
            assets.add(str(COMPONENTS[name]["database"]))
        return sorted(assets)
    if release_contract != FULL_RELEASE_CONTRACT:
        raise ValueError(f"Unknown V2 release contract: {release_contract}")
    assets = set(WEB_EXPORT_ASSETS)
    assets.update(EXTRA_ASSETS)
    for component in COMPONENTS.values():
        assets.add(component["database"])
        assets.add(component["manifest"])
    return sorted(assets)


def source_copy_assets() -> list[str]:
    assets = set(EXTRA_ASSETS)
    for name in SOURCE_COMPONENTS:
        details = COMPONENTS[name]
        assets.add(str(details["database"]))
        assets.add(str(details["manifest"]))
    return sorted(assets)


def asset_source(name: str) -> str:
    if name in WEB_EXPORT_ASSETS:
        return "generated_from_bundled_snapshot"
    if name.startswith("public_non_dicom_") or name.startswith(
        ("participant_inventory.", "participant_inventory_")
    ):
        return "v2_build"
    return "source_release_copy"


def asset_category(name: str) -> tuple[str, bool]:
    if name in WEB_EXPORT_ASSETS:
        return "core", name in {"agent_datasets.jsonl.gz", "agent_current_downloads.jsonl.gz"}
    if name in EXTRA_ASSETS:
        details = EXTRA_ASSETS[name]
        return str(details["category"]), bool(details["default_download"])
    for component in COMPONENTS.values():
        if name in {component["database"], component["manifest"]}:
            return str(component["category"]), bool(component["default_download"])
    raise KeyError(name)


def asset_profile(name: str) -> str:
    category, default_download = asset_category(name)
    if default_download:
        return "research_core"
    if category == "research_detail":
        return "research_detail"
    if category == "audit_support":
        return "audit_support"
    return "compatibility_exports"


def assets_for_profile(
    profile: str,
    *,
    include_dependencies: bool = True,
    release_contract: str = FULL_RELEASE_CONTRACT,
) -> list[str]:
    if profile not in PROFILE_ORDER:
        raise ValueError(f"Unknown V2 profile: {profile}")
    expected = expected_payload_assets(release_contract)
    selected = {name for name in expected if asset_profile(name) == profile}
    if include_dependencies:
        for dependency in PROFILE_DEPENDENCIES[profile]:
            selected.update(
                name for name in expected if asset_profile(name) == dependency
            )
    return sorted(selected)


def load_component_manifest(asset_dir: Path, component: str) -> dict[str, Any]:
    details = COMPONENTS[component]
    path = asset_dir / str(details["manifest"])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {component} manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Component manifest must be an object: {path}")
    return payload


def validate_component_assets(
    asset_dir: Path,
    component_names: tuple[str, ...] | None = None,
) -> dict[str, dict[str, Any]]:
    errors: list[str] = []
    components: dict[str, dict[str, Any]] = {}
    for name in component_names or tuple(COMPONENTS):
        details = COMPONENTS[name]
        database_path = asset_dir / str(details["database"])
        manifest_path = asset_dir / str(details["manifest"])
        if not database_path.is_file():
            errors.append(f"missing component database: {database_path.name}")
            continue
        if not manifest_path.is_file():
            errors.append(f"missing component manifest: {manifest_path.name}")
            continue
        manifest = load_component_manifest(asset_dir, name)
        actual_gzip_sha256 = file_sha256(database_path)
        expected_gzip_sha256 = manifest.get("gzip_sha256")
        if not expected_gzip_sha256:
            errors.append(f"{manifest_path.name} has no gzip_sha256")
        elif expected_gzip_sha256 != actual_gzip_sha256:
            errors.append(f"{database_path.name} does not match {manifest_path.name} gzip_sha256")
        components[name] = {
            "database_asset": database_path.name,
            "manifest_asset": manifest_path.name,
            "profile": asset_profile(database_path.name),
            "schema_version": manifest.get("schema_version"),
            "release_fingerprint": manifest.get("release_fingerprint"),
            "sqlite_sha256": manifest.get("sqlite_sha256"),
            "gzip_sha256": actual_gzip_sha256,
            "provenance": manifest.get("provenance"),
            "storage_contract": manifest.get("storage_contract"),
        }

    snapshot_manifest = components.get("snapshot")
    if snapshot_manifest:
        raw_snapshot_manifest = load_component_manifest(asset_dir, "snapshot")
        web_exports = raw_snapshot_manifest.get("web_exports") or {}
        for export_name in WEB_EXPORT_ASSETS:
            export_path = asset_dir / export_name
            if not export_path.is_file():
                errors.append(f"missing web export: {export_name}")
                continue
            expected = (web_exports.get(export_name) or {}).get("sha256")
            actual = file_sha256(export_path)
            if not expected:
                errors.append(f"snapshot manifest has no hash for {export_name}")
            elif expected != actual:
                errors.append(f"{export_name} does not match snapshot manifest")

    if errors:
        raise RuntimeError("; ".join(errors))
    return components


def validate_source_release(asset_dir: Path, release_json_path: Path) -> dict[str, Any]:
    release = json.loads(release_json_path.read_text(encoding="utf-8"))
    remote_assets = {asset.get("name"): asset for asset in release.get("assets") or []}
    errors: list[str] = []
    verified: dict[str, dict[str, Any]] = {}
    for name in source_copy_assets():
        local_path = asset_dir / name
        remote = remote_assets.get(name)
        if not local_path.is_file():
            errors.append(f"missing downloaded source asset: {name}")
            continue
        if not remote:
            errors.append(f"source release does not contain: {name}")
            continue
        actual = file_sha256(local_path)
        digest = str(remote.get("digest") or "")
        expected = digest.removeprefix("sha256:")
        if not expected:
            errors.append(f"source release has no SHA-256 digest for: {name}")
        elif expected != actual:
            errors.append(f"downloaded source asset does not match captured release digest: {name}")
        verified[name] = {
            "asset_id": remote.get("id"),
            "bytes": local_path.stat().st_size,
            "sha256": actual,
        }
    if errors:
        raise RuntimeError("; ".join(errors))
    return {
        "release_id": release.get("id"),
        "release_tag": release.get("tag_name"),
        "target_commitish": release.get("target_commitish"),
        "published_at": release.get("published_at"),
        "updated_at": release.get("updated_at"),
        "assets": verified,
    }


def build_bundle_manifest(
    asset_dir: Path,
    *,
    repository: str = DEFAULT_REPOSITORY,
    source_tag: str = DEFAULT_SOURCE_TAG,
    release_tag: str = DEFAULT_RELEASE_TAG,
    source_release_json: Path | None = None,
    producer_commit: str = "",
    producer_skill_version: str = "",
    release_channel: str = "stable",
    release_contract: str = FULL_RELEASE_CONTRACT,
) -> dict[str, Any]:
    if (
        release_contract == STREAMLINED_CANDIDATE_CONTRACT
        and release_channel != "candidate"
    ):
        raise ValueError("The streamlined contract must use release_channel=candidate")
    required = expected_payload_assets(release_contract)
    missing = [name for name in required if not (asset_dir / name).is_file()]
    if missing:
        raise RuntimeError("Missing bundle assets: " + ", ".join(missing))
    components = validate_component_assets(
        asset_dir, component_names_for_contract(release_contract)
    )
    source_release = (
        validate_source_release(asset_dir, source_release_json)
        if source_release_json is not None
        else {"release_tag": source_tag}
    )
    assets: dict[str, dict[str, Any]] = {}
    for name in required:
        path = asset_dir / name
        category, default_download = asset_category(name)
        assets[name] = {
            "bytes": path.stat().st_size,
            "category": category,
            "default_download": default_download,
            "profile": asset_profile(name),
            "sha256": file_sha256(path),
            "source": asset_source(name),
        }
    source_details = {"repository": repository, **source_release}
    fingerprint_payload = {
        "artifact": BUNDLE_ARTIFACT,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "release_channel": release_channel,
        "release_tag": release_tag,
        "release_contract": release_contract,
        "source": {"repository": repository, "release_tag": source_details.get("release_tag")},
        "producer": {
            "repository": repository,
            "commit": producer_commit,
            "skill_version": producer_skill_version,
        },
        "assets": {name: assets[name]["sha256"] for name in sorted(assets)},
    }
    return {
        "artifact": BUNDLE_ARTIFACT,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "release_channel": release_channel,
        "release_tag": release_tag,
        "release_contract": release_contract,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": source_details,
        "producer": {
            "repository": repository,
            "commit": producer_commit,
            "skill_version": producer_skill_version,
        },
        "asset_count": len(assets),
        "assets": assets,
        "components": components,
        "profiles": {
            profile: {
                "assets": assets_for_profile(
                    profile, release_contract=release_contract
                ),
                "depends_on": list(PROFILE_DEPENDENCIES[profile]),
            }
            for profile in PROFILE_ORDER
        },
        "release_fingerprint": hashlib.sha256(canonical_json(fingerprint_payload).encode("utf-8")).hexdigest(),
    }


def validate_bundle(asset_dir: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if manifest.get("artifact") != BUNDLE_ARTIFACT:
        errors.append("unexpected artifact identifier")
    if manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        errors.append("unexpected bundle schema version")
    release_contract = str(manifest.get("release_contract") or FULL_RELEASE_CONTRACT)
    try:
        expected_names = expected_payload_assets(release_contract)
    except ValueError as exc:
        errors.append(str(exc))
        expected_names = []
    manifest_assets = manifest.get("assets") or {}
    if sorted(manifest_assets) != expected_names:
        errors.append("bundle manifest asset names do not match the contract")
    try:
        expected_components = set(component_names_for_contract(release_contract))
    except ValueError:
        expected_components = set()
    if set(manifest.get("components") or {}) != expected_components:
        errors.append("bundle components do not match the release contract")
    profiles = manifest.get("profiles") or {}
    if sorted(profiles) != sorted(PROFILE_ORDER):
        errors.append("bundle manifest profiles do not match the contract")
    else:
        for profile in PROFILE_ORDER:
            if (profiles.get(profile) or {}).get("assets") != assets_for_profile(
                profile, release_contract=release_contract
            ):
                errors.append(f"bundle profile {profile} asset names do not match the contract")
    for name in expected_names:
        path = asset_dir / name
        if not path.is_file():
            errors.append(f"missing bundle asset: {name}")
            continue
        details = manifest_assets.get(name) or {}
        if details.get("sha256") != file_sha256(path):
            errors.append(f"bundle asset hash mismatch: {name}")
        if details.get("bytes") != path.stat().st_size:
            errors.append(f"bundle asset size mismatch: {name}")
    for component, details in (manifest.get("components") or {}).items():
        database_asset = str((details or {}).get("database_asset") or "")
        database_path = asset_dir / database_asset
        if not database_path.is_file():
            errors.append(f"missing component database: {database_asset}")
            continue
        if file_sha256(database_path) != (details or {}).get("gzip_sha256"):
            errors.append(f"component gzip hash mismatch: {component}")
    source = manifest.get("source") or {}
    fingerprint_payload = {
        "artifact": manifest.get("artifact"),
        "schema_version": manifest.get("schema_version"),
        "release_channel": manifest.get("release_channel"),
        "release_tag": manifest.get("release_tag"),
        "release_contract": release_contract,
        "source": {"repository": source.get("repository"), "release_tag": source.get("release_tag")},
        "producer": manifest.get("producer"),
        "assets": {name: (manifest_assets.get(name) or {}).get("sha256") for name in sorted(manifest_assets)},
    }
    if "release_contract" not in manifest:
        fingerprint_payload.pop("release_contract", None)
    expected_fingerprint = hashlib.sha256(canonical_json(fingerprint_payload).encode("utf-8")).hexdigest()
    if manifest.get("release_fingerprint") != expected_fingerprint:
        errors.append("bundle release fingerprint mismatch")
    return {
        "ok": not errors,
        "errors": errors,
        "asset_count": len(manifest_assets),
        "release_fingerprint": manifest.get("release_fingerprint"),
    }


def validate_published_release(manifest_path: Path, release_json_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    release = json.loads(release_json_path.read_text(encoding="utf-8"))
    remote_assets = {asset.get("name"): asset for asset in release.get("assets") or []}
    expected_names = sorted([*manifest.get("assets", {}), BUNDLE_MANIFEST_ASSET])
    errors: list[str] = []
    if sorted(remote_assets) != expected_names:
        errors.append("published release asset names do not match the bundle contract")
    for name, details in (manifest.get("assets") or {}).items():
        remote = remote_assets.get(name) or {}
        digest = str(remote.get("digest") or "").removeprefix("sha256:")
        if digest != details.get("sha256"):
            errors.append(f"published release digest mismatch: {name}")
        if remote.get("size") != details.get("bytes"):
            errors.append(f"published release size mismatch: {name}")
    bundle_remote = remote_assets.get(BUNDLE_MANIFEST_ASSET) or {}
    bundle_digest = str(bundle_remote.get("digest") or "").removeprefix("sha256:")
    if bundle_digest != file_sha256(manifest_path):
        errors.append(f"published release digest mismatch: {BUNDLE_MANIFEST_ASSET}")
    return {
        "ok": not errors,
        "errors": errors,
        "asset_count": len(remote_assets),
        "release_tag": release.get("tag_name"),
    }


def changed_payload_assets(current_manifest_path: Path, previous_manifest_path: Path | None = None) -> list[str]:
    current = json.loads(current_manifest_path.read_text(encoding="utf-8"))
    current_assets = current.get("assets") or {}
    previous_assets: dict[str, Any] = {}
    if previous_manifest_path is not None and previous_manifest_path.is_file():
        previous = json.loads(previous_manifest_path.read_text(encoding="utf-8"))
        previous_assets = previous.get("assets") or {}
    return sorted(
        name
        for name, details in current_assets.items()
        if (previous_assets.get(name) or {}).get("sha256") != details.get("sha256")
    )


def materialize_bundle(
    asset_dir: Path, manifest_path: Path, out_dir: Path
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = validate_manifest_contract(manifest)
    if errors:
        raise RuntimeError("Invalid V2 bundle manifest: " + "; ".join(errors))
    if out_dir.exists():
        raise FileExistsError(f"Bundle output already exists: {out_dir}")
    out_dir.mkdir(parents=True)
    names = sorted([*(manifest.get("assets") or {}), BUNDLE_MANIFEST_ASSET])
    for name in names:
        source = manifest_path if name == BUNDLE_MANIFEST_ASSET else asset_dir / name
        if not source.is_file():
            raise FileNotFoundError(source)
        shutil.copy2(source, out_dir / name)
    return {
        "out_dir": str(out_dir),
        "asset_count": len(names),
        "release_contract": manifest.get("release_contract", FULL_RELEASE_CONTRACT),
    }


def validate_manifest_contract(manifest: dict[str, Any]) -> list[str]:
    """Validate the self-contained bundle contract before downloading payloads."""
    errors: list[str] = []
    if manifest.get("artifact") != BUNDLE_ARTIFACT:
        errors.append("unexpected artifact identifier")
    if manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        errors.append("unexpected bundle schema version")
    release_contract = str(manifest.get("release_contract") or FULL_RELEASE_CONTRACT)
    try:
        expected_names = expected_payload_assets(release_contract)
    except ValueError as exc:
        errors.append(str(exc))
        expected_names = []
    manifest_assets = manifest.get("assets") or {}
    if sorted(manifest_assets) != expected_names:
        errors.append("bundle manifest asset names do not match the contract")
    try:
        expected_components = set(component_names_for_contract(release_contract))
    except ValueError:
        expected_components = set()
    if set(manifest.get("components") or {}) != expected_components:
        errors.append("bundle components do not match the release contract")
    profiles = manifest.get("profiles") or {}
    if sorted(profiles) != sorted(PROFILE_ORDER):
        errors.append("bundle manifest profiles do not match the contract")
    else:
        for profile in PROFILE_ORDER:
            details = profiles.get(profile) or {}
            if details.get("assets") != assets_for_profile(
                profile, release_contract=release_contract
            ):
                errors.append(f"bundle profile {profile} asset names do not match the contract")
            if details.get("depends_on") != list(PROFILE_DEPENDENCIES[profile]):
                errors.append(f"bundle profile {profile} dependencies do not match the contract")
    source = manifest.get("source") or {}
    fingerprint_payload = {
        "artifact": manifest.get("artifact"),
        "schema_version": manifest.get("schema_version"),
        "release_channel": manifest.get("release_channel"),
        "release_tag": manifest.get("release_tag"),
        "source": {"repository": source.get("repository"), "release_tag": source.get("release_tag")},
        "producer": manifest.get("producer"),
        "assets": {
            name: (manifest_assets.get(name) or {}).get("sha256")
            for name in sorted(manifest_assets)
        },
    }
    if "release_contract" in manifest:
        fingerprint_payload["release_contract"] = release_contract
    expected_fingerprint = hashlib.sha256(
        canonical_json(fingerprint_payload).encode("utf-8")
    ).hexdigest()
    if manifest.get("release_fingerprint") != expected_fingerprint:
        errors.append("bundle release fingerprint mismatch")
    for name, details in manifest_assets.items():
        if not isinstance(details, dict) or not details.get("sha256"):
            errors.append(f"bundle asset has no SHA-256: {name}")
        if not isinstance((details or {}).get("bytes"), int):
            errors.append(f"bundle asset has no byte size: {name}")
    return errors


def _read_json_bytes(body: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot parse {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must contain a JSON object")
    return payload


def _validate_download(body: bytes, details: dict[str, Any], asset: str) -> None:
    if len(body) != details.get("bytes"):
        raise RuntimeError(f"Downloaded V2 asset byte-size mismatch: {asset}")
    if hashlib.sha256(body).hexdigest() != details.get("sha256"):
        raise RuntimeError(f"Downloaded V2 asset SHA-256 mismatch: {asset}")


def _sqlite_integrity(path: Path, asset: str) -> None:
    try:
        with closing(sqlite3.connect(path)) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as exc:
        raise RuntimeError(f"Cannot open installed V2 SQLite for {asset}: {exc}") from exc
    if result != "ok":
        raise RuntimeError(f"V2 SQLite integrity check failed for {asset}: {result}")


def install_bundle(
    *,
    repository: str = DEFAULT_REPOSITORY,
    tag: str = DEFAULT_RELEASE_TAG,
    profile: str = "research_core",
    install_dir: Path = DEFAULT_INSTALL_DIR,
    manifest_url: str | None = None,
) -> dict[str, Any]:
    """Install one manifest-pinned V2 profile after validating every changed asset."""
    if profile not in PROFILE_ORDER:
        raise ValueError(f"Unknown V2 profile: {profile}")
    bundle_url = manifest_url or release_asset_url(repository, tag, BUNDLE_MANIFEST_ASSET)
    manifest_body = fetch_bytes(bundle_url)
    manifest = _read_json_bytes(manifest_body, BUNDLE_MANIFEST_ASSET)
    errors = validate_manifest_contract(manifest)
    if errors:
        raise RuntimeError("Invalid V2 bundle manifest: " + "; ".join(errors))
    if manifest.get("release_tag") != tag and manifest_url is None:
        raise RuntimeError(
            f"V2 manifest release tag {manifest.get('release_tag')!r} does not match requested tag {tag!r}"
        )

    selected_assets = list((manifest["profiles"][profile] or {}).get("assets") or [])
    manifest_assets = manifest["assets"]
    component_by_database = {
        str(details.get("database_asset")): details
        for details in (manifest.get("components") or {}).values()
        if details.get("database_asset")
    }
    component_payloads: dict[str, dict[str, Any]] = {}
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    install_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".tcia-v2-stage-", dir=install_dir.parent) as temporary:
        stage = Path(temporary)
        downloaded: list[str] = []
        unchanged: list[str] = []

        # Component manifests are small and are needed to validate decompressed databases.
        for database_asset, component_details in component_by_database.items():
            if database_asset not in selected_assets:
                continue
            component_manifest_asset = str(component_details.get("manifest_asset") or "")
            if component_manifest_asset in manifest_assets:
                details = manifest_assets[component_manifest_asset]
                body = fetch_bytes(release_asset_url(repository, tag, component_manifest_asset))
                _validate_download(body, details, component_manifest_asset)
                component_payloads[database_asset] = _read_json_bytes(
                    body, component_manifest_asset
                )
                (stage / component_manifest_asset).write_bytes(body)
            else:
                component_payloads[database_asset] = component_details

        for asset in selected_assets:
            destination = install_dir / installed_asset_name(asset)
            details = manifest_assets[asset]
            if asset in component_by_database:
                expected_sqlite = component_payloads[asset].get("sqlite_sha256")
                if destination.is_file() and expected_sqlite and file_sha256(destination) == expected_sqlite:
                    _sqlite_integrity(destination, asset)
                    unchanged.append(asset)
                    continue
            elif destination.is_file() and file_sha256(destination) == details.get("sha256"):
                unchanged.append(asset)
                continue

            staged = stage / installed_asset_name(asset)
            staged.parent.mkdir(parents=True, exist_ok=True)
            if asset in component_by_database:
                compressed = stage / asset
                download_to_path(
                    release_asset_url(repository, tag, asset),
                    compressed,
                    details,
                    asset,
                )
                expected_sqlite = component_payloads[asset].get("sqlite_sha256")
                decompress_gzip_to_path(compressed, staged, expected_sqlite, asset)
                compressed.unlink()
                _sqlite_integrity(staged, asset)
            else:
                download_to_path(
                    release_asset_url(repository, tag, asset),
                    staged,
                    details,
                    asset,
                )
            downloaded.append(asset)

        # Validate first, then replace changed payloads. Component manifests are refreshed
        # even when their database bytes are unchanged so metadata stays release-consistent.
        for asset in selected_assets:
            staged = stage / installed_asset_name(asset)
            if staged.exists():
                os.replace(staged, install_dir / installed_asset_name(asset))
        for database_asset, component_details in component_by_database.items():
            component_manifest_asset = str(component_details.get("manifest_asset") or "")
            staged_manifest = stage / component_manifest_asset
            if component_manifest_asset and database_asset in selected_assets and staged_manifest.exists():
                os.replace(staged_manifest, install_dir / component_manifest_asset)

        state = {
            "artifact": "tcia_metadata_v2_install",
            "release_tag": tag,
            "release_channel": manifest.get("release_channel"),
            "release_fingerprint": manifest.get("release_fingerprint"),
            "installed_profile": profile,
            "installed_assets": selected_assets,
            "installed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        staged_manifest = stage / BUNDLE_MANIFEST_ASSET
        staged_manifest.write_bytes(manifest_body)
        staged_state = stage / INSTALL_STATE_ASSET
        staged_state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(staged_manifest, install_dir / BUNDLE_MANIFEST_ASSET)
        os.replace(staged_state, install_dir / INSTALL_STATE_ASSET)

    return {
        "status": "downloaded" if downloaded else "unchanged",
        "install_dir": str(install_dir),
        "profile": profile,
        "release_tag": tag,
        "release_fingerprint": manifest.get("release_fingerprint"),
        "downloaded_assets": downloaded,
        "unchanged_assets": unchanged,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    exports = sub.add_parser("exports", help="Regenerate all web exports from the bundled snapshot.")
    exports.add_argument("--snapshot-db", required=True)
    exports.add_argument("--out-dir", required=True)
    build = sub.add_parser("build", help="Build the top-level V2 bundle manifest.")
    build.add_argument("--asset-dir", required=True)
    build.add_argument("--out", required=True)
    build.add_argument("--repository", default=DEFAULT_REPOSITORY)
    build.add_argument("--source-tag", default=DEFAULT_SOURCE_TAG)
    build.add_argument("--release-tag", default=DEFAULT_RELEASE_TAG)
    build.add_argument("--source-release-json")
    build.add_argument("--producer-commit", default="")
    build.add_argument("--producer-skill-version", default="")
    build.add_argument(
        "--release-channel", choices=("stable", "preview", "candidate"), default="stable"
    )
    build.add_argument(
        "--release-contract", choices=RELEASE_CONTRACTS, default=FULL_RELEASE_CONTRACT
    )
    validate = sub.add_parser("validate", help="Validate a complete V2 bundle directory.")
    validate.add_argument("--asset-dir", required=True)
    validate.add_argument("--manifest", required=True)
    source = sub.add_parser("validate-source", help="Validate copied assets against a captured source release.")
    source.add_argument("--asset-dir", required=True)
    source.add_argument("--source-release-json", required=True)
    published = sub.add_parser("validate-published", help="Validate GitHub release assets against the bundle manifest.")
    published.add_argument("--manifest", required=True)
    published.add_argument("--release-json", required=True)
    changed = sub.add_parser("changed-assets", help="Print payload assets changed from a previous bundle manifest.")
    changed.add_argument("--current", required=True)
    changed.add_argument("--previous")
    materialize = sub.add_parser(
        "materialize", help="Copy only manifest-selected assets into a candidate directory."
    )
    materialize.add_argument("--asset-dir", required=True)
    materialize.add_argument("--manifest", required=True)
    materialize.add_argument("--out-dir", required=True)
    expected = sub.add_parser("expected-assets", help="Print the exact release asset contract.")
    expected.add_argument("--include-bundle-manifest", action="store_true")
    expected.add_argument("--profile", choices=PROFILE_ORDER)
    expected.add_argument("--no-dependencies", action="store_true")
    expected.add_argument(
        "--release-contract", choices=RELEASE_CONTRACTS, default=FULL_RELEASE_CONTRACT
    )
    install = sub.add_parser("install", help="Install a manifest-pinned V2 release profile.")
    install.add_argument("--repository", default=DEFAULT_REPOSITORY)
    install.add_argument("--tag", default=DEFAULT_RELEASE_TAG)
    install.add_argument("--profile", choices=PROFILE_ORDER, default="research_core")
    install.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR))
    install.add_argument("--manifest-url")
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "exports":
        snapshot_db = Path(args.snapshot_db)
        out_dir = Path(args.out_dir)
        result = export_web_artifacts(snapshot_db, out_dir)
        validate_snapshot_file(snapshot_db, out_dir)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "build":
        payload = build_bundle_manifest(
            Path(args.asset_dir),
            repository=args.repository,
            source_tag=args.source_tag,
            release_tag=args.release_tag,
            source_release_json=Path(args.source_release_json) if args.source_release_json else None,
            producer_commit=args.producer_commit,
            producer_skill_version=args.producer_skill_version,
            release_channel=args.release_channel,
            release_contract=args.release_contract,
        )
        Path(args.out).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"asset_count": payload["asset_count"], "release_fingerprint": payload["release_fingerprint"]}, indent=2))
        return 0
    if args.command == "validate":
        result = validate_bundle(Path(args.asset_dir), Path(args.manifest))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    if args.command == "validate-source":
        result = validate_source_release(Path(args.asset_dir), Path(args.source_release_json))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "validate-published":
        result = validate_published_release(Path(args.manifest), Path(args.release_json))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    if args.command == "changed-assets":
        changed_assets = changed_payload_assets(
            Path(args.current),
            Path(args.previous) if args.previous else None,
        )
        print("\n".join(changed_assets))
        return 0
    if args.command == "materialize":
        result = materialize_bundle(
            Path(args.asset_dir),
            Path(args.manifest),
            Path(args.out_dir),
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "install":
        result = install_bundle(
            repository=args.repository,
            tag=args.tag,
            profile=args.profile,
            install_dir=Path(args.install_dir),
            manifest_url=args.manifest_url,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    assets = (
        assets_for_profile(
            args.profile,
            include_dependencies=not args.no_dependencies,
            release_contract=args.release_contract,
        )
        if args.profile
        else expected_payload_assets(args.release_contract)
    )
    if args.include_bundle_manifest:
        assets.append(BUNDLE_MANIFEST_ASSET)
    print("\n".join(sorted(assets)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
