"""Lossless research storage projections; agent views retain the public contract.

Assembly databases keep the writable producer schema. Compact research files
store primary source-location references and only nonredundant image JSON.
"""
from __future__ import annotations

import re
import sqlite3

ASSET_METADATA_FIELDS = (
    "file_format", "media_kind", "object_role",
    "spatial_dimensionality", "temporal_dimensionality",
)


def quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def is_compact(conn: sqlite3.Connection) -> bool:
    return "source_location_id" in {
        row[1] for row in conn.execute("PRAGMA table_info(public_non_dicom_assets)")
    }


def source_url_sql(alias: str = "a") -> str:
    return (f"COALESCE((SELECT l.access_url FROM public_non_dicom_locations l "
            f"WHERE l.location_id={alias}.source_location_id), {alias}.source_url_override)")


def metadata_sql(metadata_alias: str = "m", asset_alias: str = "a") -> str:
    expression = f"{metadata_alias}.metadata_json"
    for bit, field in enumerate(ASSET_METADATA_FIELDS):
        expression = (
            f"json_patch({expression}, CASE WHEN ({metadata_alias}.asset_metadata_mask & {1 << bit}) != 0 "
            f"THEN json_object('{field}', {asset_alias}.{field}) ELSE '{{}}' END)"
        )
    return expression


def table_sql(table: str, sql: str) -> str:
    if table == "public_non_dicom_assets":
        return sql.replace("source_url TEXT,", "source_location_id TEXT REFERENCES public_non_dicom_locations(location_id), source_url_override TEXT,")
    if table == "public_non_dicom_image_metadata":
        return sql.replace("metadata_json TEXT NOT NULL DEFAULT '{}'", "asset_metadata_mask INTEGER NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL DEFAULT '{}'")
    return sql


def copy_projection(conn: sqlite3.Connection, table: str, columns: list[str],
                    replacements: dict[str, str]) -> None:
    """Copy an assembly table from attached source without storing duplicates."""
    names, expressions, params = [], [], []
    for column in columns:
        if table == "public_non_dicom_assets" and column == "source_url":
            names.extend(["source_location_id", "source_url_override"])
            match = ("SELECT l.location_id FROM source.public_non_dicom_locations l "
                     "WHERE l.asset_id=s.asset_id AND l.access_url=s.source_url "
                     "ORDER BY l.location_id LIMIT 1")
            expressions.extend([f"({match})", f"CASE WHEN ({match}) IS NULL THEN s.source_url END"])
        elif table == "public_non_dicom_image_metadata" and column == "metadata_json":
            mask, expression = [], "s.metadata_json"
            for bit, field in enumerate(ASSET_METADATA_FIELDS):
                same = (f"json_type(s.metadata_json, '$.{field}') IS NOT NULL AND "
                        f"json_extract(s.metadata_json, '$.{field}') IS a.{field} AND a.{field} IS NOT NULL")
                mask.append(f"CASE WHEN {same} THEN {1 << bit} ELSE 0 END")
                expression = f"CASE WHEN {same} THEN json_remove({expression}, '$.{field}') ELSE {expression} END"
            names.extend(["asset_metadata_mask", "metadata_json"])
            expressions.extend([" + ".join(mask), expression])
        else:
            names.append(column)
            if column in replacements:
                expressions.append("?"); params.append(replacements[column])
            else:
                expressions.append("s." + quote(column))
    join = (" JOIN source.public_non_dicom_assets a USING(asset_id)"
            if table == "public_non_dicom_image_metadata" else "")
    conn.execute(f"INSERT INTO main.{quote(table)} ({','.join(map(quote,names))}) "
                 f"SELECT {','.join(expressions)} FROM source.{quote(table)} s{join}", params)


