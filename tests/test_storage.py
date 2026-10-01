import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from solid_sync.app import app as sync


def snapshot(index):
    return {
        "captured_at": f"2026-09-01T12:00:{index:02d}+00:00",
        "measurements": {
            "temperature": {"state": str(20 + index), "attributes": {"unit_of_measurement": "°C"}}
        },
    }


class AtomicStorageTests(unittest.TestCase):
    def test_failed_replace_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(b'{"old":true}')
            with patch.object(sync.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    sync.atomic_write(path, b'{"new":true}')
            self.assertEqual(json.loads(path.read_bytes()), {"old": True})
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_failed_fsync_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(b'{"old":true}')
            with patch.object(sync.os, "fsync", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    sync.atomic_write(path, b'{"new":true}')
            self.assertEqual(json.loads(path.read_bytes()), {"old": True})


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config_path = Path(self.directory.name) / "solid-sync.json"
        patcher = patch.object(sync, "CONFIG_PATH", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.body = sync.encode_json({"entries": [snapshot(0)]})
        self.etag = '"v1"'
        self.puts = []
        self.gets = []
        self.mode = "ok"
        server_app = web.Application()
        server_app.router.add_route("*", "/{name}", self.resource)
        self.server = TestServer(server_app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        self.addAsyncCleanup(self.session.close)
        self.client = sync.SolidOIDCClient(
            self.session, str(self.server.make_url("/")), str(self.server.make_url("/")), "test", "test"
        )
        self.client.get_access_token = AsyncMock(return_value="test-token")
        self.service = sync.SolidSyncService()
        self.service._settings = sync.SolidSettings("https://issuer.test", "https://pod.test", "test", "test")
        self.service._client = self.client
        # Keep legacy client tests separate from the gzip migration tests below.
        self.service._target_path = lambda profile: profile.resource_path
        self.profile = sync.SyncProfile(
            "test-profile", "Garden", "history.json", pending_entries=[snapshot(1)],
            next_flush_at="2026-09-02T12:00:00+00:00",
        )
        self.service._profiles[self.profile.id] = self.profile
        await self.service._save_config()

    async def resource(self, request):
        if request.method == "GET":
            self.gets.append(dict(request.headers))
            if self.mode == "verify-unavailable" and self.puts:
                return web.Response(status=503)
            if self.body is None:
                return web.Response(status=404)
            headers = {"ETag": self.etag} if self.etag else {}
            return web.Response(body=self.body, content_type="application/json", headers=headers)
        self.puts.append(dict(request.headers))
        body = await request.read()
        if self.mode == "concurrent":
            self.body = sync.encode_json({"entries": [snapshot(0), snapshot(2)]})
            self.etag = '"other-writer"'
        if request.headers.get("If-Match") and request.headers["If-Match"] != self.etag:
            return web.Response(status=412)
        if request.headers.get("If-None-Match") == "*" and self.body is not None:
            return web.Response(status=412)
        self.body = body
        self.etag = '"v2"'
        if self.mode == "truncate":
            self.body = body[:-12]
        elif self.mode == "drop-entry":
            document = json.loads(body)
            document["entries"].pop()
            self.body = sync.encode_json(document)
        elif self.mode == "lost-response":
            return web.Response(status=500)
        return web.Response(status=204)

    async def flush(self):
        await self.service._flush_profile(self.profile.id, suppress_errors=False)

    def saved_pending(self):
        return json.loads(self.config_path.read_bytes())["profiles"][0]["pending_entries"]

    def recovery(self):
        return json.loads(gzip.decompress(self.service._recovery_path(self.profile).read_bytes()))

    async def test_success_is_verified_and_recoverable(self):
        await self.flush()
        document = json.loads(self.body)
        self.assertEqual(document["entries"], [snapshot(0), snapshot(1)])
        self.assertEqual(self.recovery(), document)
        self.assertEqual(self.saved_pending(), [])
        self.assertEqual(self.puts[0]["If-Match"], '"v1"')
        self.assertEqual(len(self.gets), 2)
        self.assertTrue(all(headers["Cache-Control"] == "no-cache" for headers in self.gets))
        self.assertTrue(all(headers["Accept-Encoding"] == "identity" for headers in self.gets))
        self.assertNotIn(b'": ', self.body)

    async def test_extra_history_metadata_survives(self):
        entry = snapshot(0)
        entry["quality"] = "calibrated"
        self.body = sync.encode_json({"entries": [entry], "station": {"altitude": 123}})
        await self.flush()
        document = json.loads(self.body)
        self.assertEqual(document["entries"][0], entry)
        self.assertEqual(document["station"], {"altitude": 123})

    async def test_new_resource_uses_create_precondition(self):
        self.body = None
        await self.flush()
        self.assertEqual(self.puts[0]["If-None-Match"], "*")
        self.assertEqual(json.loads(self.body)["entries"], [snapshot(1)])

    async def test_truncated_success_keeps_queue_and_full_recovery_copy(self):
        self.mode = "truncate"
        with self.assertRaisesRegex(RuntimeError, "not valid JSON"):
            await self.flush()
        self.assertEqual(self.saved_pending(), [snapshot(1)])
        self.assertEqual(self.recovery()["entries"], [snapshot(0), snapshot(1)])
        recovery_bytes = self.service._recovery_path(self.profile).read_bytes()
        with self.assertRaisesRegex(RuntimeError, "not valid JSON"):
            await self.flush()
        self.assertEqual(self.service._recovery_path(self.profile).read_bytes(), recovery_bytes)
        self.assertEqual(len(self.puts), 1)

    async def test_valid_but_incomplete_response_does_not_clear_queue(self):
        self.mode = "drop-entry"
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            await self.flush()
        self.assertEqual(self.saved_pending(), [snapshot(1)])

    async def test_retry_after_lost_response_does_not_duplicate(self):
        self.mode = "lost-response"
        with self.assertRaisesRegex(RuntimeError, "PUT failed"):
            await self.flush()
        self.assertEqual(self.saved_pending(), [snapshot(1)])
        # Simulate restart before retry.
        await self.service._load_config()
        self.service._client = self.client
        self.profile = self.service._profiles[self.profile.id]
        self.mode = "ok"
        await self.flush()
        self.assertEqual(json.loads(self.body)["entries"], [snapshot(0), snapshot(1)])
        self.assertEqual(self.saved_pending(), [])

    async def test_unavailable_verification_keeps_queue(self):
        self.mode = "verify-unavailable"
        with self.assertRaisesRegex(RuntimeError, "GET failed"):
            await self.flush()
        self.assertEqual(self.saved_pending(), [snapshot(1)])

    async def test_concurrent_writer_is_not_overwritten(self):
        self.mode = "concurrent"
        with self.assertRaisesRegex(RuntimeError, "changed during upload"):
            await self.flush()
        self.assertEqual(json.loads(self.body)["entries"], [snapshot(0), snapshot(2)])
        self.assertEqual(self.saved_pending(), [snapshot(1)])

    async def test_missing_etag_prevents_unsafe_overwrite(self):
        for etag in (None, 'W/"weak"'):
            with self.subTest(etag=etag):
                self.etag = etag
                with self.assertRaisesRegex(RuntimeError, "strong ETag"):
                    await self.flush()
        self.assertEqual(self.puts, [])

    async def test_malformed_remote_entry_is_not_silently_dropped(self):
        self.body = sync.encode_json({"entries": [snapshot(0), {"unexpected": True}]})
        with self.assertRaisesRegex(RuntimeError, "unsupported entry"):
            await self.flush()
        self.assertEqual(self.puts, [])
        self.assertEqual(self.saved_pending(), [snapshot(1)])

    async def test_empty_or_null_existing_resource_is_not_overwritten(self):
        for body in (b"", b"null"):
            with self.subTest(body=body):
                self.body = body
                with self.assertRaises(RuntimeError):
                    await self.flush()
        self.assertEqual(self.puts, [])

    async def test_recovery_disk_failure_prevents_put(self):
        real_write = sync.atomic_write

        def fail_recovery(path, data):
            if path.suffix == ".gz":
                raise OSError("disk full")
            real_write(path, data)

        with patch.object(sync, "atomic_write", side_effect=fail_recovery):
            with self.assertRaisesRegex(RuntimeError, "disk full"):
                await self.flush()
        self.assertEqual(self.puts, [])
        self.assertEqual(self.saved_pending(), [snapshot(1)])

    async def test_failed_queue_commit_can_retry_without_duplicates(self):
        with patch.object(self.service, "_save_config", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                await self.flush()
        self.assertEqual(self.profile.pending_entries, [snapshot(1)])
        self.assertEqual(self.saved_pending(), [snapshot(1)])
        await self.flush()
        self.assertEqual(json.loads(self.body)["entries"], [snapshot(0), snapshot(1)])

    async def test_new_snapshot_does_not_hide_upload_error(self):
        self.profile.last_error = "upload failed"
        with patch.object(self.service, "_build_snapshot", AsyncMock(return_value=snapshot(2))):
            await self.service._queue_profile_snapshot(self.profile.id, suppress_errors=False)
        self.assertEqual(self.profile.last_error, "upload failed")
        self.assertEqual(self.saved_pending(), [snapshot(1), snapshot(2)])


class GzipUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await UploadTests.asyncSetUp(self)
        del self.service._target_path
        self.legacy_body = self.body
        self.gzip_body = None
        self.gzip_content_type = "application/gzip"
        self.paths = []

    async def resource(self, request):
        self.paths.append((request.method, request.path))
        if request.path == "/history.json":
            self.assertEqual(request.method, "GET")
            return web.Response(body=self.legacy_body, content_type="application/json", headers={"ETag": '"legacy"'})
        if request.method == "GET":
            if self.gzip_body is None:
                return web.Response(status=404)
            accepted = {part.split(";", 1)[0].strip() for part in request.headers.get("Accept", "").split(",")}
            if self.gzip_content_type not in accepted and "*/*" not in accepted:
                return web.Response(status=501, text="No conversion path for stored gzip media type")
            return web.Response(body=self.gzip_body, content_type=self.gzip_content_type, headers={"ETag": '"gzip"'})
        self.puts.append(dict(request.headers))
        self.assertEqual(request.headers["Content-Type"], self.gzip_content_type)
        self.assertNotIn("Content-Encoding", request.headers)
        self.assertEqual(request.headers.get("If-Match") if self.gzip_body else request.headers.get("If-None-Match"), '"gzip"' if self.gzip_body else "*")
        self.gzip_body = await request.read()
        if self.mode == "truncate":
            self.gzip_body = self.gzip_body[:-8]
        return web.Response(status=500 if self.mode == "lost-response" else 201)

    flush = UploadTests.flush
    recovery = UploadTests.recovery
    saved_pending = UploadTests.saved_pending

    async def test_migration_keeps_legacy_and_all_attributes(self):
        old = json.loads(self.legacy_body)
        old["location"] = {"lat": 51.29, "lon": 7.21}
        self.legacy_body = sync.encode_json(old)
        before = self.legacy_body
        await self.flush()
        uploaded = json.loads(gzip.decompress(self.gzip_body))
        self.assertEqual(uploaded["entries"], [snapshot(0), snapshot(1)])
        self.assertEqual(uploaded["location"], old["location"])
        self.assertEqual(uploaded["resource_path"], "history.json.gz")
        self.assertEqual(self.legacy_body, before)
        self.assertEqual(self.recovery(), uploaded)
        self.assertEqual(self.profile.last_resource_path, "history.json.gz")

    async def test_existing_gzip_is_authoritative(self):
        self.gzip_body = gzip.compress(sync.encode_json({"entries": [snapshot(0), snapshot(2)]}))
        await self.flush()
        self.assertEqual(json.loads(gzip.decompress(self.gzip_body))["entries"], [snapshot(0), snapshot(2), snapshot(1)])
        self.assertNotIn(("GET", "/history.json"), self.paths)

    async def test_browser_uploaded_gzip_media_types_can_be_appended_and_verified(self):
        for media_type in ("application/x-gzip", "application/octet-stream"):
            with self.subTest(media_type=media_type):
                self.gzip_content_type = media_type
                self.gzip_body = gzip.compress(sync.encode_json({"entries": [snapshot(0)]}))
                self.profile.pending_entries = [snapshot(1)]
                await self.flush()
                self.assertEqual(json.loads(gzip.decompress(self.gzip_body))["entries"], [snapshot(0), snapshot(1)])
                self.assertEqual(self.saved_pending(), [])
                self.assertEqual(self.puts[-1]["If-Match"], '"gzip"')
                self.assertNotIn(("GET", "/history.json"), self.paths)

    async def test_corrupt_upload_keeps_queue_and_recovery_and_never_uses_old_json(self):
        self.mode = "truncate"
        with self.assertRaisesRegex(RuntimeError, "not valid JSON"):
            await self.flush()
        self.assertEqual(self.saved_pending(), [snapshot(1)])
        self.assertEqual(self.recovery()["entries"], [snapshot(0), snapshot(1)])
        self.paths.clear()
        with self.assertRaises(RuntimeError):
            await self.flush()
        self.assertNotIn(("GET", "/history.json"), self.paths)
        self.assertEqual(len(self.puts), 1)

    async def test_retry_lost_response_does_not_duplicate_gzip_entries(self):
        self.mode = "lost-response"
        with self.assertRaises(RuntimeError):
            await self.flush()
        self.mode = "ok"
        await self.flush()
        self.assertEqual(json.loads(gzip.decompress(self.gzip_body))["entries"], [snapshot(0), snapshot(1)])

    async def test_expansion_limit_retains_queue(self):
        self.gzip_body = gzip.compress(sync.encode_json({"entries": [snapshot(0)]}))
        with patch.object(sync, "MAX_JSON_BYTES", 32):
            with self.assertRaisesRegex(RuntimeError, "limit"):
                await self.flush()
        self.assertEqual(self.puts, [])
        self.assertEqual(self.saved_pending(), [snapshot(1)])

    async def test_missing_previously_written_gzip_does_not_resurrect_old_history(self):
        self.profile.last_resource_path = "history.json.gz"
        with self.assertRaisesRegex(RuntimeError, "missing"):
            await self.flush()
        self.assertNotIn(("GET", "/history.json"), self.paths)
        self.assertEqual(self.puts, [])


if __name__ == "__main__":
    unittest.main()
