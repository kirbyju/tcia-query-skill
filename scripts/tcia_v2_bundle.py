#!/usr/bin/env python3
"""Assemble and validate the complete TCIA metadata V2 release bundle."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import http.client
import json
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.request
import urllib.error
from contextlib import closing
from pathlib import Path
from typing import Any

try:
    from tcia_snapshot import WEB_EXPORT_ASSETS, export_web_artifacts, validate_snapshot_file
except ModuleNotFoundError:  # Imported as scripts.tcia_v2_bundle by tests or another module.
    from scripts.tcia_snapshot import WEB_EXPORT_ASSETS, export_web_artifacts, validate_snapshot_file


BUNDLE_SCHEMA_VERSION = 3
SUPPORTED_BUNDLE_SCHEMA_VERSIONS = {2, BUNDLE_SCHEMA_VERSION}
BUNDLE_ARTIFACT = "tcia_metadata_v2_bundle"
BUNDLE_MANIFEST_ASSET = "tcia_metadata_v2_bundle_manifest.json"
DEFAULT_REPOSITORY = "kirbyju/tcia-query-skill"
DEFAULT_SOURCE_TAG = "workflow-artifact:update-tcia-metadata-v2-source-inputs"
DEFAULT_RELEASE_TAG = "tcia-metadata-v2-latest"
DEFAULT_INSTALL_DIR = Path(__file__).resolve().parents[1] / "cache" / DEFAULT_RELEASE_TAG
INSTALL_STATE_ASSET = "tcia_metadata_v2_install.json"
GENERATIONS_DIRNAME = "generations"
CURRENT_POINTER = "current"
MIN_RETAINED_GENERATIONS = 2
DEFAULT_NETWORK_TIMEOUT_SECONDS = 30.0
DEFAULT_NETWORK_RETRIES = 2
MAX_RETRY_DELAY_SECONDS = 30.0
STALE_STAGE_MIN_AGE_HOURS = 24
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
    "correction_registry": {
        "database": "tcia_correction_registry.sqlite.gz",
        "manifest": "tcia_correction_registry_manifest.json",
        "category": "audit_support",
        "default_download": False,
    },
}

EXTRA_ASSETS = {
    "clinical_qc_manual_review.csv": {
        "category": "audit_support",
        "default_download": False,
        "source": "source_workflow_input",
    },
}

# Exact filenames formerly managed by the stable V2 installer. They are safe
# prune candidates only inside a directory with a valid official install
# receipt; arbitrary files and maintainer build directories are never removed.
LEGACY_INSTALLED_ASSETS = {
    "agent_datasets.sqlite",
    "nifti_metadata.sqlite",
    "nifti_metadata_manifest.json",
    "pathology_metadata.sqlite",
    "pathology_metadata_manifest.json",
}

SOURCE_COMPONENTS = ("snapshot", "controlled_access", "clinical", "correction_registry")
STREAMLINED_COMPONENTS = (
    "snapshot",
    "controlled_access",
    "clinical",
    "public_non_dicom",
    "participant_inventory",
    "public_non_dicom_audit",
    "participant_inventory_audit",
    "correction_registry",
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
HEALTHY_SOURCE_MODES = {
    "live",
    "loaded",
    "promoted",
    "reused",
    "reused_unchanged_release",
    "not_applicable",
    "no_candidates",
    "validated_component",
}
DEGRADED_SOURCE_MODES = {
    "fallback_snapshot",
    "failed",
    "local_fallback",
    "reused_after_refresh_failure",
    "reused_offline",
}
WAIVER_SCHEMA_VERSION = 1


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def load_source_health_waivers(path: Path | None, *, at: dt.datetime) -> list[dict[str, str]]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != WAIVER_SCHEMA_VERSION:
        raise RuntimeError("Unexpected source-health waiver schema version")
    result: list[dict[str, str]] = []
    for index, raw in enumerate(payload.get("waivers") or []):
        source = str((raw or {}).get("source") or "").strip()
        reason = str((raw or {}).get("reason") or "").strip()
        approved_by = str((raw or {}).get("approved_by") or "").strip()
        expires = str((raw or {}).get("expires_at_utc") or "").strip()
        if not all((source, reason, approved_by, expires)):
            raise RuntimeError(f"Incomplete source-health waiver at index {index}")
        try:
            expires_at = parse_utc(expires)
        except ValueError as exc:
            raise RuntimeError(f"Invalid waiver expiry at index {index}: {expires}") from exc
        if expires_at <= at:
            raise RuntimeError(f"Expired source-health waiver for {source}: {expires}")
        result.append(
            {
                "source": source,
                "reason": reason,
                "approved_by": approved_by,
                "expires_at_utc": expires_at.isoformat(),
            }
        )
    sources = [item["source"] for item in result]
    if len(sources) != len(set(sources)):
        raise RuntimeError("Duplicate source-health waiver scope")
    return result


def component_source_health(component: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Summarize source acquisition health without copying local paths."""
    sources: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, str]] = []

    def record(name: str, mode: Any, message: str = "") -> None:
        normalized = str(mode or "unknown")
        status = "degraded" if normalized in DEGRADED_SOURCE_MODES else "healthy"
        if normalized not in HEALTHY_SOURCE_MODES | DEGRADED_SOURCE_MODES:
            status = "unknown"
        sources[name] = {"status": status, "mode": normalized}
        if message:
            warnings.append({"source": name, "message": message[:500]})

    for name, mode in sorted((manifest.get("source_status") or {}).items()):
        record(f"{component}.{name}", mode)
    for warning in manifest.get("warnings") or []:
        source = f"{component}.{str((warning or {}).get('source') or 'unknown')}"
        message = str((warning or {}).get("message") or "source warning")
        if source not in sources:
            record(source, "fallback_snapshot", message)
        else:
            warnings.append({"source": source, "message": message[:500]})

    if not manifest.get("source_status"):
        clinical_meta = manifest.get("clinical_meta") or {}
        for key in ("idc_clinical_result", "cda_clinical_result"):
            details = clinical_meta.get(key)
            if isinstance(details, dict) and details.get("status"):
                record(
                    f"{component}.{key}",
                    details.get("status"),
                    str(details.get("error") or ""),
                )

    artifact_counts = manifest.get("artifact_counts") or {}
    artifact_errors = int(artifact_counts.get("artifact_errors") or 0)
    if artifact_errors:
        record(
            f"{component}.public_artifact_fetch",
            "failed",
            f"{artifact_errors} public metadata artifact fetches failed",
        )
    if manifest.get("no_network"):
        record(
            f"{component}.network",
            "reused_offline",
            "component was built with network access disabled",
        )

    if not sources:
        record(f"{component}.component", "validated_component")

    degraded = sorted(name for name, item in sources.items() if item["status"] == "degraded")
    unknown = sorted(name for name, item in sources.items() if item["status"] == "unknown")
    return {
        "status": "degraded" if degraded else ("unknown" if unknown else "healthy"),
        "sources": sources,
        "degraded_sources": degraded,
        "unknown_sources": unknown,
        "warning_count": len(warnings),
        "warnings": warnings[:50],
        "warning_summary": manifest.get("warning_summary") or {},
    }