def view_sql(conn: sqlite3.Connection, sql: str, asset_columns: list[str]) -> str:
    sql = sql.replace("    a.source_url\n", "    a.source_url AS source_url\n")
    sql = re.sub(r"\ba\.source_url\b", lambda _: source_url_sql(), sql)
    # agent assets expose the old column order, without internal storage fields.
    sql = sql.replace("a.*", ", ".join(
        source_url_sql() + " AS source_url" if col == "source_url" else "a." + quote(col)
        for col in asset_columns))
    if "FROM public_non_dicom_image_metadata m" in sql:
        # Normalized acquisition columns read the compact JSON directly. None
        # of those keys is removed. Reconstruct only the exported JSON document,
        # rather than evaluating five JSON patches again for every scalar field.
        sql = sql.replace("    m.metadata_json,", "    " + metadata_sql() + " AS metadata_json,")
    return sql


def expand_assembly(conn: sqlite3.Connection) -> None:
    """Restore producer columns before applying the audit companion or refreshing."""
    if not is_compact(conn):
        return
    views = list(conn.execute("SELECT name, sql FROM sqlite_master WHERE type='view'"))
    conn.execute("CREATE TEMP TABLE expanded_metadata AS SELECT asset_id, metadata_json FROM agent_public_non_dicom_image_metadata")
    conn.execute("CREATE UNIQUE INDEX expanded_metadata_asset ON expanded_metadata(asset_id)")
    conn.execute("CREATE TEMP TABLE expanded_urls AS SELECT asset_id, source_url FROM agent_public_non_dicom_assets")
    conn.execute("CREATE UNIQUE INDEX expanded_urls_asset ON expanded_urls(asset_id)")
    for name, _ in views:
        conn.execute("DROP VIEW " + quote(name))
    conn.execute("ALTER TABLE public_non_dicom_assets ADD COLUMN source_url TEXT")
    conn.execute("UPDATE public_non_dicom_assets AS a SET source_url=(SELECT source_url FROM expanded_urls u WHERE u.asset_id=a.asset_id)")
    conn.execute("UPDATE public_non_dicom_image_metadata AS m SET metadata_json=(SELECT metadata_json FROM expanded_metadata e WHERE e.asset_id=m.asset_id)")
    conn.execute("ALTER TABLE public_non_dicom_assets DROP COLUMN source_location_id")
    conn.execute("ALTER TABLE public_non_dicom_assets DROP COLUMN source_url_override")
    conn.execute("ALTER TABLE public_non_dicom_image_metadata DROP COLUMN asset_metadata_mask")
    conn.execute("DROP TABLE expanded_metadata"); conn.execute("DROP TABLE expanded_urls")
    # Restore the canonical producer views, including ordinary asset/source fields.
    try:
        from tcia_public_non_dicom_metadata import SCHEMA
    except ModuleNotFoundError:
        from scripts.tcia_public_non_dicom_metadata import SCHEMA
    for statement in SCHEMA.split(";"):
        if statement.strip().upper().startswith("CREATE VIEW"):
            conn.execute(statement)
    conn.execute("DELETE FROM artifact_meta WHERE key='research_storage_contract'")


def install_comparison_views(conn: sqlite3.Connection) -> None:
    """Compare logical content across storage generations without waiving changes."""
    if not is_compact(conn):
        return
    asset_columns = [row[1] for row in conn.execute("PRAGMA main.table_info(public_non_dicom_assets)")
                     if row[1] not in {"source_location_id", "source_url_override"}]
    asset_columns.append("source_url")
    conn.execute("CREATE TEMP VIEW public_non_dicom_assets AS SELECT " +
                 ",".join(map(quote, asset_columns)) + " FROM main.agent_public_non_dicom_assets")
    metadata_columns = [row[1] for row in conn.execute("PRAGMA main.table_info(public_non_dicom_image_metadata)")
                        if row[1] != "asset_metadata_mask"]
    conn.execute("CREATE TEMP VIEW public_non_dicom_image_metadata AS SELECT " +
                 ",".join(map(quote, metadata_columns)) + " FROM main.agent_public_non_dicom_image_metadata")
