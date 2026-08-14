#!/usr/bin/env python3
"""Assemble and validate the complete TCIA metadata V2 release bundle."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

try:
    from tcia_snapshot import WEB_EXPORT_ASSETS, export_web_artifacts, validate_snapshot_file
except ModuleNotFoundError:  # Imported as scripts.tcia_v2_bundle by tests or another module.
    from scripts.tcia_snapshot import WEB_EXPORT_ASSETS, export_web_artifacts, validate_snapshot_file


BUNDLE_SCHEMA_VERSION = 1
BUNDLE_ARTIFACT = "tcia_metadata_v2_bundle"
BUNDLE_MANIFEST_ASSET = "tcia_metadata_v2_bundle_manifest.json"
DEFAULT_REPOSITORY = "kirbyju/tcia-query-skill"
DEFAULT_SOURCE_TAG = "tcia-snapshot-latest"
DEFAULT_RELEASE_TAG = "tcia-metadata-v2-preview"

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
        "category": "optional_detail",
        "default_download": False,
    },
    "pathology": {
        "database": "pathology_metadata.sqlite.gz",
        "manifest": "pathology_metadata_manifest.json",
        "category": "optional_detail",
        "default_download": False,
    },
    "controlled_access": {
        "database": "controlled_access_metadata.sqlite.gz",
        "manifest": "controlled_access_metadata_manifest.json",
        "category": "optional_detail",
        "default_download": False,
    },
    "clinical": {
        "database": "clinical_metadata.sqlite.gz",
        "manifest": "clinical_metadata_manifest.json",
        "category": "optional_detail",
        "default_download": False,
    },
    "public_non_dicom": {
        "database": "public_non_dicom_metadata.sqlite.gz",
        "manifest": "public_non_dicom_metadata_manifest.json",
        "category": "v2_core",
        "default_download": True,
    },
    "participant_inventory": {
        "database": "participant_inventory.sqlite.gz",
        "manifest": "participant_inventory_manifest.json",
        "category": "v2_core",
        "default_download": True,
    },
}

EXTRA_ASSETS = {
    "clinical_qc_manual_review.csv": {
        "category": "optional_detail",
        "default_download": False,
        "source": "source_release_copy",
    },
}

SOURCE_COMPONENTS = ("snapshot", "nifti", "pathology", "controlled_access", "clinical")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_payload_assets() -> list[str]:
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


def validate_component_assets(asset_dir: Path) -> dict[str, dict[str, Any]]:
    errors: list[str] = []
    components: dict[str, dict[str, Any]] = {}
    for name, details in COMPONENTS.items():
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
            "schema_version": manifest.get("schema_version"),
            "release_fingerprint": manifest.get("release_fingerprint"),
            "sqlite_sha256": manifest.get("sqlite_sha256"),
            "gzip_sha256": actual_gzip_sha256,
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
) -> dict[str, Any]:
    required = expected_payload_assets()
    missing = [name for name in required if not (asset_dir / name).is_file()]
    if missing:
        raise RuntimeError("Missing bundle assets: " + ", ".join(missing))
    components = validate_component_assets(asset_dir)
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
            "sha256": file_sha256(path),
            "source": asset_source(name),
        }
    source_details = {"repository": repository, **source_release}
    fingerprint_payload = {
        "artifact": BUNDLE_ARTIFACT,
        "schema_version": BUNDLE_SCHEMA_VERSION,
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
        "release_channel": "preview",
        "release_tag": release_tag,
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
        "release_fingerprint": hashlib.sha256(canonical_json(fingerprint_payload).encode("utf-8")).hexdigest(),
    }


def validate_bundle(asset_dir: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if manifest.get("artifact") != BUNDLE_ARTIFACT:
        errors.append("unexpected artifact identifier")
    if manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        errors.append("unexpected bundle schema version")
    expected_names = expected_payload_assets()
    manifest_assets = manifest.get("assets") or {}
    if sorted(manifest_assets) != expected_names:
        errors.append("bundle manifest asset names do not match the contract")
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
    try:
        validate_component_assets(asset_dir)
    except RuntimeError as exc:
        errors.append(str(exc))
    source = manifest.get("source") or {}
    fingerprint_payload = {
        "artifact": manifest.get("artifact"),
        "schema_version": manifest.get("schema_version"),
        "source": {"repository": source.get("repository"), "release_tag": source.get("release_tag")},
        "producer": manifest.get("producer"),
        "assets": {name: (manifest_assets.get(name) or {}).get("sha256") for name in sorted(manifest_assets)},
    }
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
    expected = sub.add_parser("expected-assets", help="Print the exact release asset contract.")
    expected.add_argument("--include-bundle-manifest", action="store_true")
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
    assets = expected_payload_assets()
    if args.include_bundle_manifest:
        assets.append(BUNDLE_MANIFEST_ASSET)
    print("\n".join(sorted(assets)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