def summarize_bundle_health(
    component_manifests: dict[str, dict[str, Any]],
    waivers: list[dict[str, str]],
) -> dict[str, Any]:
    components = {
        name: component_source_health(name, manifest)
        for name, manifest in sorted(component_manifests.items())
    }
    degraded = sorted(
        source
        for details in components.values()
        for source in details["degraded_sources"]
    )
    unknown = sorted(
        source
        for details in components.values()
        for source in details["unknown_sources"]
    )
    waiver_by_source = {item["source"]: item for item in waivers}
    waived = sorted(source for source in degraded if source in waiver_by_source)
    waived_unknown = sorted(source for source in unknown if source in waiver_by_source)
    unwaived = sorted(set(degraded) - set(waived))
    unwaived_unknown = sorted(set(unknown) - set(waived_unknown))
    unused_waivers = sorted(set(waiver_by_source) - set(degraded) - set(unknown))
    return {
        "status": "degraded" if degraded else ("unknown" if unknown else "healthy"),
        "components": components,
        "degraded_sources": degraded,
        "unknown_sources": unknown,
        "unwaived_degraded_sources": unwaived,
        "unwaived_unknown_sources": unwaived_unknown,
        "waivers": [
            waiver_by_source[source] for source in sorted(set(waived + waived_unknown))
        ],
        "unused_waivers": unused_waivers,
        "warning_count": sum(item["warning_count"] for item in components.values()),
    }


def validate_source_health_summary(health: Any, *, release_channel: str) -> list[str]:
    """Validate schema-3 source health and fail stable contracts closed."""
    if not isinstance(health, dict):
        return ["bundle manifest has no valid source_health summary"]
    components = health.get("components")
    if not isinstance(components, dict):
        return ["bundle source_health components must be an object"]
    degraded: list[str] = []
    unknown: list[str] = []
    for component, details in components.items():
        if not isinstance(details, dict):
            return [f"bundle source_health component is invalid: {component}"]
        sources = details.get("sources")
        if not isinstance(sources, dict):
            return [f"bundle source_health sources are invalid: {component}"]
        component_degraded = sorted(
            str(name)
            for name, item in sources.items()
            if isinstance(item, dict) and item.get("status") == "degraded"
        )
        component_unknown = sorted(
            str(name)
            for name, item in sources.items()
            if not isinstance(item, dict)
            or item.get("status") not in {"healthy", "degraded"}
        )
        if details.get("degraded_sources") != component_degraded:
            return [f"bundle source_health degraded sources disagree: {component}"]
        if details.get("unknown_sources") != component_unknown:
            return [f"bundle source_health unknown sources disagree: {component}"]
        expected_status = (
            "degraded" if component_degraded else ("unknown" if component_unknown else "healthy")
        )
        if details.get("status") != expected_status:
            return [f"bundle source_health component status disagrees: {component}"]
        degraded.extend(component_degraded)
        unknown.extend(component_unknown)
    degraded = sorted(degraded)
    unknown = sorted(unknown)
    waivers = health.get("waivers")
    if not isinstance(waivers, list) or not all(isinstance(item, dict) for item in waivers):
        return ["bundle source_health waivers must be a list"]
    waived = sorted(str(item.get("source") or "") for item in waivers)
    if not all(waived) or len(waived) != len(set(waived)):
        return ["bundle source_health waivers have invalid or duplicate scopes"]
    expected_unwaived_degraded = sorted(set(degraded) - set(waived))
    expected_unwaived_unknown = sorted(set(unknown) - set(waived))
    expected_status = "degraded" if degraded else ("unknown" if unknown else "healthy")
    checks = {
        "status": expected_status,
        "degraded_sources": degraded,
        "unknown_sources": unknown,
        "unwaived_degraded_sources": expected_unwaived_degraded,
        "unwaived_unknown_sources": expected_unwaived_unknown,
    }
    errors = [
        f"bundle source_health {name} is inconsistent"
        for name, expected in checks.items()
        if health.get(name) != expected
    ]
    if release_channel == "stable" and (
        expected_unwaived_degraded or expected_unwaived_unknown
    ):
        errors.append("stable bundle has unwaived degraded or unknown authoritative sources")
    return errors


def decision_set_summary(
    component: str, manifest: dict[str, Any]
) -> dict[str, Any] | None:
    """Return a bounded, hashable decision-set summary for the top manifest."""
    raw = manifest.get("decision_set_summary")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RuntimeError(f"{component} decision_set_summary must be an object")
    sha256 = str(raw.get("sha256") or "").lower()
    try:
        digest_valid = len(sha256) == 64 and int(sha256, 16) >= 0
    except ValueError:
        digest_valid = False
    if not digest_valid:
        raise RuntimeError(f"{component} decision_set_summary has no valid SHA-256")
    status = str(raw.get("status") or "").strip()
    if not status or len(status) > 64:
        raise RuntimeError(f"{component} decision_set_summary has no bounded status")
    counts = raw.get("counts")
    if not isinstance(counts, dict) or len(counts) > 32:
        raise RuntimeError(
            f"{component} decision_set_summary counts must be a compact object"
        )
    normalized_counts: dict[str, int] = {}
    for key, value in sorted(counts.items()):
        name = str(key).strip()
        if (
            not name
            or len(name) > 64
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise RuntimeError(f"{component} decision_set_summary has an invalid count")
        normalized_counts[name] = value
    return {"sha256": sha256, "status": status, "counts": normalized_counts}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_url(
    url: str,
    *,
    timeout: float = DEFAULT_NETWORK_TIMEOUT_SECONDS,
    retries: int = DEFAULT_NETWORK_RETRIES,
):
    for attempt in range(max(retries, 0) + 1):
        try:
            return urllib.request.urlopen(url, timeout=timeout)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            delay = _retry_delay(exc, attempt)
            if delay is None or attempt >= retries:
                raise
            time.sleep(delay)
    raise RuntimeError("unreachable release download retry state")


def _retry_delay(exc: BaseException, attempt: int) -> float | None:
    headers: Any = {}
    if isinstance(exc, urllib.error.HTTPError):
        headers = exc.headers or {}
        rate_limited = exc.code == 429 or (
            exc.code == 403 and str(headers.get("X-RateLimit-Remaining") or "") == "0"
        )
        if exc.code < 500 and not rate_limited:
            return None
    elif not isinstance(
        exc,
        (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, RuntimeError),
    ):
        return None
    delay = float(min(2**attempt, 4))
    retry_after = str(headers.get("Retry-After") or "").strip()
    if retry_after.isdigit():
        delay = float(retry_after)
    else:
        reset = str(headers.get("X-RateLimit-Reset") or "").strip()
        if reset.isdigit():
            delay = max(float(reset) - time.time(), 0.0)
    return min(delay, MAX_RETRY_DELAY_SECONDS)


def _read_response(response: Any) -> bytes:
    body = response.read()
    content_length = str(getattr(response, "headers", {}).get("Content-Length") or "")
    if content_length.isdigit() and len(body) != int(content_length):
        raise RuntimeError("HTTP response ended before Content-Length bytes were received")
    return body


def fetch_bytes(
    url: str,
    *,
    timeout: float = DEFAULT_NETWORK_TIMEOUT_SECONDS,
    retries: int = DEFAULT_NETWORK_RETRIES,
) -> bytes:
    for attempt in range(max(retries, 0) + 1):
        try:
            with open_url(url, timeout=timeout, retries=0) as response:
                return _read_response(response)
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
            RuntimeError,
        ) as exc:
            delay = _retry_delay(exc, attempt)
            if delay is None or attempt >= retries:
                raise
            time.sleep(delay)
    raise RuntimeError("unreachable release fetch retry state")


