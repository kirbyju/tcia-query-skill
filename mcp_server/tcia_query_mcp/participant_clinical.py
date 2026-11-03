"""Clinical predicates for participant search, applied before paging read-only data."""

import json
import sqlite3

from .service import InvalidRequestError, ServiceUnavailableError


class ClinicalParticipantSearch:
    def __init__(self, service, filters):
        self.service = service
        self.diagnosis = self._text(filters.get("diagnosis"), "diagnosis")
        self.primary_site = self._text(filters.get("primary_site"), "primary_site")
        self.include_inferred = filters.get("include_inferred", True)
        if type(self.include_inferred) is not bool:
            raise InvalidRequestError("include_inferred must be a boolean")
        self.active = bool(self.diagnosis or self.primary_site or not self.include_inferred)
        self.identities = {}
        self.params = []
        self.cte = ""
        if not self.active:
            return
        kind = str(filters.get("dataset_type") or "both").strip().casefold().replace("_", " ")
        if kind not in {"both", "collection"}:
            raise InvalidRequestError("Clinical participant filters currently support Collections only")
        self.identities = self._identities()
        if len({value.split(":", 1)[0] for value in self.identities.values()}) != 1:
            raise ServiceUnavailableError("Clinical and participant metadata generations differ")
        clauses = []
        for column, value in (("primary_diagnosis", self.diagnosis), ("primary_site", self.primary_site)):
            if value is not None:
                clauses.append(f"instr(tcia_casefold(c.{column}), ?) > 0")
                self.params.append(value.casefold())
                if not self.include_inferred:
                    clauses.extend([
                        f"c.{column}_is_inferred = 0",
                        f"json_extract(c.resolved_sources_json, '$.{column}.evidence_scope') "
                        "IN ('patient', 'patient_inherited')",
                    ])
        if not self.include_inferred:
            # A no-value clinical query still requests patient-level evidence.
            clauses.append("""EXISTS (
                SELECT 1 FROM json_each(c.resolved_sources_json) AS source
                WHERE json_extract(CASE WHEN source.type = 'object'
                    THEN source.value ELSE '{}' END, '$.evidence_scope')
                    IN ('patient', 'patient_inherited')
            )""")
        self.cte = (
            "WITH clinical_matches AS MATERIALIZED ("
            "SELECT c.short_title COLLATE NOCASE AS short_title, c.subject_id "
            "FROM tcia_clinical.agent_clinical_subjects c WHERE "
            + " AND ".join(clauses or ["1 = 1"]) + ") "
        )

    @staticmethod
    def _text(value, name):
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
            raise InvalidRequestError(f"{name} must be a nonblank string of at most 200 characters")
        return value.strip()

    def _identities(self):
        try:
            return {
                asset: self.service._component_identity(asset)
                for asset in ("tcia_snapshot.sqlite.gz", "participant_inventory.sqlite.gz", "clinical_metadata.sqlite.gz")
            }
        except (OSError, sqlite3.Error):
            raise ServiceUnavailableError("Required clinical participant search metadata is unavailable") from None

    @property
    def query(self):
        if not self.active:
            return None
        return {
            "diagnosis": self.diagnosis,
            "primary_site": self.primary_site,
            "include_inferred": self.include_inferred,
            "match_mode": "case_insensitive_substring",
            "dataset_scope": "Collection",
            "metadata_snapshot": next(iter(self.identities.values())).split(":", 1)[0],
        }

    def attach(self, conn):
        if not self.active:
            return
        try:
            conn.execute("ATTACH DATABASE ? AS tcia_clinical", (
                self.service.clinical_db.resolve().as_uri() + "?mode=ro",
            ))
            conn.execute("""SELECT short_title, subject_id, primary_diagnosis, primary_site,
                primary_diagnosis_is_inferred, primary_site_is_inferred, resolved_sources_json
                FROM tcia_clinical.agent_clinical_subjects LIMIT 0""")
            conn.create_function("tcia_casefold", 1, lambda v: v.casefold() if isinstance(v, str) else "", deterministic=True)
            if not self.include_inferred:
                invalid = conn.execute("""SELECT 1 FROM tcia_clinical.agent_clinical_subjects
                    WHERE CASE WHEN json_valid(resolved_sources_json)
                    THEN json_type(resolved_sources_json) ELSE NULL END IS NOT 'object' LIMIT 1""").fetchone()
                if invalid:
                    raise ServiceUnavailableError("Clinical source evidence is invalid")
        except sqlite3.Error:
            raise ServiceUnavailableError("Clinical metadata cannot support participant search") from None

    def predicate(self, alias):
        if not self.active:
            return "1 = 1"
        return (
            f"lower({alias}.dataset_type) = 'collection' AND EXISTS ("
            "SELECT 1 FROM clinical_matches cm WHERE "
            f"cm.short_title = {alias}.short_title COLLATE NOCASE AND "
            f"cm.subject_id = {alias}.display_participant_id COLLATE BINARY)"
        )

    def finish(self, conn, rows, visible):
        if not self.active:
            return
        if rows:
            wanted = {(r["short_title"].casefold(), r["display_participant_id"]) for r in rows}
            for title, _identifier in wanted:
                if len({kind for kind, t in visible if t == title}) != 1:
                    raise ServiceUnavailableError("Clinical metadata cannot disambiguate this short_title across dataset types")
            titles, ids = sorted({k[0] for k in wanted}), sorted({k[1] for k in wanted})
            sql = (
                "SELECT short_title, subject_id, primary_diagnosis, primary_site, "
                "primary_diagnosis_is_inferred, primary_site_is_inferred, resolved_sources_json "
                "FROM tcia_clinical.agent_clinical_subjects WHERE "
                f"lower(short_title) IN ({','.join('?' for _ in titles)}) AND "
                f"subject_id COLLATE BINARY IN ({','.join('?' for _ in ids)})"
            )
            found = {}
            for record in conn.execute(sql, [*titles, *ids]):
                key = (record["short_title"].casefold(), record["subject_id"])
                if key not in wanted:
                    continue
                if key in found:
                    raise ServiceUnavailableError("Clinical subject identity is ambiguous")
                try:
                    sources = json.loads(record["resolved_sources_json"])
                    if not isinstance(sources, dict):
                        raise ValueError()
                    match = {k: record[k] for k in ("subject_id", "primary_diagnosis", "primary_site")}
                    match["has_patient_level_clinical_evidence"] = any(
                        isinstance(source, dict) and source.get("evidence_scope") in {"patient", "patient_inherited"}
                        for source in sources.values()
                    )
                    for name, label in (("primary_diagnosis", "diagnosis"), ("primary_site", "site")):
                        flag = record[name + "_is_inferred"]
                        if flag is not None and (type(flag) is not int or flag not in {0, 1}):
                            raise ValueError()
                        match[name + "_is_inferred"] = None if flag is None else bool(flag)
                        source = sources.get(name)
                        scope = source.get("evidence_scope") if isinstance(source, dict) else None
                        match[label + "_evidence_scope"] = scope if isinstance(scope, str) else None
                    found[key] = match
                except (ValueError, TypeError):
                    raise ServiceUnavailableError("Clinical match evidence is invalid") from None
            for row in rows:
                key = (row["short_title"].casefold(), row["display_participant_id"])
                if key not in found:
                    raise ServiceUnavailableError("Clinical subject identity changed during search")
                row["clinical_match"] = found[key]
        if self._identities() != self.identities:
            raise ServiceUnavailableError("Clinical or participant metadata changed during search; restart the query")
