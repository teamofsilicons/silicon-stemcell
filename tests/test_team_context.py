from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from interface import config as glass

from interface.team import constants as t_constants
from interface.team import http as t_http
from interface.team import memory as t_memory
from interface.team import own_sync as t_own_sync
from interface.team import publish as t_publish
from interface.team import reads as t_reads
from interface.team import service as t_service


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class QueuedAPI:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if not self.responses:
            raise AssertionError(f"Unexpected Glass request: {method} {path}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def assert_drained(self):
        if self.responses:
            raise AssertionError(f"{len(self.responses)} queued response(s) were unused")


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def identity(silicon_id="self-si", team_slug="alpha"):
    return FakeResponse(
        body={
            "silicon_id": silicon_id,
            "owner_team_slug": team_slug,
            "is_active": True,
        }
    )


def memory_payload(silicon_id: str, content: str, revision: int):
    return {
        "silicon_id": silicon_id,
        "path": f"prompts/advertising/{silicon_id}.md",
        "revision": revision,
        "sha256": digest(content),
        "updated_at": None,
        "line_count": len(content.splitlines()),
        "byte_count": len(content.encode("utf-8")),
        "content": content,
    }


def manifest_entry(silicon_id: str, content: str, revision: int):
    return {
        key: value
        for key, value in memory_payload(silicon_id, content, revision).items()
        if key not in {"content", "line_count", "byte_count"}
    }


def context_payload(
    markdown: str,
    memories: list[dict],
    *,
    team_slug="alpha",
    sync_seed="sync-1",
):
    return {
        "team_id": f"team-{team_slug}",
        "team_slug": team_slug,
        "path": "prompts/TEAM.md",
        "revision": digest(markdown),
        "sync_revision": digest(sync_seed),
        "markdown": markdown,
        "advertising_memories": memories,
    }


class GlassTransportTests(unittest.TestCase):
    def test_config_is_scoped_to_the_exact_silicon_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            child = parent / "nested-silicon"
            child.mkdir()
            (parent / ".glass.json").write_text(
                json.dumps({
                    "server_url": "https://glass.example",
                    "api_key": "parent-key",
                }),
                encoding="utf-8",
            )

            self.assertIsNone(glass.find_config(child))
            with self.assertRaises(FileNotFoundError):
                glass.load_config(child)

    @unittest.skipIf(os.name == "nt", "POSIX permission mode")
    def test_loading_config_hardens_permissions_to_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".glass.json"
            config_path.write_text(
                json.dumps({
                    "server_url": "https://glass.example",
                    "api_key": "local-key",
                }),
                encoding="utf-8",
            )
            config_path.chmod(0o644)

            loaded, actual_path = glass.load_config(root)

            self.assertEqual(loaded["api_key"], "local-key")
            self.assertEqual(actual_path, config_path.resolve())
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

    def test_config_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "silicon"
            root.mkdir()
            outside = base / "outside.json"
            outside.write_text(
                json.dumps({
                    "server_url": "https://glass.example",
                    "api_key": "outside-key",
                }),
                encoding="utf-8",
            )
            (root / ".glass.json").symlink_to(outside)

            with self.assertRaises(glass.InterfaceConfigError):
                glass.load_config(root)

    def test_authenticated_request_uses_silicon_header_and_preserves_304(self):
        response = FakeResponse(304, headers={"ETag": '"context-v1"'})
        config = {
            "server_url": "https://glass.example",
            "api_key": "scs_live_secret",
        }
        with mock.patch.object(glass.requests, "request", return_value=response) as request:
            actual = glass.silicon_api_request(
                "GET",
                "/api/v1/teams/alpha/silicon-context",
                config=config,
                headers={
                    "If-None-Match": '"context-v0"',
                    "Authorization": "Bearer should-not-pass",
                    "authorization": "Bearer should-also-not-pass",
                    "x-silicon-key": "caller-key-should-not-pass",
                },
            )

        self.assertIs(actual, response)
        args, kwargs = request.call_args
        self.assertEqual(
            args,
            ("GET", "https://glass.example/api/v1/teams/alpha/silicon-context"),
        )
        self.assertEqual(kwargs["headers"]["X-Silicon-Key"], "scs_live_secret")
        self.assertEqual(kwargs["headers"]["If-None-Match"], '"context-v0"')
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertNotIn("authorization", kwargs["headers"])
        self.assertNotIn("x-silicon-key", kwargs["headers"])
        self.assertFalse(kwargs["allow_redirects"])

    def test_authenticated_request_rejects_non_loopback_http(self):
        with self.assertRaises(glass.InterfaceConfigError):
            glass.silicon_api_request(
                "GET",
                "/api/v1/silicons/me",
                config={
                    "server_url": "http://glass.example",
                    "api_key": "scs_live_secret",
                },
            )

    def test_legacy_key_alias_uses_the_same_header(self):
        response = FakeResponse(200, body={})
        with mock.patch.object(glass.requests, "request", return_value=response) as request:
            glass.silicon_api_request(
                "GET",
                "/api/v1/silicons/me",
                config={
                    "server_url": "http://127.0.0.1:8000",
                    "silicon_api_key": "legacy-key",
                },
            )
        self.assertEqual(
            request.call_args.kwargs["headers"]["X-Silicon-Key"],
            "legacy-key",
        )

    def test_provider_key_fetch_uses_safe_non_redirecting_transport(self):
        config = {
            "server_url": "https://glass.example",
            "api_key": "scs_live_secret",
        }
        response = FakeResponse(
            200,
            body={"keys": {"ANTHROPIC_API_KEY": "provider-secret"}},
        )
        with (
            mock.patch.object(
                glass,
                "silicon_api_request",
                return_value=response,
            ) as request,
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            loaded = glass.load_provider_keys_into_env(config)

        request.assert_called_once_with(
            "GET",
            "/api/v1/silicons/me/provider-keys",
            config=config,
            timeout=15,
        )
        self.assertEqual(
            loaded,
            {"ANTHROPIC_API_KEY": "provider-secret"},
        )

    def test_provider_key_fetch_rejects_redirect_response(self):
        with (
            mock.patch.object(
                glass,
                "silicon_api_request",
                return_value=FakeResponse(302),
            ),
            mock.patch("builtins.print"),
        ):
            loaded = glass.load_provider_keys_into_env(
                {
                    "server_url": "https://glass.example",
                    "api_key": "scs_live_secret",
                }
            )

        self.assertEqual(loaded, {})


class TeamContextSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "prompts").mkdir()
        (self.root / ".glass.json").write_text(
            json.dumps(
                {
                    "server_url": "https://glass.example",
                    "api_key": "scs_live_test",
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, api, *, force=True):
        with mock.patch.object(t_http, "silicon_api_request", side_effect=api):
            result = t_service.reconcile_team_context(
                self.root,
                force=force,
                reason="test",
            )
        api.assert_drained()
        return result

    def _initial_sync(self, peer_id="peer-si", peer_content="Peer secret"):
        markdown = "# Alpha Silicon Team\n\nAdvertising paths only.\n"
        own_content = "Owner status"
        memories = [
            manifest_entry("self-si", own_content, 1),
            manifest_entry(peer_id, peer_content, 3),
        ]
        api = QueuedAPI(
            identity(),
            FakeResponse(
                body=context_payload(markdown, memories),
                headers={"ETag": '"team-context-v1"'},
            ),
            FakeResponse(
                body=memory_payload(peer_id, peer_content, 3),
                headers={"ETag": '"peer-v3"'},
            ),
            FakeResponse(
                body=memory_payload("self-si", own_content, 1),
                headers={"ETag": '"self-v1"'},
            ),
        )
        result = self._run(api)
        self.assertTrue(result["ok"])
        return markdown, own_content, peer_content

    def test_prefetch_layout_exists_but_placeholder_is_not_verified(self):
        with mock.patch.object(
            t_http,
            "silicon_api_request",
            side_effect=OSError("Glass is offline"),
        ):
            result = t_service.reconcile_team_context(self.root, force=True)

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(
            (self.root / "prompts" / "TEAM.md").read_text(encoding="utf-8"),
            t_constants.TEAM_PLACEHOLDER_MARKDOWN,
        )
        self.assertTrue(
            (self.root / "prompts" / "advertising").is_dir()
        )
        self.assertEqual(t_reads.read_verified_team_markdown(self.root), "")

    def test_initial_sync_writes_team_and_memories_without_content_in_state(self):
        markdown, own_content, peer_content = self._initial_sync()

        self.assertEqual(
            (self.root / "prompts" / "TEAM.md").read_text(encoding="utf-8"),
            markdown,
        )
        self.assertEqual(
            (self.root / "prompts" / "advertising" / "self-si.md").read_text(
                encoding="utf-8"
            ),
            own_content,
        )
        self.assertEqual(
            (self.root / "prompts" / "advertising" / "peer-si.md").read_text(
                encoding="utf-8"
            ),
            peer_content,
        )
        raw_state = (
            self.root / "interface" / "state" / "team_context.json"
        ).read_text(encoding="utf-8")
        self.assertNotIn(own_content, raw_state)
        self.assertNotIn(peer_content, raw_state)
        self.assertNotIn("scs_live", raw_state)

    def test_team_reader_fails_closed_immediately_when_configured_origin_changes(self):
        markdown, _own_content, _peer_content = self._initial_sync()
        self.assertEqual(
            t_reads.read_verified_team_markdown(self.root),
            markdown,
        )
        (self.root / ".glass.json").write_text(
            json.dumps(
                {
                    "server_url": "https://different-glass.example",
                    "api_key": "scs_live_different",
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(t_reads.read_verified_team_markdown(self.root), "")

    def test_team_reader_fails_closed_immediately_when_silicon_key_changes(self):
        markdown, _own_content, _peer_content = self._initial_sync()
        self.assertEqual(
            t_reads.read_verified_team_markdown(self.root),
            markdown,
        )
        (self.root / ".glass.json").write_text(
            json.dumps(
                {
                    "server_url": "https://glass.example",
                    "api_key": "scs_live_another_silicon",
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(t_reads.read_verified_team_markdown(self.root), "")
        api = QueuedAPI(
            identity("different-self", "beta"),
            FakeResponse(status_code=503),
        )
        with mock.patch.object(t_http, "silicon_api_request", side_effect=api):
            result = t_service.team_context_tick(self.root)
        api.assert_drained()

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(
            (self.root / "prompts" / "TEAM.md").read_text(encoding="utf-8"),
            t_constants.TEAM_PLACEHOLDER_MARKDOWN,
        )

    def test_team_sync_never_inherits_parent_silicon_credentials(self):
        child = self.root / "nested-silicon"
        (child / "prompts").mkdir(parents=True)
        with mock.patch.object(t_http, "silicon_api_request") as request:
            result = t_service.reconcile_team_context(child, force=True)

        self.assertEqual(result["status"], "unavailable")
        request.assert_not_called()

    def test_lock_symlink_is_never_followed_or_modified(self):
        state_dir = self.root / "interface" / "state"
        state_dir.mkdir(parents=True)
        victim = self.root / "victim.txt"
        victim.write_text("must remain unchanged", encoding="utf-8")
        lock_path = state_dir / "team_context.lock"
        try:
            lock_path.symlink_to(victim)
        except OSError:
            self.skipTest("symlinks are unavailable")

        result = t_service.reconcile_team_context(self.root, force=True)

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(victim.read_text(), "must remain unchanged")

    def test_windows_invalid_silicon_id_is_rejected_before_file_access(self):
        api = QueuedAPI(identity("bad:id", "alpha"))

        result = self._run(api)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue((self.root / "prompts" / "advertising").is_dir())

    def test_context_304_does_not_refetch_unchanged_memories(self):
        self._initial_sync()
        api = QueuedAPI(
            identity(),
            FakeResponse(304, headers={"ETag": '"team-context-v1"'}),
        )
        result = self._run(api, force=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "current")
        context_call = api.calls[1]
        self.assertEqual(
            context_call[2]["headers"]["If-None-Match"],
            '"team-context-v1"',
        )

    def test_corrupt_peer_is_repaired_even_when_both_etags_return_304(self):
        self._initial_sync(peer_content="Canonical peer")
        peer_path = self.root / "prompts" / "advertising" / "peer-si.md"
        peer_path.write_text("locally corrupted", encoding="utf-8")
        api = QueuedAPI(
            identity(),
            FakeResponse(304, headers={"ETag": '"team-context-v1"'}),
            FakeResponse(304, headers={"ETag": '"peer-v3"'}),
            FakeResponse(
                body=memory_payload("peer-si", "Canonical peer", 3),
                headers={"ETag": '"peer-v3"'},
            ),
        )
        result = self._run(api, force=False)

        self.assertTrue(result["ok"])
        self.assertEqual(peer_path.read_text(encoding="utf-8"), "Canonical peer")
        peer_calls = [call for call in api.calls if "advertising-memories" in call[1]]
        self.assertEqual(len(peer_calls), 2)
        self.assertEqual(peer_calls[0][2]["headers"]["If-None-Match"], '"peer-v3"')
        self.assertNotIn("If-None-Match", peer_calls[1][2]["headers"])

    def test_manifest_change_fetches_delta_and_prunes_only_managed_peer(self):
        self._initial_sync(peer_id="old-peer", peer_content="Old")
        unknown = self.root / "prompts" / "advertising" / "notes.md"
        unknown.write_text("user-owned", encoding="utf-8")
        markdown = "# Alpha Silicon Team\n\nNew roster.\n"
        memories = [
            manifest_entry("self-si", "Owner status", 1),
            manifest_entry("new-peer", "New", 1),
        ]
        api = QueuedAPI(
            identity(),
            FakeResponse(
                body=context_payload(markdown, memories, sync_seed="sync-2"),
                headers={"ETag": '"team-context-v2"'},
            ),
            FakeResponse(
                body=memory_payload("new-peer", "New", 1),
                headers={"ETag": '"new-peer-v1"'},
            ),
        )
        result = self._run(api, force=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["peer_files_removed"], 1)
        self.assertFalse(
            (self.root / "prompts" / "advertising" / "old-peer.md").exists()
        )
        self.assertTrue(unknown.exists())
        self.assertEqual(
            (self.root / "prompts" / "advertising" / "new-peer.md").read_text(),
            "New",
        )

    def test_failed_peer_repair_removes_locally_tampered_mirror(self):
        markdown, _own, _peer = self._initial_sync(
            peer_id="peer",
            peer_content="Canonical peer",
        )
        peer_path = self.root / "prompts" / "advertising" / "peer.md"
        peer_path.write_text("TAMPERED LOCAL BYTES", encoding="utf-8")
        api = QueuedAPI(
            identity(),
            FakeResponse(304, headers={"ETag": '"team-context-v1"'}),
            OSError("offline"),
        )

        result = self._run(api, force=False)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["error_count"], 1)
        self.assertFalse(peer_path.exists())
        self.assertEqual(
            t_reads.read_verified_team_markdown(self.root),
            markdown,
        )
        state = json.loads(
            (
                self.root
                / "interface"
                / "state"
                / "team_context.json"
            ).read_text(encoding="utf-8")
        )
        self.assertNotIn("peer", state["peers"])
        self.assertIn("peer", state["managed_peer_ids"])

    def test_failed_peer_repair_keeps_verified_last_known_good_mirror(self):
        self._initial_sync(peer_id="peer", peer_content="Canonical peer")
        peer_path = self.root / "prompts" / "advertising" / "peer.md"
        api = QueuedAPI(
            identity(),
            FakeResponse(
                body=context_payload(
                    "# Alpha Silicon Team\n\nAdvertising paths only.\n",
                    [
                        manifest_entry("self-si", "Owner status", 1),
                        manifest_entry("peer", "New remote peer", 4),
                    ],
                    sync_seed="new-peer-revision",
                ),
                headers={"ETag": '"team-context-v2"'},
            ),
            OSError("offline"),
        )

        result = self._run(api, force=False)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(peer_path.read_text(encoding="utf-8"), "Canonical peer")

    def test_failed_peer_prune_retains_authority_and_retries(self):
        self._initial_sync(peer_id="old-peer", peer_content="Old")
        markdown = "# Alpha Silicon Team\n\nOwner only.\n"
        memories = [manifest_entry("self-si", "Owner status", 1)]
        api = QueuedAPI(
            identity(),
            FakeResponse(
                body=context_payload(markdown, memories, sync_seed="owner-only"),
                headers={"ETag": '"team-context-v2"'},
            ),
        )
        original_unlink = Path.unlink

        def fail_old_peer(path, *args, **kwargs):
            if path.name == "old-peer.md":
                raise PermissionError("temporarily locked")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(t_http, "silicon_api_request", side_effect=api):
            with mock.patch.object(Path, "unlink", new=fail_old_peer):
                result = t_service.reconcile_team_context(self.root, force=False)
        api.assert_drained()

        self.assertFalse(result["ok"])
        self.assertTrue(
            (self.root / "prompts" / "advertising" / "old-peer.md").exists()
        )
        state_path = self.root / "interface" / "state" / "team_context.json"
        state = json.loads(state_path.read_text())
        self.assertIn("old-peer", state["managed_peer_ids"])
        self.assertIn("old-peer", state["peers"])

        retry_api = QueuedAPI(
            identity(),
            FakeResponse(304, headers={"ETag": '"team-context-v2"'}),
        )
        retry_result = self._run(retry_api, force=False)
        self.assertTrue(retry_result["ok"])
        self.assertEqual(retry_result["peer_files_removed"], 1)
        self.assertFalse(
            (self.root / "prompts" / "advertising" / "old-peer.md").exists()
        )

    def test_remote_only_own_change_downloads_without_put(self):
        self._initial_sync()
        markdown = "# Alpha Silicon Team\n\nAdvertising paths only.\n"
        memories = [
            manifest_entry("self-si", "Remote owner update", 2),
            manifest_entry("peer-si", "Peer secret", 3),
        ]
        api = QueuedAPI(
            identity(),
            FakeResponse(
                body=context_payload(markdown, memories, sync_seed="sync-own-2"),
                headers={"ETag": '"team-context-v2"'},
            ),
            FakeResponse(
                body=memory_payload("self-si", "Remote owner update", 2),
                headers={"ETag": '"self-v2"'},
            ),
        )
        result = self._run(api, force=False)

        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["own_status"], "downloaded")
        self.assertEqual(
            (self.root / "prompts" / "advertising" / "self-si.md").read_text(),
            "Remote owner update",
        )
        self.assertFalse(any(call[0] == "PUT" for call in api.calls))

    def test_local_only_change_tick_uploads_with_expected_revision(self):
        self._initial_sync()
        own_path = self.root / "prompts" / "advertising" / "self-si.md"
        own_path.write_text("Local update", encoding="utf-8")
        api = QueuedAPI(
            identity(),
            FakeResponse(
                body={
                    **memory_payload("self-si", "Local update", 2),
                    "changed": True,
                },
                headers={"ETag": '"self-v2"'},
            )
        )
        with mock.patch.object(t_http, "silicon_api_request", side_effect=api):
            result = t_service.team_context_tick(self.root)
        api.assert_drained()

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "uploaded")
        method, path, kwargs = api.calls[1]
        self.assertEqual((method, path), ("PUT", "/api/v1/silicons/me/advertising-memory"))
        self.assertEqual(kwargs["json_body"]["expected_revision"], 1)
        self.assertEqual(kwargs["json_body"]["content"], "Local update")

    def test_cas_conflict_preserves_local_draft(self):
        self._initial_sync()
        own_path = self.root / "prompts" / "advertising" / "self-si.md"
        own_path.write_text("Unsynced local", encoding="utf-8")
        api = QueuedAPI(
            identity(),
            FakeResponse(409, body={"actual_revision": 2}),
            FakeResponse(
                body=memory_payload("self-si", "Concurrent remote", 2),
                headers={"ETag": '"self-v2"'},
            ),
        )
        with mock.patch.object(t_http, "silicon_api_request", side_effect=api):
            result = t_service.team_context_tick(self.root)
        api.assert_drained()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(result["local_saved"])
        self.assertEqual(result["actual_revision"], 2)
        self.assertEqual(own_path.read_text(encoding="utf-8"), "Unsynced local")

        no_retry_api = QueuedAPI()
        with mock.patch.object(
            t_http,
            "silicon_api_request",
            side_effect=no_retry_api,
        ):
            no_retry = t_service.team_context_tick(self.root)
        no_retry_api.assert_drained()
        self.assertEqual(no_retry["status"], "conflict")
        self.assertEqual(no_retry["actual_revision"], 2)

    def test_put_rechecks_identity_and_never_uploads_under_switched_credentials(self):
        self._initial_sync()
        own_path = self.root / "prompts" / "advertising" / "self-si.md"
        own_path.write_text("Must not cross identities", encoding="utf-8")
        api = QueuedAPI(identity("different-self", "beta"))
        with mock.patch.object(t_http, "silicon_api_request", side_effect=api):
            result = t_service.team_context_tick(self.root)
        api.assert_drained()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(any(call[0] == "PUT" for call in api.calls))
        state = json.loads(
            (
                self.root / "interface" / "state" / "team_context.json"
            ).read_text()
        )
        self.assertEqual(state["identity"]["silicon_id"], "different-self")
        self.assertFalse(own_path.exists())
        archive_root = self.root / "interface" / "state" / "team_context_drafts"
        self.assertTrue(
            any(
                path.is_file()
                and path.read_text(encoding="utf-8") == "Must not cross identities"
                for path in archive_root.rglob("*.md")
            )
        )

    def test_own_symlink_is_never_read_or_uploaded(self):
        self._initial_sync()
        secret = self.root / ".env"
        secret.write_text("TOP_SECRET=value", encoding="utf-8")
        own_path = self.root / "prompts" / "advertising" / "self-si.md"
        own_path.unlink()
        own_path.symlink_to(secret)
        api = QueuedAPI()
        with mock.patch.object(t_http, "silicon_api_request", side_effect=api):
            result = t_service.team_context_tick(self.root)
        api.assert_drained()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid")
        self.assertIn("regular file", result["detail"])
        self.assertFalse(any(call[0] == "PUT" for call in api.calls))
        self.assertEqual(secret.read_text(), "TOP_SECRET=value")

    def test_full_reconcile_preserves_own_invalid_detail(self):
        markdown = "# Alpha Silicon Team\n"
        own_content = "Owner status"
        api = QueuedAPI(
            identity(),
            FakeResponse(
                body=context_payload(
                    markdown,
                    [manifest_entry("self-si", own_content, 1)],
                ),
                headers={"ETag": '"team-context-v1"'},
            ),
        )
        detail = "Local context file changed while it was being read."
        own_result = {
            "ok": False,
            "status": "invalid",
            "changed": False,
            "local_saved": True,
            "detail": detail,
        }

        with mock.patch.object(
            t_own_sync,
            "_sync_own",
            return_value=own_result,
        ):
            result = self._run(api)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["own_status"], "invalid")
        self.assertEqual(result["own_detail"], detail)

    def test_symlinked_advertising_directory_is_never_read_or_uploaded(self):
        self._initial_sync()
        advertising = self.root / "prompts" / "advertising"
        advertising.rename(self.root / "old-advertising")
        with tempfile.TemporaryDirectory() as outside:
            outside_dir = Path(outside)
            secret = outside_dir / "self-si.md"
            secret.write_text("TOP_SECRET=value", encoding="utf-8")
            try:
                advertising.symlink_to(outside_dir, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks are unavailable")

            api = QueuedAPI()
            with mock.patch.object(
                t_http,
                "silicon_api_request",
                side_effect=api,
            ):
                result = t_service.team_context_tick(self.root)
            api.assert_drained()

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "invalid")
            self.assertIn("local directory", result["detail"])
            self.assertFalse(any(call[0] == "PUT" for call in api.calls))
            self.assertEqual(secret.read_text(), "TOP_SECRET=value")

    def test_explicit_update_uses_fixed_own_path_and_cas(self):
        self._initial_sync()
        api = QueuedAPI(
            identity(),
            identity(),
            FakeResponse(
                body={
                    **memory_payload("self-si", "Manager-authored", 2),
                    "changed": True,
                },
                headers={"ETag": '"self-v2"'},
            ),
        )
        with mock.patch.object(t_http, "silicon_api_request", side_effect=api):
            result = t_publish.update_own_advertising_memory(
                "Manager-authored",
                self.root,
            )
        api.assert_drained()

        self.assertTrue(result["ok"])
        self.assertEqual(
            (self.root / "prompts" / "advertising" / "self-si.md").read_text(),
            "Manager-authored",
        )
        self.assertEqual(api.calls[2][0], "PUT")
        self.assertEqual(api.calls[2][2]["json_body"]["expected_revision"], 1)

    def test_direct_update_rejects_non_boolean_conflict_resolution(self):
        result = t_publish.update_own_advertising_memory(
            "Valid content",
            self.root,
            resolve_conflict="false",
        )

        self.assertEqual(result["status"], "invalid")
        self.assertIn("must be a boolean", result["detail"])

    def test_explicit_update_preserves_draft_when_glass_is_unreachable(self):
        self._initial_sync()
        api = QueuedAPI(OSError("offline"))
        with mock.patch.object(t_http, "silicon_api_request", side_effect=api):
            result = t_publish.update_own_advertising_memory(
                "Offline draft",
                self.root,
            )
        api.assert_drained()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "pending")
        self.assertTrue(result["local_saved"])
        self.assertEqual(
            (self.root / "prompts" / "advertising" / "self-si.md").read_text(),
            "Offline draft",
        )

    def test_explicit_update_privately_preserves_draft_when_identity_is_rejected(self):
        self._initial_sync()
        own_path = self.root / "prompts" / "advertising" / "self-si.md"
        rejected_draft = "Keep this edit even though Glass revoked the key"
        api = QueuedAPI(FakeResponse(status_code=401))
        with mock.patch.object(t_http, "silicon_api_request", side_effect=api):
            result = t_publish.update_own_advertising_memory(
                rejected_draft,
                self.root,
            )
        api.assert_drained()

        self.assertEqual(result["status"], "unauthorized")
        self.assertTrue(result["local_saved"])
        self.assertEqual(own_path.read_text(), "Owner status")
        archive_root = self.root / "interface" / "state" / "team_context_drafts"
        self.assertTrue(
            any(
                path.is_file()
                and path.read_text(encoding="utf-8") == rejected_draft
                for path in archive_root.rglob("*.md")
            )
        )
        raw_state = (
            self.root / "interface" / "state" / "team_context.json"
        ).read_text(encoding="utf-8")
        self.assertNotIn(rejected_draft, raw_state)

    def test_explicit_update_uses_stale_base_as_cas_and_preserves_conflict(self):
        self._initial_sync()
        api = QueuedAPI(
            identity(),
            identity(),
            FakeResponse(409, body={"actual_revision": 2}),
            FakeResponse(
                body=memory_payload("self-si", "Concurrent remote", 2),
                headers={"ETag": '"self-v2"'},
            ),
        )
        with mock.patch.object(t_http, "silicon_api_request", side_effect=api):
            result = t_publish.update_own_advertising_memory(
                "Intentional local choice",
                self.root,
            )
        api.assert_drained()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(api.calls[2][2]["json_body"]["expected_revision"], 1)
        self.assertEqual(
            (self.root / "prompts" / "advertising" / "self-si.md").read_text(),
            "Intentional local choice",
        )

    def test_conflict_requires_explicit_resolution_against_latest_revision(self):
        self._initial_sync()
        own_path = self.root / "prompts" / "advertising" / "self-si.md"
        own_path.write_text("Chosen local", encoding="utf-8")
        conflict_api = QueuedAPI(
            identity(),
            FakeResponse(409, body={"actual_revision": 2}),
            FakeResponse(
                body=memory_payload("self-si", "Concurrent remote", 2),
                headers={"ETag": '"self-v2"'},
            ),
        )
        with mock.patch.object(
            t_http,
            "silicon_api_request",
            side_effect=conflict_api,
        ):
            first = t_service.team_context_tick(self.root)
        conflict_api.assert_drained()
        self.assertEqual(first["status"], "conflict")

        ordinary_api = QueuedAPI(identity())
        with mock.patch.object(
            t_http,
            "silicon_api_request",
            side_effect=ordinary_api,
        ):
            ordinary = t_publish.update_own_advertising_memory(
                "Chosen local",
                self.root,
            )
        ordinary_api.assert_drained()
        self.assertEqual(ordinary["status"], "conflict")
        self.assertFalse(any(call[0] == "PUT" for call in ordinary_api.calls))

        resolution_api = QueuedAPI(
            identity(),
            FakeResponse(
                body=memory_payload("self-si", "Concurrent remote", 2),
                headers={"ETag": '"self-v2"'},
            ),
            identity(),
            FakeResponse(
                body={
                    **memory_payload("self-si", "Chosen local", 3),
                    "changed": True,
                },
                headers={"ETag": '"self-v3"'},
            ),
        )
        with mock.patch.object(
            t_http,
            "silicon_api_request",
            side_effect=resolution_api,
        ):
            resolved = t_publish.update_own_advertising_memory(
                "Chosen local",
                self.root,
                resolve_conflict=True,
            )
        resolution_api.assert_drained()

        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["status"], "uploaded")
        put_call = next(call for call in resolution_api.calls if call[0] == "PUT")
        self.assertEqual(put_call[2]["json_body"]["expected_revision"], 2)
        self.assertEqual(own_path.read_text(), "Chosen local")

    def test_bad_context_is_fail_open_and_keeps_last_known_good_team_file(self):
        markdown, _own, _peer = self._initial_sync()
        bad_payload = context_payload(
            "tampered",
            [
                manifest_entry("self-si", "Owner status", 1),
                manifest_entry("peer-si", "Peer secret", 3),
            ],
            sync_seed="bad",
        )
        bad_payload["revision"] = "0" * 64
        api = QueuedAPI(identity(), FakeResponse(body=bad_payload))
        result = self._run(api, force=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(
            (self.root / "prompts" / "TEAM.md").read_text(encoding="utf-8"),
            markdown,
        )

    def test_oversized_context_keeps_last_known_good_team_file(self):
        markdown, _own, _peer = self._initial_sync()
        oversized = "x" * (t_constants.MAX_TEAM_CONTEXT_BYTES + 1)
        api = QueuedAPI(
            identity(),
            FakeResponse(
                body=context_payload(
                    oversized,
                    [
                        manifest_entry("self-si", "Owner status", 1),
                        manifest_entry("peer-si", "Peer secret", 3),
                    ],
                    sync_seed="oversized",
                )
            ),
        )

        result = self._run(api, force=False)

        self.assertFalse(result["ok"])
        self.assertEqual(
            (self.root / "prompts" / "TEAM.md").read_text(encoding="utf-8"),
            markdown,
        )
        self.assertEqual(
            t_reads.read_verified_team_markdown(self.root),
            markdown,
        )

    def test_identity_switch_does_not_send_old_etag_or_leave_old_own_file(self):
        self._initial_sync()
        markdown = "# Beta Silicon Team\n"
        memories = [manifest_entry("new-self", "", 0)]
        api = QueuedAPI(
            identity("new-self", "beta"),
            FakeResponse(
                body=context_payload(
                    markdown,
                    memories,
                    team_slug="beta",
                    sync_seed="beta",
                ),
                headers={"ETag": '"beta-v1"'},
            ),
            FakeResponse(
                body=memory_payload("new-self", "", 0),
                headers={"ETag": '"new-self-v0"'},
            ),
        )
        result = self._run(api, force=False)

        self.assertTrue(result["ok"])
        self.assertNotIn("If-None-Match", api.calls[1][2]["headers"])
        self.assertFalse(
            (self.root / "prompts" / "advertising" / "self-si.md").exists()
        )
        self.assertFalse(
            (self.root / "prompts" / "advertising" / "peer-si.md").exists()
        )

    def test_identity_switch_to_former_peer_keeps_the_new_own_file(self):
        self._initial_sync(peer_id="next-self", peer_content="Peer becomes owner")
        markdown = "# Beta Silicon Team\n"
        memories = [manifest_entry("next-self", "Peer becomes owner", 3)]
        api = QueuedAPI(
            identity("next-self", "beta"),
            FakeResponse(
                body=context_payload(
                    markdown,
                    memories,
                    team_slug="beta",
                    sync_seed="beta-next-self",
                ),
                headers={"ETag": '"beta-v1"'},
            ),
        )
        result = self._run(api, force=False)

        self.assertTrue(result["ok"])
        self.assertEqual(
            (self.root / "prompts" / "advertising" / "next-self.md").read_text(),
            "Peer becomes owner",
        )

    def test_advertising_validation_is_exact_and_never_writes_invalid_content(self):
        exactly_100 = "\n".join(f"line {index}" for index in range(100))
        self.assertEqual(
            t_memory.validate_advertising_memory(exactly_100),
            exactly_100,
        )
        invalid = "\n".join(f"line {index}" for index in range(101))
        with mock.patch.object(t_http, "silicon_api_request") as request:
            result = t_publish.update_own_advertising_memory(invalid, self.root)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid")
        request.assert_not_called()
        self.assertFalse((self.root / "prompts" / "advertising").exists())

    def test_state_file_is_valid_json_and_atomic_temps_are_cleaned(self):
        self._initial_sync()
        state_dir = self.root / "interface" / "state"
        state = json.loads((state_dir / "team_context.json").read_text())

        self.assertEqual(state["version"], 1)
        self.assertEqual(
            [path.name for path in state_dir.iterdir() if path.name.startswith(".")],
            [],
        )

    def test_malformed_schedule_is_repaired_and_sync_continues(self):
        self._initial_sync()
        state_path = self.root / "interface" / "state" / "team_context.json"
        state = json.loads(state_path.read_text())
        state["schedule"]["next_reconcile_at"] = "broken"
        state["schedule"]["failure_count"] = "also-broken"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        api = QueuedAPI(
            identity(),
            FakeResponse(304, headers={"ETag": '"team-context-v1"'}),
        )

        with mock.patch.object(
            t_http,
            "silicon_api_request",
            side_effect=api,
        ):
            result = t_service.team_context_tick(self.root)
        api.assert_drained()

        self.assertTrue(result["ok"])
        repaired = json.loads(state_path.read_text())
        self.assertIsInstance(
            repaired["schedule"]["next_reconcile_at"],
            (int, float),
        )
        self.assertEqual(repaired["schedule"]["failure_count"], 0)

    def test_verified_team_reader_requires_the_validated_hash(self):
        markdown, _own, _peer = self._initial_sync()
        team_path = self.root / "prompts" / "TEAM.md"

        self.assertEqual(
            t_reads.read_verified_team_markdown(self.root),
            markdown,
        )
        team_path.write_text("tampered", encoding="utf-8")
        self.assertEqual(t_reads.read_verified_team_markdown(self.root), "")

    def test_verified_team_reader_rejects_symlink_and_size_limit(self):
        markdown, _own, _peer = self._initial_sync()
        team_path = self.root / "prompts" / "TEAM.md"
        real_path = self.root / "real-team.md"
        team_path.replace(real_path)
        team_path.symlink_to(real_path)

        self.assertEqual(t_reads.read_verified_team_markdown(self.root), "")
        team_path.unlink()
        real_path.replace(team_path)
        self.assertEqual(
            t_reads.read_verified_team_markdown(
                self.root,
                max_bytes=len(markdown.encode("utf-8")) - 1,
            ),
            "",
        )

    def test_explicit_update_without_verified_identity_archives_draft(self):
        (self.root / ".glass.json").unlink()
        content = "Valuable explicit draft before provisioning"

        result = t_publish.update_own_advertising_memory(
            content,
            self.root,
        )

        self.assertEqual(result["status"], "identity_unavailable")
        self.assertTrue(result["local_saved"])
        archive = (
            self.root
            / "interface"
            / "state"
            / "team_context_drafts"
            / "unverified"
            / f"{digest(content)}.md"
        )
        self.assertEqual(archive.read_text(encoding="utf-8"), content)
        state = json.loads(
            (
                self.root
                / "interface"
                / "state"
                / "team_context.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            state["draft_archives"][-1]["reason"],
            "identity_unverified",
        )


if __name__ == "__main__":
    unittest.main()
