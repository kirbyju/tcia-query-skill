"""Keep reported series sizes separate from exact controlled payload bytes."""

import re


def _source_rows(conn, table, key, ids, columns):
    if not ids:
        return {}
    sql = f"SELECT {key}, {columns} FROM {table} WHERE {key} IN ({','.join('?' for _ in ids)})"
    return {row[key]: dict(row) for row in conn.execute(sql, sorted(ids))}


def _exact_bytes(label):
    if not isinstance(label, str):
        return None
    # Rounded KB/MB labels are estimates, regardless of the supplier's unit base.
    match = re.fullmatch(r"\s*([0-9]+)\s*(?:B|bytes)?\s*", label, re.IGNORECASE)
    return int(match[1]) if match else None


def add_transfer_sizes(conn, rows):
    """Enrich selected public rows using existing manifest/series provenance only.

    file_size_bytes remains the existing reported value. Only a manifest's
    explicit integer byte count becomes download_size_bytes. This never resolves
    a DRS URI or retrieves a controlled payload.
    """
    manifests = _source_rows(conn, "manifest_rows", "manifest_row_id",
        {r["manifest_row_id"] for r in rows if r.get("manifest_row_id")},
        "short_title, drs_uri, file_size_text, file_size_bytes")
    metadata = _source_rows(conn, "metadata_rows", "metadata_row_id",
        {r["metadata_row_id"] for r in rows if r.get("metadata_row_id")},
        "short_title, file_size_bytes")
    for row in rows:
        manifest = manifests.get(row.get("manifest_row_id"))
        series = metadata.get(row.get("metadata_row_id"))
        for key, source in (("manifest_row_id", manifest), ("metadata_row_id", series)):
            if row.get(key) and (source is None or str(source["short_title"]).casefold() != str(row["short_title"]).casefold()):
                raise ValueError("Controlled size provenance has inconsistent dataset identity")
        if manifest and manifest.get("drs_uri") != row.get("drs_uri"):
            raise ValueError("Controlled size provenance has inconsistent file identity")
        label = manifest.get("file_size_text") if manifest else None
        exact = _exact_bytes(label)
        row["download_size_bytes"] = exact
        row["download_size_label"] = label or None
        size = row.get("file_size_bytes")
        if size is None:
            basis = "unknown"
        elif series and series.get("file_size_bytes") is not None:
            basis = "series_metadata"
        elif manifest and manifest.get("file_size_bytes") is not None:
            basis = "manifest_exact" if exact is not None else "manifest_rounded"
        else:
            basis = "unknown"
        row["file_size_basis"] = basis
