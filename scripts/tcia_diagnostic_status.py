#!/usr/bin/env python3
"""Maintain an atomic diagnostics-only status sentinel for source CI."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def atomic_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def initialize(path: Path, *, run_id: str, run_attempt: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "artifact_role": "diagnostics_only_nonvalidated",
        "diagnostics_only": True,
        "eligible_as_release_input": False,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "stage": "source_build_started",
        "disposition": "pending",
        "exit_code": None,
        "report_json_present": False,
        "report_markdown_present": False,
        "updated_at_utc": now_utc(),
    }
    atomic_write(path, payload)
    return payload


def load_validated(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version") != 1
        or payload.get("artifact_role") != "diagnostics_only_nonvalidated"
        or payload.get("diagnostics_only") is not True
        or payload.get("eligible_as_release_input") is not False
    ):
        raise ValueError("diagnostic status sentinel has an invalid artifact role")
    return payload


def update(
    path: Path,
    *,
    stage: str,
    disposition: str,
    exit_code: int | None,
    report_json: Path | None,
    report_markdown: Path | None,
) -> dict[str, object]:
    payload = load_validated(path)
    payload.update({
        "stage": stage,
        "disposition": disposition,
        "exit_code": exit_code,
        "report_json_present": bool(report_json and report_json.is_file()),
        "report_markdown_present": bool(report_markdown and report_markdown.is_file()),
        "updated_at_utc": now_utc(),
    })
    atomic_write(path, payload)
    return payload


def finalize(
    path: Path,
    *,
    job_status: str,
    report_json: Path | None,
    report_markdown: Path | None,
) -> dict[str, object]:
    payload = load_validated(path)
    if payload.get("disposition") == "pending":
        payload = update(
            path,
            stage="failed_before_semantic_report",
            disposition="pre_report_failure",
            exit_code=None,
            report_json=report_json,
            report_markdown=report_markdown,
        )
    payload["job_status_before_diagnostic_upload"] = job_status
    payload["updated_at_utc"] = now_utc()
    atomic_write(path, payload)
    return payload


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--out", type=Path, required=True)
    init.add_argument("--run-id", required=True)
    init.add_argument("--run-attempt", required=True)
    change = sub.add_parser("update")
    change.add_argument("--status", type=Path, required=True)
    change.add_argument("--stage", required=True)
    change.add_argument("--disposition", required=True)
    change.add_argument("--exit-code", type=int)
    change.add_argument("--report-json", type=Path)
    change.add_argument("--report-markdown", type=Path)
    final = sub.add_parser("finalize")
    final.add_argument("--status", type=Path, required=True)
    final.add_argument("--job-status", required=True)
    final.add_argument("--report-json", type=Path)
    final.add_argument("--report-markdown", type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "init":
        payload = initialize(args.out, run_id=args.run_id, run_attempt=args.run_attempt)
    elif args.command == "update":
        payload = update(
            args.status,
            stage=args.stage,
            disposition=args.disposition,
            exit_code=args.exit_code,
            report_json=args.report_json,
            report_markdown=args.report_markdown,
        )
    else:
        payload = finalize(
            args.status,
            job_status=args.job_status,
            report_json=args.report_json,
            report_markdown=args.report_markdown,
        )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
