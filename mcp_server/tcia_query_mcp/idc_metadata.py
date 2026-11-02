"""Bounded reads of IDC's versioned public metadata API; no payload transfers."""

from collections import OrderedDict
from copy import deepcopy
import json
import re
import threading
from time import monotonic
from urllib.request import Request, urlopen

BASE_URL = "https://api.imaging.datacommons.cancer.gov"
COLUMNS = (
    "collection_id",
    "analysis_result_id",
    "source_DOI",
    "PatientID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "Modality",
    "instanceCount",
    "series_size_MB",
    "license_short_name",
)


class MetadataUnavailable(RuntimeError):
    pass


class IdcMetadataClient:
    def __init__(self, request=None, *, max_rows=10000):
        self.request = request or self._request
        self.max_rows = max_rows
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _request(path, payload, timeout):
        body = None if payload is None else json.dumps(payload).encode()
        req = Request(
            BASE_URL + path, data=body, headers={"Content-Type": "application/json"}
        )
        with urlopen(req, timeout=timeout) as response:
            data = response.read(8_000_001)
        if len(data) > 8_000_000:
            raise MetadataUnavailable(
                "IDC metadata response exceeds the supported size"
            )
        return json.loads(data)

    def _call(self, path, payload, deadline):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise MetadataUnavailable("IDC metadata planning timed out")
        try:
            result = self.request(path, payload, min(15, remaining))
        except MetadataUnavailable:
            raise
        except Exception:
            raise MetadataUnavailable(
                "IDC metadata is unavailable; retry the plan"
            ) from None
        if not isinstance(result, dict):
            raise MetadataUnavailable("IDC returned malformed metadata")
        return result

    def _version(self, deadline):
        raw = self._call("/v3/version", None, deadline)
        keys = ("idc_version", "idc_index_data_version", "api_version")
        if any(not isinstance(raw.get(k), str) or not raw[k] for k in keys):
            raise MetadataUnavailable("IDC version metadata is incomplete")
        return {k: raw.get(k) for k in (*keys, "build")}

    def resolve(self, doi, patient_ids, modality, *, collection_ids=(), time_budget=45):
        deadline = monotonic() + min(45, time_budget)
        version = self._version(deadline)
        patient_ids = tuple(sorted(set(patient_ids)))
        collection_ids = tuple(sorted(set(collection_ids)))
        key = (
            json.dumps(version, sort_keys=True),
            doi,
            patient_ids,
            modality,
            collection_ids,
        )
        with self._lock:
            cached = self._cache.get(key)
            if cached and monotonic() - cached[0] < 60:
                return deepcopy(cached[1])
        where = [
            "lower(source_DOI) = ?",
            "PatientID IN (" + ",".join("?" for _ in patient_ids) + ")",
            "upper(Modality) = ?",
        ]
        params = [doi.lower(), *patient_ids, modality.upper()]
        if collection_ids:
            where.append(
                "collection_id IN (" + ",".join("?" for _ in collection_ids) + ")"
            )
            params.extend(collection_ids)
        rows, seen, last = [], set(), ""
        while True:
            sql = (
                "SELECT "
                + ",".join(COLUMNS)
                + ' FROM "index" WHERE '
                + " AND ".join(where)
                + " AND SeriesInstanceUID > ? ORDER BY SeriesInstanceUID LIMIT 1000"
            )
            page = self._call(
                "/v3/sql",
                {"sql": sql, "params": [*params, last], "max_rows": 1000},
                deadline,
            )
            batch = page.get("rows")
            if (
                not isinstance(batch, list)
                or page.get("row_count") != len(batch)
                or page.get("truncated") is not False
                or len(batch) > 1000
            ):
                raise MetadataUnavailable(
                    "IDC returned a truncated or invalid metadata page"
                )
            for row in batch:
                if not isinstance(row, dict) or any(k not in row for k in COLUMNS):
                    raise MetadataUnavailable(
                        "IDC series metadata lacks required fields"
                    )
                uid = row["SeriesInstanceUID"]
                if (
                    not isinstance(uid, str)
                    or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", uid)
                    or uid <= last
                    or uid in seen
                    or row["PatientID"] not in patient_ids
                    or not isinstance(row["source_DOI"], str)
                    or row["source_DOI"].lower() != doi.lower()
                    or row["Modality"] != modality.upper()
                    or (collection_ids and row["collection_id"] not in collection_ids)
                ):
                    raise MetadataUnavailable(
                        "IDC returned series outside the requested selection"
                    )
                seen.add(uid)
                last = uid
                rows.append(dict(row))
            if len(rows) > self.max_rows:
                raise MetadataUnavailable(
                    "IDC selection exceeds the plan limit; split the participant selection"
                )
            if len(batch) < 1000:
                break
        if self._version(deadline) != version:
            raise MetadataUnavailable(
                "IDC data version changed during planning; retry the plan"
            )
        result = {"rows": rows, "version": version}
        with self._lock:
            self._cache[key] = (monotonic(), deepcopy(result))
            self._cache.move_to_end(key)
            while len(self._cache) > 16:
                self._cache.popitem(last=False)
        return result
