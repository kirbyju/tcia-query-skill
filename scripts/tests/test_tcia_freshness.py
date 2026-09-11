import importlib.util
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("tcia_freshness", SCRIPTS / "tcia_freshness.py")
FRESHNESS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(FRESHNESS)


class Response(io.BytesIO):
    def __init__(self, body: bytes, etag: str = '"etag-1"'):
        super().__init__(body)
        self.headers = {"ETag": etag}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class FreshnessTests(unittest.TestCase):
    def test_valid_receipt_avoids_remote_check_within_ttl(self):
        local = {"skill_version": "1", "files": {"SKILL.md": "abc"}}
        remote = dict(local)
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "receipt.json"
            FRESHNESS.write_receipt(
                cache,
                {
                    "schema_version": FRESHNESS.RECEIPT_SCHEMA_VERSION,
                    "repository": "owner/repo",
                    "ref": "main",
                    "checked_at_utc": FRESHNESS.now_utc().isoformat(),
                    "etag": '"etag-1"',
                    "remote_manifest": remote,
                    "remote_manifest_sha256": FRESHNESS.payload_sha256(remote),
                },
            )
            with mock.patch.object(FRESHNESS, "load_manifest", return_value=local), mock.patch.object(
                FRESHNESS, "validate_manifest", return_value={"ok": True}
            ), mock.patch.object(
                FRESHNESS, "remote_skill_manifest", side_effect=AssertionError("network not expected")
            ):
                result = FRESHNESS.check_skill(
                    "owner/repo", "main", cache_file=cache, ttl_seconds=3600
                )
            self.assertTrue(result["current"])
            self.assertEqual(result["remote_check"], "cached")

    def test_expired_receipt_uses_etag_and_304_refreshes_receipt(self):
        local = {"skill_version": "1", "files": {}}
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "receipt.json"
            FRESHNESS.write_receipt(
                cache,
                {
                    "schema_version": FRESHNESS.RECEIPT_SCHEMA_VERSION,
                    "repository": "owner/repo",
                    "ref": "main",
                    "checked_at_utc": "2020-01-01T00:00:00+00:00",
                    "etag": '"etag-1"',
                    "remote_manifest": local,
                    "remote_manifest_sha256": FRESHNESS.payload_sha256(local),
                },
            )
            with mock.patch.object(FRESHNESS, "load_manifest", return_value=local), mock.patch.object(
                FRESHNESS, "validate_manifest", return_value={"ok": True}
            ), mock.patch.object(
                FRESHNESS,
                "remote_skill_manifest",
                return_value=(None, '"etag-1"', True),
            ) as remote_call:
                result = FRESHNESS.check_skill(
                    "owner/repo", "main", cache_file=cache, ttl_seconds=60
                )
            self.assertEqual(result["remote_check"], "conditional_not_modified")
            self.assertEqual(remote_call.call_args.kwargs["etag"], '"etag-1"')
            self.assertIsNotNone(FRESHNESS.load_receipt(cache, "owner/repo", "main"))

    def test_github_request_retries_transient_network_failure(self):
        body = json.dumps({"content": "value"}).encode()
        with mock.patch.object(
            FRESHNESS.urllib.request,
            "urlopen",
            side_effect=[urllib.error.URLError("temporary"), Response(body)],
        ) as urlopen, mock.patch.object(FRESHNESS.time, "sleep"):
            payload, etag, not_modified = FRESHNESS.github_json(
                "https://example.invalid", timeout=3, retries=1
            )
        self.assertEqual(payload, {"content": "value"})
        self.assertEqual(etag, '"etag-1"')
        self.assertFalse(not_modified)
        self.assertEqual(urlopen.call_count, 2)

    def test_receipt_freshness_rejects_future_and_ttl_boundaries(self):
        at = dt.datetime(2026, 9, 11, 12, 0, tzinfo=dt.timezone.utc)
        cases = (
            ("2026-09-11T12:00:00+00:00", 60, True),
            ("2026-09-11T11:59:01+00:00", 60, True),
            ("2026-09-11T11:59:00+00:00", 60, False),
            ("2026-09-11T12:00:01+00:00", 60, False),
            ("2026-09-11T12:00:00+00:00", 0, False),
            ("2026-09-11T12:00:00", 60, True),
            ("2026-09-11T08:00:00-04:00", 60, True),
        )
        for checked_at, ttl, expected in cases:
            with self.subTest(checked_at=checked_at, ttl=ttl):
                self.assertEqual(
                    FRESHNESS.receipt_is_fresh(
                        {"checked_at_utc": checked_at}, ttl, at
                    ),
                    expected,
                )

    def test_github_request_retries_429_and_rate_limit_403(self):
        body = json.dumps({"content": "value"}).encode()
        limited = urllib.error.HTTPError(
            "https://example.invalid", 429, "limited", {"Retry-After": "0"}, None
        )
        forbidden = urllib.error.HTTPError(
            "https://example.invalid",
            403,
            "limited",
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"},
            None,
        )
        with mock.patch.object(
            FRESHNESS.urllib.request,
            "urlopen",
            side_effect=[limited, forbidden, Response(body)],
        ) as urlopen, mock.patch.object(FRESHNESS.time, "sleep"):
            payload, _, _ = FRESHNESS.github_json(
                "https://example.invalid", retries=2
            )
        self.assertEqual(payload, {"content": "value"})
        self.assertEqual(urlopen.call_count, 3)


if __name__ == "__main__":
    unittest.main()
