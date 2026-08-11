from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from interface import team_context


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
            raise AssertionError(
                f"{len(self.responses)} queued response(s) were unused"
            )


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


class TeamContextTransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "prompts").mkdir()
        self._write_glass_config("https://glass-a.example", "scs_live_a")

    def tearDown(self):
        self.temp.cleanup()

    def _write_glass_config(self, server_url: str, api_key: str):
        (self.root / ".glass.json").write_text(
            json.dumps({"server_url": server_url, "api_key": api_key}),
            encoding="utf-8",
        )

    def _run(self, api, *, force=True):
        with mock.patch.object(
            team_context,
            "silicon_api_request",
            side_effect=api,
        ):
            result = team_context.reconcile_team_context(
                self.root,
                force=force,
                reason="transition-test",
            )
        api.assert_drained()
        return result

    def _initial_sync(
        self,
        *,
        own_content="Owner canonical",
        peer_id="peer-si",
        peer_content="Peer canonical",
    ):
        markdown = "# Alpha Silicon Team\n\nCurrent roster.\n"
        memories = [
            manifest_entry("self-si", own_content, 1),
            manifest_entry(peer_id, peer_content, 3),
        ]
        api = QueuedAPI(
            identity(),
            FakeResponse(
                body=context_payload(markdown, memories),
                headers={"ETag": '"team-context-a"'},
            ),
            FakeResponse(
                body=memory_payload(peer_id, peer_content, 3),
                headers={"ETag": '"peer-a-v3"'},
            ),
            FakeResponse(
                body=memory_payload("self-si", own_content, 1),
                headers={"ETag": '"self-a-v1"'},
            ),
        )
        result = self._run(api)
        self.assertTrue(result["ok"])
        return markdown, own_content, peer_content

    def _assert_authorization_loss_invalidates_team(self, *responses):
        self._initial_sync()
        own_path = self.root / "prompts" / "advertising" / "self-si.md"
        own_path.write_text("Unsynced own draft", encoding="utf-8")

        result = self._run(QueuedAPI(*responses))

        self.assertFalse(result["ok"])
        self.assertEqual(team_context.read_verified_team_markdown(self.root), "")
        self.assertEqual(
            (self.root / "prompts" / "TEAM.md").read_text(encoding="utf-8"),
            team_context.TEAM_PLACEHOLDER_MARKDOWN,
        )
        self.assertFalse(
            (self.root / "prompts" / "advertising" / "peer-si.md").exists()
        )
        self.assertEqual(own_path.read_text(encoding="utf-8"), "Unsynced own draft")

        state = json.loads(
            (self.root / "interface" / "state" / "team_context.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["context"], {})
        self.assertEqual(state["peers"], {})
        self.assertEqual(state["managed_peer_ids"], [])

    def test_401_authorization_loss_invalidates_team_but_preserves_own_draft(self):
        self._assert_authorization_loss_invalidates_team(FakeResponse(status_code=401))

    def test_403_authorization_loss_invalidates_team_but_preserves_own_draft(self):
        self._assert_authorization_loss_invalidates_team(
            identity(),
            FakeResponse(status_code=403),
        )

    def test_authorization_failure_respects_retry_schedule(self):
        self._initial_sync()
        denied = self._run(QueuedAPI(FakeResponse(status_code=401)))
        self.assertEqual(denied["status"], "unauthorized")

        with mock.patch.object(team_context, "silicon_api_request") as request:
            deferred = team_context.team_context_tick(self.root)

        self.assertEqual(deferred["status"], "deferred")
        request.assert_not_called()

    def test_verified_team_switch_clears_old_context_before_new_context_fetch(self):
        self._initial_sync()
        own_path = self.root / "prompts" / "advertising" / "self-si.md"
        own_path.write_text("Own draft survives team move", encoding="utf-8")

        api = QueuedAPI(
            identity("self-si", "beta"),
            FakeResponse(status_code=503),
        )
        result = self._run(api, force=False)

        self.assertFalse(result["ok"])
        self.assertEqual(team_context.read_verified_team_markdown(self.root), "")
        self.assertEqual(
            (self.root / "prompts" / "TEAM.md").read_text(encoding="utf-8"),
            team_context.TEAM_PLACEHOLDER_MARKDOWN,
        )
        self.assertFalse(
            (self.root / "prompts" / "advertising" / "peer-si.md").exists()
        )
        self.assertEqual(
            own_path.read_text(encoding="utf-8"),
            "Own draft survives team move",
        )

        state = json.loads(
            (self.root / "interface" / "state" / "team_context.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["identity"]["silicon_id"], "self-si")
        self.assertEqual(state["identity"]["team_slug"], "beta")
        self.assertEqual(state["context"], {})
        self.assertEqual(state["peers"], {})
        self.assertEqual(state["managed_peer_ids"], [])

    def test_silicon_id_switch_quarantines_old_draft_even_if_context_fetch_fails(self):
        self._initial_sync(
            peer_id="next-self",
            peer_content="Next owner canonical",
        )
        former_own_path = self.root / "prompts" / "advertising" / "self-si.md"
        draft = "Unpublished work owned by the former Silicon ID"
        former_own_path.write_text(draft, encoding="utf-8")

        result = self._run(
            QueuedAPI(
                identity("next-self", "beta"),
                FakeResponse(status_code=503),
            ),
            force=False,
        )

        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(former_own_path.exists())
        archive_root = self.root / "interface" / "state" / "team_context_drafts"
        self.assertTrue(
            any(
                path.is_file() and path.read_text(encoding="utf-8") == draft
                for path in archive_root.rglob("*.md")
            )
        )

    def test_identity_switch_archives_former_own_draft_before_peer_sync(self):
        self._initial_sync(
            peer_id="next-self",
            peer_content="Next owner canonical",
        )
        former_own_path = self.root / "prompts" / "advertising" / "self-si.md"
        draft = "Former owner has unsynced, irreplaceable work"
        former_own_path.write_text(draft, encoding="utf-8")

        new_markdown = "# Beta Silicon Team\n\nRotated identity.\n"
        former_own_remote = "Former owner canonical peer memory"
        memories = [
            manifest_entry("next-self", "Next owner canonical", 3),
            manifest_entry("self-si", former_own_remote, 2),
        ]
        api = QueuedAPI(
            identity("next-self", "beta"),
            FakeResponse(
                body=context_payload(
                    new_markdown,
                    memories,
                    team_slug="beta",
                    sync_seed="beta-identity-switch",
                ),
                headers={"ETag": '"team-context-beta"'},
            ),
            FakeResponse(
                body=memory_payload("self-si", former_own_remote, 2),
                headers={"ETag": '"former-own-as-peer-v2"'},
            ),
        )
        result = self._run(api, force=False)

        self.assertTrue(result["ok"])
        self.assertEqual(
            former_own_path.read_text(encoding="utf-8"),
            former_own_remote,
        )

        archive_root = self.root / "interface" / "state" / "team_context_drafts"
        archived_files = [path for path in archive_root.rglob("*") if path.is_file()]
        self.assertTrue(
            any(path.read_text(encoding="utf-8") == draft for path in archived_files),
            "The former principal's unsynced draft must have a private archive copy.",
        )
        raw_state = (
            self.root / "interface" / "state" / "team_context.json"
        ).read_text(encoding="utf-8")
        self.assertNotIn(draft, raw_state)

    def test_identity_switch_archives_invalid_former_own_before_peer_sync(self):
        self._initial_sync(
            peer_id="next-self",
            peer_content="Next owner canonical",
        )
        former_own_path = self.root / "prompts" / "advertising" / "self-si.md"
        invalid_draft = "\n".join(f"private line {index}" for index in range(101))
        former_own_path.write_text(invalid_draft, encoding="utf-8")

        new_markdown = "# Beta Silicon Team\n\nRotated identity.\n"
        former_own_remote = "Former owner canonical peer memory"
        memories = [
            manifest_entry("next-self", "Next owner canonical", 3),
            manifest_entry("self-si", former_own_remote, 2),
        ]
        api = QueuedAPI(
            identity("next-self", "beta"),
            FakeResponse(
                body=context_payload(
                    new_markdown,
                    memories,
                    team_slug="beta",
                    sync_seed="beta-invalid-draft",
                ),
                headers={"ETag": '"team-context-beta"'},
            ),
            FakeResponse(
                body=memory_payload("self-si", former_own_remote, 2),
                headers={"ETag": '"former-own-as-peer-v2"'},
            ),
        )
        result = self._run(api, force=False)

        self.assertTrue(result["ok"])
        self.assertEqual(former_own_path.read_text(), former_own_remote)
        archive_root = self.root / "interface" / "state" / "team_context_drafts"
        archived_files = [path for path in archive_root.rglob("*.md") if path.is_file()]
        self.assertTrue(
            any(path.read_text(encoding="utf-8") == invalid_draft for path in archived_files)
        )
        raw_state = (
            self.root / "interface" / "state" / "team_context.json"
        ).read_text(encoding="utf-8")
        self.assertNotIn(invalid_draft, raw_state)
        state = json.loads(raw_state)
        self.assertTrue(
            any(
                archive.get("validation_status") == "invalid"
                for archive in state["draft_archives"]
            )
        )

    def test_identity_switch_rejects_symlinked_invalid_draft_archive(self):
        self._initial_sync(
            peer_id="next-self",
            peer_content="Next owner canonical",
        )
        former_own_path = self.root / "prompts" / "advertising" / "self-si.md"
        invalid_draft = "\n".join(f"private line {index}" for index in range(101))
        former_own_path.write_text(invalid_draft, encoding="utf-8")
        archive_link = (
            self.root
            / "interface"
            / "state"
            / "team_context_drafts"
            / "self-si"
        )
        archive_link.parent.mkdir(parents=True)

        with tempfile.TemporaryDirectory() as outside_raw:
            outside = Path(outside_raw)
            try:
                archive_link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks are unavailable")

            result = self._run(QueuedAPI(identity("next-self", "beta")), force=False)

            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(
                former_own_path.read_text(encoding="utf-8"),
                invalid_draft,
            )
            self.assertEqual(list(outside.iterdir()), [])

    def test_origin_switch_never_auto_publishes_old_content_to_empty_authority(self):
        markdown, _own, _peer = self._initial_sync()
        own_path = self.root / "prompts" / "advertising" / "self-si.md"
        old_authority_draft = "Only Glass A was allowed to receive this"
        own_path.write_text(old_authority_draft, encoding="utf-8")
        self._write_glass_config("https://glass-b.example", "scs_live_b")

        api = QueuedAPI(
            identity(),
            FakeResponse(
                body=context_payload(
                    markdown,
                    [manifest_entry("self-si", "", 0)],
                    sync_seed="glass-b-empty",
                ),
                headers={"ETag": '"team-context-b"'},
            ),
        )
        result = self._run(api, force=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["own_status"], "conflict")
        self.assertEqual(own_path.read_text(), old_authority_draft)
        self.assertFalse(any(call[0] == "PUT" for call in api.calls))

    def test_missing_state_never_auto_publishes_preexisting_own_file(self):
        advertising = self.root / "prompts" / "advertising"
        advertising.mkdir()
        own_path = advertising / "self-si.md"
        unscoped_content = "Authority is unknown because local state was lost"
        own_path.write_text(unscoped_content, encoding="utf-8")
        markdown = "# Alpha Silicon Team\n"
        api = QueuedAPI(
            identity(),
            FakeResponse(
                body=context_payload(
                    markdown,
                    [manifest_entry("self-si", "", 0)],
                    sync_seed="missing-state-own",
                ),
                headers={"ETag": '"team-context-a"'},
            ),
        )

        result = self._run(api)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["own_status"], "conflict")
        self.assertEqual(own_path.read_text(), unscoped_content)
        self.assertFalse(any(call[0] == "PUT" for call in api.calls))

    def test_missing_state_quarantines_stale_advertising_files(self):
        advertising = self.root / "prompts" / "advertising"
        advertising.mkdir()
        stale_path = advertising / "old-peer.md"
        stale_content = "Old team peer content with no surviving provenance"
        stale_path.write_text(stale_content, encoding="utf-8")
        markdown = "# Alpha Silicon Team\n"
        api = QueuedAPI(
            identity(),
            FakeResponse(
                body=context_payload(
                    markdown,
                    [manifest_entry("self-si", "", 0)],
                    sync_seed="missing-state-peer",
                ),
                headers={"ETag": '"team-context-a"'},
            ),
            FakeResponse(
                body=memory_payload("self-si", "", 0),
                headers={"ETag": '"self-v0"'},
            ),
        )

        result = self._run(api)

        self.assertTrue(result["ok"])
        self.assertFalse(stale_path.exists())
        archive_root = self.root / "interface" / "state" / "team_context_drafts"
        self.assertTrue(
            any(
                path.is_file()
                and path.read_text(encoding="utf-8") == stale_content
                for path in archive_root.rglob("*.quarantine")
            )
        )

    def test_missing_state_rejects_symlinked_unscoped_archive(self):
        advertising = self.root / "prompts" / "advertising"
        advertising.mkdir()
        stale_path = advertising / "old-peer.md"
        stale_content = "Unscoped content must not leave the Silicon root"
        stale_path.write_text(stale_content, encoding="utf-8")
        archive_link = (
            self.root
            / "interface"
            / "state"
            / "team_context_drafts"
            / "unscoped"
        )
        archive_link.parent.mkdir(parents=True)

        with tempfile.TemporaryDirectory() as outside_raw:
            outside = Path(outside_raw)
            try:
                archive_link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks are unavailable")

            result = self._run(QueuedAPI(identity()))

            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(
                stale_path.read_text(encoding="utf-8"),
                stale_content,
            )
            self.assertEqual(list(outside.iterdir()), [])

    def test_server_origin_switch_forces_refetch_and_discards_old_cas_base(self):
        markdown, _own, peer_content = self._initial_sync()
        own_path = self.root / "prompts" / "advertising" / "self-si.md"
        local_draft = "Local edit made against the old Glass authority"
        own_path.write_text(local_draft, encoding="utf-8")
        self._write_glass_config("https://glass-b.example", "scs_live_b")

        b_own_content = "Glass B canonical owner memory"
        b_own_revision = 7
        b_memories = [
            manifest_entry("self-si", b_own_content, b_own_revision),
            manifest_entry("peer-si", peer_content, 3),
        ]
        calls = []

        def glass_b_api(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET" and path == "/api/v1/silicons/me":
                return identity()
            if method == "GET" and path.endswith("/silicon-context"):
                return FakeResponse(
                    body=context_payload(
                        markdown,
                        b_memories,
                        # Deliberately preserve TEAM.md's revision while the
                        # configured Glass authority changes.
                        sync_seed="glass-b",
                    ),
                    headers={"ETag": '"team-context-b"'},
                )
            if method == "GET" and path == "/api/v1/silicons/me/advertising-memory":
                return FakeResponse(
                    body=memory_payload(
                        "self-si",
                        b_own_content,
                        b_own_revision,
                    ),
                    headers={"ETag": '"self-b-v7"'},
                )
            if method == "GET" and path.endswith("/advertising-memories/peer-si"):
                return FakeResponse(
                    body=memory_payload("peer-si", peer_content, 3),
                    headers={"ETag": '"peer-b-v3"'},
                )
            if method == "PUT" and path == "/api/v1/silicons/me/advertising-memory":
                return FakeResponse(
                    body={
                        **memory_payload(
                            "self-si",
                            local_draft,
                            b_own_revision + 1,
                        ),
                        "changed": True,
                    },
                    headers={"ETag": '"self-b-v8"'},
                )
            raise AssertionError(f"Unexpected Glass B request: {method} {path}")

        with mock.patch.object(
            team_context,
            "silicon_api_request",
            side_effect=glass_b_api,
        ):
            team_context.team_context_tick(self.root)

        context_calls = [call for call in calls if call[1].endswith("/silicon-context")]
        self.assertEqual(
            len(context_calls),
            1,
            "Changing Glass origin must force a context fetch even before the fallback is due.",
        )
        self.assertNotIn(
            "If-None-Match",
            context_calls[0][2].get("headers") or {},
            "An ETag learned from another Glass origin must never be reused.",
        )

        put_indexes = [index for index, call in enumerate(calls) if call[0] == "PUT"]
        context_index = next(
            index
            for index, call in enumerate(calls)
            if call[1].endswith("/silicon-context")
        )
        for put_index in put_indexes:
            put_call = calls[put_index]
            self.assertLess(
                context_index,
                put_index,
                "A PUT must follow a context refetch from the new Glass origin.",
            )
            self.assertEqual(
                put_call[2]["json_body"]["expected_revision"],
                b_own_revision,
            )

        self.assertEqual(own_path.read_text(encoding="utf-8"), local_draft)

    def test_state_save_failure_restores_previous_team_and_verified_reader(self):
        markdown, own_content, peer_content = self._initial_sync()
        replacement = "# Alpha Silicon Team\n\nUncommitted replacement.\n"
        memories = [
            manifest_entry("self-si", own_content, 1),
            manifest_entry("peer-si", peer_content, 3),
        ]
        api = QueuedAPI(
            identity(),
            FakeResponse(
                body=context_payload(
                    replacement,
                    memories,
                    sync_seed="replacement",
                ),
                headers={"ETag": '"team-context-replacement"'},
            ),
        )

        with (
            mock.patch.object(
                team_context,
                "silicon_api_request",
                side_effect=api,
            ),
            mock.patch.object(
                team_context,
                "_save_state",
                side_effect=OSError("state volume is read-only"),
            ),
        ):
            result = team_context.reconcile_team_context(
                self.root,
                force=False,
                reason="state-save-failure",
            )
        api.assert_drained()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "state_error")
        self.assertEqual(
            (self.root / "prompts" / "TEAM.md").read_text(encoding="utf-8"),
            markdown,
        )
        self.assertEqual(
            team_context.read_verified_team_markdown(self.root),
            markdown,
        )

    def test_state_save_failure_after_peer_prune_blocks_old_team_visibility(self):
        _markdown, own_content, _peer_content = self._initial_sync()
        replacement = "# Alpha Silicon Team\n\nOwner only.\n"
        api = QueuedAPI(
            identity(),
            FakeResponse(
                body=context_payload(
                    replacement,
                    [manifest_entry("self-si", own_content, 1)],
                    sync_seed="peer-prune-state-failure",
                ),
                headers={"ETag": '"team-context-owner-only"'},
            ),
        )
        with (
            mock.patch.object(
                team_context,
                "silicon_api_request",
                side_effect=api,
            ),
            mock.patch.object(
                team_context,
                "_save_state",
                side_effect=OSError("state volume is read-only"),
            ),
            mock.patch.object(
                team_context,
                "_write_team_placeholder",
                side_effect=PermissionError("TEAM.md is temporarily locked"),
            ),
        ):
            result = team_context.reconcile_team_context(
                self.root,
                force=False,
                reason="peer-prune-state-save-failure",
            )
        api.assert_drained()

        self.assertEqual(result["status"], "state_error")
        self.assertFalse(
            (self.root / "prompts" / "advertising" / "peer-si.md").exists()
        )
        self.assertTrue((self.root / "prompts" / "TEAM.md").exists())
        self.assertTrue(
            (
                self.root
                / "interface"
                / "state"
                / "team_context.blocked"
            ).exists()
        )
        self.assertEqual(team_context.read_verified_team_markdown(self.root), "")
        self.assertEqual(
            (self.root / "prompts" / "TEAM.md").read_text(encoding="utf-8"),
            replacement,
        )

    def test_state_save_failure_removes_new_untracked_peer_file(self):
        self._initial_sync()
        markdown = "# Alpha Silicon Team\n\nNew peer pending commit.\n"
        own_content = "Owner canonical"
        new_peer_content = "New peer must not escape a failed state commit"
        memories = [
            manifest_entry("self-si", own_content, 1),
            manifest_entry("new-peer", new_peer_content, 1),
        ]
        api = QueuedAPI(
            identity(),
            FakeResponse(
                body=context_payload(
                    markdown,
                    memories,
                    sync_seed="new-peer-state-failure",
                ),
                headers={"ETag": '"team-context-new-peer"'},
            ),
            FakeResponse(
                body=memory_payload("new-peer", new_peer_content, 1),
                headers={"ETag": '"new-peer-v1"'},
            ),
        )

        with (
            mock.patch.object(
                team_context,
                "silicon_api_request",
                side_effect=api,
            ),
            mock.patch.object(
                team_context,
                "_save_state",
                side_effect=OSError("state volume is read-only"),
            ),
        ):
            result = team_context.reconcile_team_context(
                self.root,
                force=False,
                reason="new-peer-state-save-failure",
            )
        api.assert_drained()

        self.assertEqual(result["status"], "state_error")
        self.assertFalse(
            (self.root / "prompts" / "advertising" / "new-peer.md").exists()
        )

    def test_authorization_loss_fails_closed_even_if_state_and_team_delete_fail(self):
        self._initial_sync()
        with (
            mock.patch.object(
                team_context,
                "silicon_api_request",
                return_value=FakeResponse(status_code=401),
            ),
            mock.patch.object(
                team_context,
                "_save_state",
                side_effect=OSError("state volume is read-only"),
            ),
            mock.patch.object(
                team_context,
                "_write_team_placeholder",
                side_effect=PermissionError("TEAM.md is locked"),
            ),
        ):
            result = team_context.reconcile_team_context(
                self.root,
                force=True,
                reason="authorization-loss",
            )

        self.assertEqual(result["status"], "state_error")
        self.assertTrue((self.root / "prompts" / "TEAM.md").exists())
        self.assertTrue(
            (self.root / "interface" / "state" / "team_context.blocked").exists()
        )
        self.assertEqual(team_context.read_verified_team_markdown(self.root), "")

        with mock.patch.object(
            team_context,
            "silicon_api_request",
            side_effect=OSError("Glass is temporarily unreachable"),
        ):
            retry = team_context.reconcile_team_context(
                self.root,
                force=True,
                reason="temporary-failure-after-revocation",
            )

        self.assertEqual(retry["status"], "unavailable")
        self.assertTrue(
            (self.root / "interface" / "state" / "team_context.blocked").exists()
        )
        self.assertEqual(team_context.read_verified_team_markdown(self.root), "")


if __name__ == "__main__":
    unittest.main()