def download_to_path(
    url: str,
    destination: Path,
    details: dict[str, Any],
    asset: str,
    *,
    timeout: float = DEFAULT_NETWORK_TIMEOUT_SECONDS,
    retries: int = DEFAULT_NETWORK_RETRIES,
) -> None:
    """Stream one release asset to disk while validating size and SHA-256."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(f".{destination.name}.part")
    for attempt in range(max(retries, 0) + 1):
        part.unlink(missing_ok=True)
        try:
            digest = hashlib.sha256()
            downloaded_bytes = 0
            with open_url(url, timeout=timeout, retries=0) as response, part.open("wb") as handle:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    handle.write(chunk)
                    digest.update(chunk)
                    downloaded_bytes += len(chunk)
            if downloaded_bytes != details.get("bytes"):
                raise RuntimeError(f"Downloaded V2 asset byte-size mismatch: {asset}")
            if digest.hexdigest() != details.get("sha256"):
                raise RuntimeError(f"Downloaded V2 asset SHA-256 mismatch: {asset}")
            os.replace(part, destination)
            return
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
            RuntimeError,
        ) as exc:
            part.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            delay = _retry_delay(exc, attempt)
            if delay is None or attempt >= retries:
                raise
            time.sleep(delay)
    raise RuntimeError("unreachable release transfer retry state")


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
    _require_safe_basename(asset, "asset")
    if asset in database_assets() and asset.endswith(".gz"):
        return asset[:-3]
    return asset


def managed_install_names() -> set[str]:
    names = {
        installed_asset_name(name)
        for name in expected_payload_assets(FULL_RELEASE_CONTRACT)
    }
    names.update(str(details["manifest"]) for details in COMPONENTS.values())
    names.update(LEGACY_INSTALLED_ASSETS)
    names.update({BUNDLE_MANIFEST_ASSET, INSTALL_STATE_ASSET})
    return names


def _require_safe_basename(name: str, label: str) -> str:
    if (
        not name
        or name in {".", ".."}
        or Path(name).is_absolute()
        or Path(name).name != name
        or "/" in name
        or "\\" in name
    ):
        raise RuntimeError(f"Unsafe {label} basename: {name!r}")
    return name


def _real_directory(path: Path, label: str, *, create: bool = False) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise RuntimeError(f"{label} must not be a symlink: {expanded}")
    if create:
        expanded.mkdir(parents=True, exist_ok=True)
    if not expanded.is_dir():
        raise RuntimeError(f"{label} is not a directory: {expanded}")
    return expanded.resolve()


def _generation_root(install_dir: Path, *, create: bool = False) -> Path:
    root = _real_directory(install_dir, "V2 install root", create=create)
    generations = root / GENERATIONS_DIRNAME
    return _real_directory(generations, "V2 generations directory", create=create)


def _regular_child(directory: Path, name: str, label: str) -> Path:
    safe_name = _require_safe_basename(name, label)
    root = _real_directory(directory, f"{label} parent")
    path = root / safe_name
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} must be a regular file: {path}")
    return path


def active_generation_dir(install_dir: Path) -> Path:
    """Resolve a generation install while accepting the legacy flat layout."""
    root = _real_directory(install_dir, "V2 install root")
    current = root / CURRENT_POINTER
    if not current.exists() and not current.is_symlink():
        return root
    if not current.is_symlink():
        raise RuntimeError(f"V2 current pointer must be a symlink: {current}")
    target = Path(os.readlink(current))
    if (
        target.is_absolute()
        or len(target.parts) != 2
        or target.parts[0] != GENERATIONS_DIRNAME
    ):
        raise RuntimeError(f"V2 current pointer escapes generations: {target}")
    generation_name = _require_safe_basename(target.parts[1], "generation")
    generations = _generation_root(root)
    generation = generations / generation_name
    if generation.is_symlink() or not generation.is_dir():
        raise RuntimeError(f"V2 current generation is not a real directory: {generation}")
    return generation.resolve()


def generation_id(fingerprint: str, profile: str) -> str:
    if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint.lower()):
        raise RuntimeError("V2 release fingerprint must be a 64-character SHA-256")
    if profile not in PROFILE_ORDER:
        raise RuntimeError(f"Unknown installed V2 profile: {profile}")
    safe_profile = "".join(c if c.isalnum() or c in "-_" else "-" for c in profile)
    return f"{fingerprint}-{safe_profile}"


def immutable_release_tag(manifest: dict[str, Any]) -> str:
    generated = str(manifest.get("generated_at_utc") or "")[:10].replace("-", ".")
    fingerprint = str(manifest.get("release_fingerprint") or "")
    if not generated or len(fingerprint) < 12:
        return ""
    return f"tcia-metadata-v2-{generated}-{fingerprint[:12]}"


def _atomic_symlink(target: str, link: Path) -> None:
    _require_safe_basename(link.name, "symlink")
    _real_directory(link.parent, "symlink parent")
    temporary = link.with_name(f".{link.name}.tmp-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    os.symlink(target, temporary)
    os.replace(temporary, link)


def _copy_or_link(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _load_install_contract(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    directory = _real_directory(directory, "V2 generation")
    manifest = _read_json_bytes(
        _regular_child(
            directory, BUNDLE_MANIFEST_ASSET, "bundle manifest"
        ).read_bytes(),
        BUNDLE_MANIFEST_ASSET,
    )
    state = _read_json_bytes(
        _regular_child(
            directory, INSTALL_STATE_ASSET, "install receipt"
        ).read_bytes(),
        INSTALL_STATE_ASSET,
    )
    manifest_errors = validate_manifest_contract(manifest)
    if manifest_errors:
        raise RuntimeError(
            f"Invalid V2 bundle manifest in {directory}: " + "; ".join(manifest_errors)
        )
    if state.get("artifact") != "tcia_metadata_v2_install":
        raise RuntimeError(f"Unexpected V2 install receipt in {directory}")
    if state.get("release_fingerprint") != manifest.get("release_fingerprint"):
        raise RuntimeError(f"V2 install receipt and manifest disagree in {directory}")
    profile = str(state.get("installed_profile") or "")
    if profile not in PROFILE_ORDER:
        raise RuntimeError(f"Invalid installed profile in {directory}: {profile!r}")
    installed_assets = state.get("installed_assets")
    expected_assets = list((manifest.get("profiles", {}).get(profile) or {}).get("assets") or [])
    if installed_assets != expected_assets:
        raise RuntimeError(f"V2 install receipt assets disagree with profile in {directory}")
    for asset in installed_assets:
        _require_safe_basename(str(asset), "installed asset")
    return manifest, state


def _verify_generation(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    directory = _real_directory(directory, "V2 generation")
    manifest, state = _load_install_contract(directory)
    manifest_assets = manifest.get("assets") or {}
    component_by_database = {
        str(details.get("database_asset")): details
        for details in (manifest.get("components") or {}).values()
        if details.get("database_asset")
    }
    installed_assets = state["installed_assets"]
    expected_names = _compatibility_names(directory, state, manifest=manifest)
    actual_names = {path.name for path in directory.iterdir()}
    if actual_names != expected_names:
        raise RuntimeError(
            f"Installed generation file set disagrees with receipt: {directory}"
        )
    for asset in installed_assets:
        details = manifest_assets.get(asset)
        if not isinstance(details, dict):
            raise RuntimeError(f"Installed asset is absent from manifest: {asset}")
        path = _regular_child(
            directory, installed_asset_name(asset), "installed generation asset"
        )
        component = component_by_database.get(asset)
        if component:
            if file_sha256(path) != component.get("sqlite_sha256"):
                raise RuntimeError(f"Installed generation SQLite hash mismatch: {asset}")
            _sqlite_integrity(path, asset)
        elif file_sha256(path) != details.get("sha256"):
            raise RuntimeError(f"Installed generation asset hash mismatch: {asset}")
    return manifest, state


def _compatibility_names(
    directory: Path,
    state: dict[str, Any],
    *,
    manifest: dict[str, Any] | None = None,
) -> set[str]:
    names = {installed_asset_name(str(asset)) for asset in state["installed_assets"]}
    names.update({BUNDLE_MANIFEST_ASSET, INSTALL_STATE_ASSET})
    contract = manifest if manifest is not None else _load_install_contract(directory)[0]
    for details in (contract.get("components") or {}).values():
        manifest_asset = str((details or {}).get("manifest_asset") or "")
        if manifest_asset and not (directory / manifest_asset).is_symlink() and (directory / manifest_asset).is_file():
            names.add(manifest_asset)
    if not names.issubset(managed_install_names()):
        raise RuntimeError("V2 compatibility names exceed the managed asset allowlist")
    for name in names:
        _require_safe_basename(name, "compatibility link")
    return names


def _ensure_compatibility_links(install_dir: Path, names: set[str]) -> None:
    install_dir = _real_directory(install_dir, "V2 install root")
    if not names.issubset(managed_install_names()):
        raise RuntimeError("V2 compatibility names exceed the managed asset allowlist")
    for name in sorted(names):
        _require_safe_basename(name, "compatibility link")
        path = install_dir / name
        desired = f"{CURRENT_POINTER}/{name}"
        if path.is_symlink() and os.readlink(path) == desired:
            continue
        if path.exists() or path.is_symlink():
            if path.is_dir():
                raise RuntimeError(f"Cannot replace compatibility directory: {path}")
            path.unlink()
        _atomic_symlink(desired, path)
    for name in sorted(managed_install_names() - names):
        path = install_dir / name
        if path.is_symlink() and os.readlink(path).startswith(f"{CURRENT_POINTER}/"):
            path.unlink()


def _prepare_compatibility_links(install_dir: Path, names: set[str]) -> None:
    """Create first-install aliases before current is committed."""
    install_dir = _real_directory(install_dir, "V2 install root")
    if not names.issubset(managed_install_names()):
        raise RuntimeError("V2 compatibility names exceed the managed asset allowlist")
    for name in sorted(names):
        _require_safe_basename(name, "compatibility link")
        path = install_dir / name
        if path.exists() or path.is_symlink():
            continue
        _atomic_symlink(f"{CURRENT_POINTER}/{name}", path)


def migrate_legacy_install(install_dir: Path) -> dict[str, Any] | None:
    """Convert a valid flat install into a first retained generation in place."""
    install_dir = _real_directory(install_dir, "V2 install root")
    if (install_dir / CURRENT_POINTER).exists() or (install_dir / CURRENT_POINTER).is_symlink():
        return None
    manifest_path = install_dir / BUNDLE_MANIFEST_ASSET
    state_path = install_dir / INSTALL_STATE_ASSET
    if not manifest_path.is_file() or not state_path.is_file():
        return None
    manifest, state = _load_install_contract(install_dir)
    generations = _generation_root(install_dir, create=True)
    identifier = generation_id(
        str(manifest.get("release_fingerprint") or "legacy"),
        str(state.get("installed_profile") or "unknown"),
    )
    destination = generations / identifier
    if destination.is_symlink():
        raise RuntimeError(f"V2 generation must not be a symlink: {destination}")
    if not destination.exists():
        with tempfile.TemporaryDirectory(prefix=".migration-", dir=generations) as temporary:
            stage = Path(temporary)
            names = {
                installed_asset_name(str(asset))
                for asset in state.get("installed_assets") or []
            }
            names.update({BUNDLE_MANIFEST_ASSET, INSTALL_STATE_ASSET})
            for details in (manifest.get("components") or {}).values():
                manifest_asset = str((details or {}).get("manifest_asset") or "")
                if manifest_asset and (install_dir / manifest_asset).is_file():
                    names.add(manifest_asset)
            for name in names:
                source = install_dir / _require_safe_basename(name, "legacy asset")
                if source.is_symlink():
                    raise RuntimeError(f"Legacy V2 asset must not be a symlink: {source}")
                if source.is_file():
                    _copy_or_link(source, stage / name)
            _verify_generation(stage)
            os.replace(stage, destination)
    _, migrated_state = _verify_generation(destination)
    names = _compatibility_names(destination, migrated_state)
    _prepare_compatibility_links(install_dir, names)
    _atomic_symlink(f"{GENERATIONS_DIRNAME}/{identifier}", install_dir / CURRENT_POINTER)
    _ensure_compatibility_links(install_dir, names)
    return {"generation": identifier, "migrated": True}


def inspect_install(
    install_dir: Path,
    *,
    stale_stage_hours: int = STALE_STAGE_MIN_AGE_HOURS,
) -> dict[str, Any]:
    """Report receipt-selected and stale installer-managed files without deleting."""
    directory = _real_directory(install_dir, "V2 install root")
    active_directory = active_generation_dir(directory)
    try:
        manifest, state = _load_install_contract(active_directory)
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Cannot inspect V2 install without a valid manifest and receipt: {directory}"
        ) from exc
    installed_assets = state["installed_assets"]

    active_names = {installed_asset_name(name) for name in installed_assets}
    active_names.update({BUNDLE_MANIFEST_ASSET, INSTALL_STATE_ASSET})
    active_files = []
    stale_files = []
    for name in sorted(managed_install_names()):
        path = directory / name
        if not path.is_file():
            continue
        item = {"path": str(path), "bytes": path.stat().st_size}
        (active_files if name in active_names else stale_files).append(item)

    threshold = time.time() - max(stale_stage_hours, 0) * 3600
    stale_stages = []
    generation_root = directory / GENERATIONS_DIRNAME
    stage_parents = []
    if generation_root.exists() or generation_root.is_symlink():
        stage_parents.append(_generation_root(directory))
    for stage_parent in stage_parents:
        if not stage_parent.is_dir():
            continue
        for path in sorted(stage_parent.glob(".tcia-v2-stage-*")):
            if path.is_symlink():
                raise RuntimeError(f"V2 staging directory must not be a symlink: {path}")
            if path.is_dir() and path.stat().st_mtime <= threshold:
                size = sum(
                    child.stat().st_size
                    for child in path.rglob("*")
                    if child.is_file()
                )
                stale_stages.append({"path": str(path), "bytes": size})
    generations = []
    if generation_root.exists() or generation_root.is_symlink():
        generation_root = _generation_root(directory)
        for path in sorted(generation_root.iterdir()):
            if path.name.startswith("."):
                continue
            if path.is_symlink() or not path.is_dir():
                generations.append(
                    {
                        "path": str(path),
                        "generation": path.name,
                        "active": False,
                        "verified": False,
                        "error": "generation entry is not a real directory",
                    }
                )
                continue
            try:
                generation_manifest, generation_state = _verify_generation(path)
            except (OSError, RuntimeError, json.JSONDecodeError) as exc:
                generations.append(
                    {
                        "path": str(path),
                        "generation": path.name,
                        "active": path == active_directory,
                        "verified": False,
                        "error": str(exc),
                    }
                )
                continue
            generations.append(
                {
                    "path": str(path),
                    "generation": path.name,
                    "active": path == active_directory,
                    "verified": True,
                    "release_fingerprint": generation_manifest.get("release_fingerprint"),
                    "installed_at_utc": generation_state.get("installed_at_utc"),
                    "bytes": sum(child.stat().st_size for child in path.rglob("*") if child.is_file()),
                }
            )
    return {
        "install_dir": str(directory),
        "active_directory": str(active_directory),
        "installed_profile": state.get("installed_profile"),
        "release_fingerprint": manifest.get("release_fingerprint"),
        "active_bytes": sum(item["bytes"] for item in active_files),
        "stale_bytes": sum(item["bytes"] for item in stale_files + stale_stages),
        "active_files": active_files,
        "stale_files": stale_files,
        "stale_stage_directories": stale_stages,
        "generations": generations,
    }


def prune_install(
    install_dir: Path,
    *,
    apply: bool = False,
    stale_stage_hours: int = STALE_STAGE_MIN_AGE_HOURS,
) -> dict[str, Any]:
    """Remove only stale, receipt-unselected files owned by the V2 installer."""
    report = inspect_install(
        install_dir,
        stale_stage_hours=stale_stage_hours,
    )
    removed = []
    if apply:
        for item in report["stale_files"]:
            path = Path(item["path"])
            if path.parent != Path(report["install_dir"]) or path.name not in managed_install_names():
                raise RuntimeError(f"Refusing to prune an unconfined V2 file: {path}")
            path.unlink()
            removed.append(item)
        for item in report["stale_stage_directories"]:
            path = Path(item["path"])
            if path.is_symlink() or path.parent != Path(report["install_dir"]) / GENERATIONS_DIRNAME:
                raise RuntimeError(f"Refusing to prune an unconfined V2 stage: {path}")
            shutil.rmtree(path)
            removed.append(item)
        verified_generations = sorted(
            (
                item for item in report.get("generations", [])
                if item.get("verified") and not item.get("active")
            ),
            key=lambda item: str(item.get("installed_at_utc") or ""),
            reverse=True,
        )
        keep_prior = max(MIN_RETAINED_GENERATIONS - 1, 1)
        for item in verified_generations[keep_prior:]:
            path = Path(item["path"])
            if path.is_symlink() or path.parent != Path(report["install_dir"]) / GENERATIONS_DIRNAME:
                raise RuntimeError(f"Refusing to prune an unconfined V2 generation: {path}")
            shutil.rmtree(path)
            removed.append(item)
    return {
        **report,
        "status": "pruned" if apply and removed else "unchanged",
        "dry_run": not apply,
        "removed_bytes": sum(item["bytes"] for item in removed),
        "removed": removed,
    }


def rollback_install(install_dir: Path, generation: str | None = None) -> dict[str, Any]:
    """Atomically select a verified retained generation without downloading."""
    install_dir = _real_directory(install_dir, "V2 install root")
    report = inspect_install(install_dir)
    candidates = [
        item
        for item in report.get("generations", [])
        if item.get("verified") and not item.get("active")
    ]
    if generation:
        candidates = [item for item in candidates if item.get("generation") == generation]
    else:
        candidates.sort(
            key=lambda item: str(item.get("installed_at_utc") or ""), reverse=True
        )
    if not candidates:
        raise RuntimeError("No verified prior V2 generation is available for rollback")
    selected = candidates[0]
    directory = Path(selected["path"])
    _, state = _verify_generation(directory)
    _atomic_symlink(
        f"{GENERATIONS_DIRNAME}/{selected['generation']}",
        install_dir / CURRENT_POINTER,
    )
    _ensure_compatibility_links(
        install_dir, _compatibility_names(directory, state)
    )
    return {
        "status": "rolled_back",
        "install_dir": str(install_dir),
        "active_generation": str(directory),
        "generation": selected["generation"],
        "release_fingerprint": selected.get("release_fingerprint"),
        "installed_profile": state.get("installed_profile"),
    }


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


def expected_payload_assets_for_schema(
    schema_version: int, release_contract: str,
) -> list[str]:
    assets = expected_payload_assets(release_contract)
    if schema_version == 2:
        legacy = COMPONENTS["correction_registry"]
        assets = [
            name for name in assets
            if name not in {legacy["database"], legacy["manifest"]}
        ]
    return assets


def component_names_for_schema(
    schema_version: int, release_contract: str,
) -> tuple[str, ...]:
    names = component_names_for_contract(release_contract)
    if schema_version == 2:
        names = tuple(name for name in names if name != "correction_registry")
    return names


def assets_for_profile_schema(
    profile: str, schema_version: int, release_contract: str,
) -> list[str]:
    allowed = set(expected_payload_assets_for_schema(schema_version, release_contract))
    return [
        name for name in assets_for_profile(profile, release_contract=release_contract)
        if name in allowed
    ]


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
    return "source_workflow_input"


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
        expected_sqlite_sha256 = str(manifest.get("sqlite_sha256") or "")
        if not expected_sqlite_sha256:
            errors.append(f"{manifest_path.name} has no sqlite_sha256")
        else:
            try:
                with tempfile.TemporaryDirectory(prefix=f".{name}-verify-", dir=asset_dir) as temporary:
                    sqlite_path = Path(temporary) / database_path.name.removesuffix(".gz")
                    with gzip.open(database_path, "rb") as source, sqlite_path.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    if file_sha256(sqlite_path) != expected_sqlite_sha256:
                        errors.append(
                            f"{database_path.name} does not match {manifest_path.name} sqlite_sha256"
                        )
                    else:
                        _sqlite_integrity(sqlite_path, database_path.name)
            except (OSError, EOFError, gzip.BadGzipFile, RuntimeError) as exc:
                errors.append(f"invalid component SQLite {database_path.name}: {exc}")
        components[name] = {
            "database_asset": database_path.name,
            "manifest_asset": manifest_path.name,
            "profile": asset_profile(database_path.name),
            "schema_version": manifest.get("schema_version"),
            "release_fingerprint": manifest.get("release_fingerprint"),
            "sqlite_sha256": expected_sqlite_sha256,
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
    return {
        "ok": not errors,
        "errors": errors,
        "release_id": release.get("id"),
        "release_tag": release.get("tag_name"),
        "target_commitish": release.get("target_commitish"),
        "published_at": release.get("published_at"),
        "updated_at": release.get("updated_at"),
        "assets": verified,
    }


def validate_selected_bundle_assets(
    asset_dir: Path,
    manifest_path: Path,
    asset_names: list[str],
    *,
    release_json_path: Path | None = None,
) -> dict[str, Any]:
    """Validate a pinned subset used as an internal V2 build baseline."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = validate_manifest_contract(manifest)
    legacy_omissions = legacy_schema_two_component_manifest_omissions(
        manifest, selected_assets=set(asset_names)
    )
    errors = [error for error in errors if error not in legacy_omissions]
    manifest_assets = manifest.get("assets") or {}
    release_assets: dict[str, dict[str, Any]] = {}
    release_tag = ""
    release_supplied = release_json_path is not None
    if release_json_path:
        release = json.loads(release_json_path.read_text(encoding="utf-8"))
        release_tag = str(release.get("tag_name") or "")
        release_assets = {
            str(asset.get("name")): asset for asset in release.get("assets") or []
        }
    verified: dict[str, dict[str, Any]] = {}
    for name in asset_names:
        details = manifest_assets.get(name)
        path = asset_dir / name
        if not details:
            errors.append(f"bundle manifest does not contain selected asset: {name}")
            continue
        if not path.is_file():
            errors.append(f"missing selected bundle asset: {name}")
            continue
        digest = file_sha256(path)
        if digest != details.get("sha256"):
            errors.append(f"selected bundle asset hash mismatch: {name}")
        if path.stat().st_size != details.get("bytes"):
            errors.append(f"selected bundle asset byte-size mismatch: {name}")
        if release_supplied:
            release_asset = release_assets.get(name)
            if not release_asset:
                errors.append(f"captured release does not contain selected asset: {name}")
            else:
                release_digest = str(release_asset.get("digest") or "").removeprefix(
                    "sha256:"
                )
                if not release_digest or release_digest != digest:
                    errors.append(f"captured release digest mismatch: {name}")
                release_size = release_asset.get("size")
                if release_size != path.stat().st_size:
                    errors.append(f"captured release byte-size mismatch: {name}")
        verified[name] = {"bytes": path.stat().st_size, "sha256": digest}
    return {
        "ok": not errors,
        "errors": errors,
        "release_tag": release_tag or manifest.get("release_tag"),
        "release_fingerprint": manifest.get("release_fingerprint"),
        "legacy_schema2_component_manifest_omissions": sorted(legacy_omissions),
        "assets": verified,
    }


