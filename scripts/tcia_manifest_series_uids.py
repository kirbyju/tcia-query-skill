#!/usr/bin/env python3
"""Extract DICOM Series Instance UIDs from a legacy TCIA .tcia manifest."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Iterable


UID_RE = re.compile(r"\b\d+(?:\.\d+)+\b")
MIN_UID_LENGTH = 10
MAX_UID_LENGTH = 64


def read_text(source: str) -> str:
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": "tcia-query-skill/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8", errors="replace")
    return Path(source).read_text(encoding="utf-8", errors="replace")


def uid_candidates(line: str) -> Iterable[str]:
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";")):
        return []
    return (
        match.group(0)
        for match in UID_RE.finditer(stripped)
        if MIN_UID_LENGTH <= len(match.group(0)) <= MAX_UID_LENGTH
    )


def extract_uids(text: str) -> list[str]:
    uids: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        for uid in uid_candidates(line):
            if uid not in seen:
                seen.add(uid)
                uids.append(uid)
    return uids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="Local legacy .tcia manifest path or manifest URL.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of newline text.")
    parser.add_argument("--comma", action="store_true", help="Print comma-separated UIDs.")
    parser.add_argument("--out", help="Write newline-delimited UIDs to this file.")
    args = parser.parse_args(argv)

    uids = extract_uids(read_text(args.manifest))
    if args.out:
        Path(args.out).write_text("\n".join(uids) + ("\n" if uids else ""), encoding="utf-8")

    if args.json:
        print(json.dumps({"series_instance_uids": uids, "count": len(uids)}, indent=2))
    elif args.comma:
        print(",".join(uids))
    else:
        print("\n".join(uids))
    return 0 if uids else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as exc:
        print(f"Could not read manifest: {exc}", file=sys.stderr)
        raise SystemExit(2)