def legacy_schema_two_component_manifest_omissions(
    manifest: dict[str, Any], *, selected_assets: set[str]
) -> set[str]:
    """Identify the exact dangling pointers in the legacy schema-2 baseline.

    Streamlined schema-2 releases published component databases but retained
    canonical component-manifest pointers in the top manifest.  This exception
    is intentionally limited to subset validation of that historical contract;
    the authoritative manifest validator remains strict for every release built
    by current code.
    """
    if manifest.get("schema_version") != 2:
        return set()
    release_contract = str(manifest.get("release_contract") or FULL_RELEASE_CONTRACT)
    if release_contract != STREAMLINED_RELEASE_CONTRACT:
        return set()
    manifest_assets = manifest.get("assets") or {}
    try:
        expected_assets = expected_payload_assets_for_schema(2, release_contract)
        expected_components = set(component_names_for_schema(2, release_contract))
    except ValueError:
        return set()
    components = manifest.get("components") or {}
    if sorted(manifest_assets) != expected_assets or set(components) != expected_components:
        return set()

    allowed: set[str] = set()
    for component in sorted(expected_components):
        pointer = str((components.get(component) or {}).get("manifest_asset") or "")
        canonical = str(COMPONENTS[component]["manifest"])
        if (
            pointer == canonical
            and pointer not in manifest_assets
            and pointer not in selected_assets
            and pointer not in expected_assets
        ):
            allowed.add(
                f"component {component} manifest_asset is not a published asset: {pointer}"
            )
    return allowed


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
    source_health_waiver: Path | None = None,
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
    component_names = component_names_for_contract(release_contract)
    components = validate_component_assets(asset_dir, component_names)
    component_manifests = {
        name: load_component_manifest(asset_dir, name) for name in component_names
    }
    decision_sets = {
        name: summary
        for name, manifest in sorted(component_manifests.items())
        if (summary := decision_set_summary(name, manifest)) is not None
    }
    source_release = (
        validate_source_release(asset_dir, source_release_json)
        if source_release_json is not None
        else {"release_tag": source_tag}
    )
    if source_release_json is not None and not source_release["ok"]:
        raise RuntimeError(
            "Invalid V2 source release: " + "; ".join(source_release["errors"])
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
    for details in components.values():
        manifest_asset = str(details.get("manifest_asset") or "")
        if manifest_asset and manifest_asset not in assets:
            details["source_manifest"] = manifest_asset
            details.pop("manifest_asset", None)
    generated_at = dt.datetime.now(dt.timezone.utc)
    waivers = load_source_health_waivers(source_health_waiver, at=generated_at)
    source_health = summarize_bundle_health(component_manifests, waivers)
    if source_health["unused_waivers"]:
        raise RuntimeError(
            "Source-health waivers do not match a currently degraded source: "
            + ", ".join(source_health["unused_waivers"])
        )
    blocked_sources = sorted(
        source_health["unwaived_degraded_sources"]
        + source_health["unwaived_unknown_sources"]
    )
    if release_channel == "stable" and blocked_sources:
        raise RuntimeError(
            "Stable V2 promotion rejected degraded or unknown authoritative sources without "
            "an active scoped waiver: "
            + ", ".join(blocked_sources)
        )
    source_details = {
        "repository": repository,
        **{
            key: value
            for key, value in source_release.items()
            if key not in {"ok", "errors"}
        },
    }
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
        "source_health": source_health,
        "decision_sets": decision_sets,
    }
    return {
        "artifact": BUNDLE_ARTIFACT,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "release_channel": release_channel,
        "release_tag": release_tag,
        "release_contract": release_contract,
        "generated_at_utc": generated_at.isoformat(),
        "source": source_details,
        "producer": {
            "repository": repository,
            "commit": producer_commit,
            "skill_version": producer_skill_version,
        },
        "asset_count": len(assets),
        "assets": assets,
        "components": components,
        "source_health": source_health,
        "decision_sets": decision_sets,
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
    errors: list[str] = validate_manifest_contract(manifest)
    if manifest.get("artifact") != BUNDLE_ARTIFACT:
        errors.append("unexpected artifact identifier")
    if manifest.get("schema_version") not in SUPPORTED_BUNDLE_SCHEMA_VERSIONS:
        errors.append("unexpected bundle schema version")
    schema_version = manifest.get("schema_version")
    schema_version = schema_version if isinstance(schema_version, int) else 0
    release_contract = str(manifest.get("release_contract") or FULL_RELEASE_CONTRACT)
    try:
        expected_names = expected_payload_assets_for_schema(schema_version, release_contract)
    except ValueError as exc:
        errors.append(str(exc))
        expected_names = []
    manifest_assets = manifest.get("assets") or {}
    if sorted(manifest_assets) != expected_names:
        errors.append("bundle manifest asset names do not match the contract")
    try:
        expected_components = set(component_names_for_schema(schema_version, release_contract))
    except ValueError:
        expected_components = set()
    if set(manifest.get("components") or {}) != expected_components:
        errors.append("bundle components do not match the release contract")
    profiles = manifest.get("profiles") or {}
    if sorted(profiles) != sorted(PROFILE_ORDER):
        errors.append("bundle manifest profiles do not match the contract")
    else:
        for profile in PROFILE_ORDER:
            if (profiles.get(profile) or {}).get("assets") != assets_for_profile_schema(
                profile, schema_version, release_contract
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
        for pointer_name in ("database_asset", "manifest_asset"):
            pointer = str((details or {}).get(pointer_name) or "")
            if pointer and pointer not in manifest_assets:
                errors.append(
                    f"component {component} {pointer_name} is not a published asset: {pointer}"
                )
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
    if manifest.get("schema_version") == BUNDLE_SCHEMA_VERSION:
        fingerprint_payload["source_health"] = manifest.get("source_health")
        if "decision_sets" in manifest:
            fingerprint_payload["decision_sets"] = manifest.get("decision_sets")
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
    if manifest.get("schema_version") not in SUPPORTED_BUNDLE_SCHEMA_VERSIONS:
        errors.append("unexpected bundle schema version")
    schema_version = manifest.get("schema_version")
    schema_version = schema_version if isinstance(schema_version, int) else 0
    release_contract = str(manifest.get("release_contract") or FULL_RELEASE_CONTRACT)
    try:
        expected_names = expected_payload_assets_for_schema(schema_version, release_contract)
    except ValueError as exc:
        errors.append(str(exc))
        expected_names = []
    manifest_assets = manifest.get("assets") or {}
    if sorted(manifest_assets) != expected_names:
        errors.append("bundle manifest asset names do not match the contract")
    try:
        expected_components = set(component_names_for_schema(schema_version, release_contract))
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
            if details.get("assets") != assets_for_profile_schema(
                profile, schema_version, release_contract
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
    if manifest.get("schema_version") == BUNDLE_SCHEMA_VERSION:
        fingerprint_payload["source_health"] = manifest.get("source_health")
        if "decision_sets" in manifest:
            fingerprint_payload["decision_sets"] = manifest.get("decision_sets")
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
    for component, details in (manifest.get("components") or {}).items():
        for pointer_name in ("database_asset", "manifest_asset"):
            pointer = str((details or {}).get(pointer_name) or "")
            if pointer and pointer not in manifest_assets:
                errors.append(
                    f"component {component} {pointer_name} is not a published asset: {pointer}"
                )
    if manifest.get("schema_version") == BUNDLE_SCHEMA_VERSION:
        errors.extend(
            validate_source_health_summary(
                manifest.get("source_health"),
                release_channel=str(manifest.get("release_channel") or ""),
            )
        )
        decision_sets = manifest.get("decision_sets")
        if decision_sets is not None:
            if not isinstance(decision_sets, dict):
                errors.append("bundle decision_sets must be an object")
            else:
                for component, summary in decision_sets.items():
                    if component not in (manifest.get("components") or {}):
                        errors.append(f"decision-set summary has no component: {component}")
                        continue
                    try:
                        normalized = decision_set_summary(
                            component, {"decision_set_summary": summary}
                        )
                    except RuntimeError as exc:
                        errors.append(str(exc))
                    else:
                        if normalized != summary:
                            errors.append(f"decision-set summary is not canonical: {component}")
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
            violations = conn.execute("PRAGMA foreign_key_check").fetchmany(20)
    except sqlite3.Error as exc:
        raise RuntimeError(f"Cannot open installed V2 SQLite for {asset}: {exc}") from exc
    if result != "ok":
        raise RuntimeError(f"V2 SQLite integrity check failed for {asset}: {result}")
    if violations:
        raise RuntimeError(
            f"V2 SQLite foreign-key check failed for {asset} (first 20): "
            + canonical_json([list(row) for row in violations])
        )


def install_bundle(
    *,
    repository: str = DEFAULT_REPOSITORY,
    tag: str = DEFAULT_RELEASE_TAG,
    profile: str = "research_core",
    install_dir: Path = DEFAULT_INSTALL_DIR,
    manifest_url: str | None = None,
    timeout: float = DEFAULT_NETWORK_TIMEOUT_SECONDS,
    retries: int = DEFAULT_NETWORK_RETRIES,
) -> dict[str, Any]:
    """Install a profile as a verified generation and atomically switch current."""
    if profile not in PROFILE_ORDER:
        raise ValueError(f"Unknown V2 profile: {profile}")
    bundle_url = manifest_url or release_asset_url(repository, tag, BUNDLE_MANIFEST_ASSET)
    manifest_body = fetch_bytes(bundle_url, timeout=timeout, retries=retries)
    manifest = _read_json_bytes(manifest_body, BUNDLE_MANIFEST_ASSET)
    errors = validate_manifest_contract(manifest)
    if errors:
        raise RuntimeError("Invalid V2 bundle manifest: " + "; ".join(errors))
    allowed_tags = {str(manifest.get("release_tag") or ""), immutable_release_tag(manifest)}
    if tag not in allowed_tags and manifest_url is None:
        raise RuntimeError(
            f"V2 manifest release tag/immutable identity does not match requested tag {tag!r}"
        )

    selected_assets = list((manifest["profiles"][profile] or {}).get("assets") or [])
    manifest_assets = manifest["assets"]
    component_by_database = {
        str(details.get("database_asset")): details
        for details in (manifest.get("components") or {}).values()
        if details.get("database_asset")
    }
    component_payloads: dict[str, dict[str, Any]] = dict(component_by_database)
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    install_dir = _real_directory(install_dir, "V2 install root", create=True)
    migration = migrate_legacy_install(install_dir)
    active_dir = active_generation_dir(install_dir)
    generations = _generation_root(install_dir, create=True)
    identifier = generation_id(str(manifest.get("release_fingerprint")), profile)
    generation = generations / identifier

    if generation.is_symlink():
        raise RuntimeError(f"V2 generation must not be a symlink: {generation}")
    if generation.is_dir():
        _, state = _verify_generation(generation)
        compatibility_names = _compatibility_names(generation, state)
        _prepare_compatibility_links(install_dir, compatibility_names)
        _atomic_symlink(f"{GENERATIONS_DIRNAME}/{identifier}", install_dir / CURRENT_POINTER)
        _ensure_compatibility_links(install_dir, compatibility_names)
        cleanup = prune_install(install_dir, apply=True)
        return {
            "status": "unchanged",
            "install_dir": str(install_dir),
            "active_generation": str(generation),
            "profile": profile,
            "release_tag": tag,
            "release_fingerprint": manifest.get("release_fingerprint"),
            "downloaded_assets": [],
            "unchanged_assets": selected_assets,
            "migrated_legacy_install": bool(migration),
            "pruned_bytes": cleanup["removed_bytes"],
            "pruned_files": [item["path"] for item in cleanup["removed"]],
        }

    with tempfile.TemporaryDirectory(prefix=".tcia-v2-stage-", dir=generations) as temporary:
        stage = Path(temporary)
        downloaded: list[str] = []
        unchanged: list[str] = []

        for asset in selected_assets:
            destination = active_dir / installed_asset_name(asset)
            details = manifest_assets[asset]
            staged = stage / installed_asset_name(asset)
            if asset in component_by_database:
                expected_sqlite = component_payloads[asset].get("sqlite_sha256")
                if destination.is_file() and expected_sqlite and file_sha256(destination) == expected_sqlite:
                    _sqlite_integrity(destination, asset)
                    _copy_or_link(destination, staged)
                    unchanged.append(asset)
                    continue
            elif destination.is_file() and file_sha256(destination) == details.get("sha256"):
                _copy_or_link(destination, staged)
                unchanged.append(asset)
                continue

            staged.parent.mkdir(parents=True, exist_ok=True)
            if asset in component_by_database:
                compressed = stage / asset
                download_to_path(
                    release_asset_url(repository, tag, asset),
                    compressed,
                    details,
                    asset,
                    timeout=timeout,
                    retries=retries,
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
                    timeout=timeout,
                    retries=retries,
                )
            downloaded.append(asset)

        state = {
            "artifact": "tcia_metadata_v2_install",
            "install_schema_version": 2,
            "release_tag": tag,
            "release_channel": manifest.get("release_channel"),
            "release_fingerprint": manifest.get("release_fingerprint"),
            "installed_profile": profile,
            "installed_assets": selected_assets,
            "installed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "generation": identifier,
        }
        (stage / BUNDLE_MANIFEST_ASSET).write_bytes(manifest_body)
        (stage / INSTALL_STATE_ASSET).write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _verify_generation(stage)
        os.replace(stage, generation)

    _, active_state = _verify_generation(generation)
    compatibility_names = _compatibility_names(generation, active_state)
    _prepare_compatibility_links(install_dir, compatibility_names)
    _atomic_symlink(f"{GENERATIONS_DIRNAME}/{identifier}", install_dir / CURRENT_POINTER)
    _ensure_compatibility_links(install_dir, compatibility_names)
    cleanup = prune_install(install_dir, apply=True)
    return {
        "status": "downloaded" if downloaded else "unchanged",
        "install_dir": str(install_dir),
        "active_generation": str(generation),
        "profile": profile,
        "release_tag": tag,
        "release_fingerprint": manifest.get("release_fingerprint"),
        "downloaded_assets": downloaded,
        "unchanged_assets": unchanged,
        "migrated_legacy_install": bool(migration),
        "pruned_bytes": cleanup["removed_bytes"],
        "pruned_files": [item["path"] for item in cleanup["removed"]],
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    selection = sub.add_parser(
        "validate-selection",
        help="Validate selected assets against a captured V2 release and bundle manifest.",
    )
    selection.add_argument("--asset-dir", required=True)
    selection.add_argument("--manifest", required=True)
    selection.add_argument("--source-release-json")
    selection.add_argument("--asset", action="append", required=True)
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
    build.add_argument(
        "--source-health-waiver",
        help="JSON file containing scoped, expiring waivers for degraded sources.",
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
    install.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("TCIA_V2_NETWORK_TIMEOUT_SECONDS", DEFAULT_NETWORK_TIMEOUT_SECONDS)),
    )
    install.add_argument(
        "--retries",
        type=int,
        default=int(os.environ.get("TCIA_V2_NETWORK_RETRIES", DEFAULT_NETWORK_RETRIES)),
    )
    prune = sub.add_parser(
        "prune",
        help="Report or remove stale files owned by an official V2 install.",
    )
    prune.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR))
    prune.add_argument(
        "--apply",
        action="store_true",
        help="Delete the reported stale managed files; otherwise perform a dry run.",
    )
    prune.add_argument(
        "--stale-stage-hours",
        type=int,
        default=STALE_STAGE_MIN_AGE_HOURS,
        help="Minimum age for abandoned .tcia-v2-stage-* directories (default: 24).",
    )
    rollback = sub.add_parser(
        "rollback", help="Atomically select a verified retained V2 generation."
    )
    rollback.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR))
    rollback.add_argument(
        "--generation", help="Exact retained generation name; defaults to newest prior."
    )
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "validate-selection":
        result = validate_selected_bundle_assets(
            Path(args.asset_dir),
            Path(args.manifest),
            args.asset,
            release_json_path=(
                Path(args.source_release_json) if args.source_release_json else None
            ),
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
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
            source_health_waiver=(
                Path(args.source_health_waiver) if args.source_health_waiver else None
            ),
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
        return 0 if result["ok"] else 1
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
            timeout=args.timeout,
            retries=args.retries,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "prune":
        result = prune_install(
            Path(args.install_dir),
            apply=args.apply,
            stale_stage_hours=args.stale_stage_hours,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "rollback":
        result = rollback_install(Path(args.install_dir), args.generation)
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
