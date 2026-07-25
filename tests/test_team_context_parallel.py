from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from core import team_context


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _manifest_entry(silicon_id: str, content: str, revision: int) -> dict:
    return {
        "silicon_id": silicon_id,
        "path": f"prompts/advertising/{silicon_id}.md",
        "revision": revision,
        "sha256": _digest(content),
        "updated_at": None,
    }


class _Response:
    status_code = 200
    headers = {"ETag": '"team-context-v1"'}

    def __init__(self, body: dict):
        self._body = body

    def json(self) -> dict:
        return self._body


class ParallelPeerSyncTests(unittest.TestCase):
    def test_peer_sync_is_bounded_and_state_order_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompts").mkdir()
            (root / ".glass.json").write_text(
                json.dumps(
                    {
                        "server_url": "https://glass.example",
                        "api_key": "scs_live_test",
                    }
                ),
                encoding="utf-8",
            )
            identity = {
                "silicon_id": "self-si",
                "team_slug": "alpha",
                "server_origin": "https://glass.example",
                "credential_fingerprint": team_context._credential_fingerprint(
                    {
                        "server_url": "https://glass.example",
                        "api_key": "scs_live_test",
                    }
                ),
                "access_valid": True,
            }
            peer_contents = {
                f"peer-{index:02d}": f"Peer memory {index}"
                for index in range(team_context.MAX_PARALLEL_PEER_SYNCS + 2)
            }
            manifest = {
                "self-si": _manifest_entry("self-si", "", 0),
                **{
                    silicon_id: _manifest_entry(silicon_id, content, 1)
                    for silicon_id, content in peer_contents.items()
                },
            }
            markdown = "# Alpha Silicon Team\n"
            response = _Response(
                {
                    "team_id": "team-alpha",
                    "team_slug": "alpha",
                    "path": team_context.TEAM_CONTEXT_PATH,
                    "revision": _digest(markdown),
                    "sync_revision": _digest("sync-1"),
                    "markdown": markdown,
                    "advertising_memories": list(manifest.values()),
                }
            )

            guard = threading.Lock()
            release_first_batch = threading.Event()
            active = 0
            maximum_active = 0

            def fetch_peer(_root, _identity, entry, *, etag="", config=None):
                del _root, _identity, etag, config
                nonlocal active, maximum_active
                with guard:
                    active += 1
                    maximum_active = max(maximum_active, active)
                    if active == team_context.MAX_PARALLEL_PEER_SYNCS:
                        release_first_batch.set()
                try:
                    if not release_first_batch.wait(timeout=2):
                        raise AssertionError("peer requests did not overlap")
                    # Make completion order differ from manifest order.
                    index = int(entry["silicon_id"].rsplit("-", 1)[1])
                    time.sleep(
                        max(0, team_context.MAX_PARALLEL_PEER_SYNCS - index) * 0.005
                    )
                    return {
                        **entry,
                        "content": peer_contents[entry["silicon_id"]],
                    }, f'"{entry["silicon_id"]}-v1"'
                finally:
                    with guard:
                        active -= 1

            with (
                mock.patch.object(
                    team_context,
                    "_fetch_identity",
                    return_value=identity,
                ),
                mock.patch.object(
                    team_context,
                    "_get_context_response",
                    return_value=response,
                ),
                mock.patch.object(
                    team_context,
                    "_fetch_peer",
                    side_effect=fetch_peer,
                ),
                mock.patch.object(
                    team_context,
                    "_sync_own",
                    return_value={
                        "ok": True,
                        "status": "unchanged",
                        "changed": False,
                        "local_saved": True,
                    },
                ),
            ):
                state = team_context._default_state()
                result = team_context._reconcile_locked(
                    root,
                    state,
                    force=True,
                    reason="test",
                )

            expected_ids = sorted(peer_contents)
            self.assertEqual(maximum_active, team_context.MAX_PARALLEL_PEER_SYNCS)
            self.assertLessEqual(maximum_active, team_context.MAX_PARALLEL_PEER_SYNCS)
            self.assertEqual(list(state["peers"]), expected_ids)
            self.assertEqual(state["managed_peer_ids"], expected_ids)
            self.assertEqual(result["peer_files_changed"], len(expected_ids))
            for silicon_id, expected_content in peer_contents.items():
                self.assertEqual(
                    (
                        root / team_context.ADVERTISING_DIRECTORY / f"{silicon_id}.md"
                    ).read_text(encoding="utf-8"),
                    expected_content,
                )

    def test_authorization_failure_removes_peer_written_by_same_parallel_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prompts").mkdir()
            (root / ".glass.json").write_text(
                json.dumps(
                    {
                        "server_url": "https://glass.example",
                        "api_key": "scs_live_test",
                    }
                ),
                encoding="utf-8",
            )
            identity = {
                "silicon_id": "self-si",
                "team_slug": "alpha",
                "server_origin": "https://glass.example",
                "credential_fingerprint": team_context._credential_fingerprint(
                    {
                        "server_url": "https://glass.example",
                        "api_key": "scs_live_test",
                    }
                ),
                "access_valid": True,
            }
            markdown = "# Alpha Silicon Team\n"
            manifest = [
                _manifest_entry("self-si", "", 0),
                _manifest_entry("peer-a", "peer A private memory", 1),
                _manifest_entry("peer-b", "peer B private memory", 1),
            ]
            response = _Response(
                {
                    "team_id": "team-alpha",
                    "team_slug": "alpha",
                    "path": team_context.TEAM_CONTEXT_PATH,
                    "revision": _digest(markdown),
                    "sync_revision": _digest("sync-auth-failure"),
                    "markdown": markdown,
                    "advertising_memories": manifest,
                }
            )

            def sync_peer(sync_root, _identity, entry, _old_record, *, config=None):
                del sync_root, _identity, _old_record, config
                if entry["silicon_id"] == "peer-b":
                    raise team_context.TeamContextError(
                        "access revoked",
                        status_code=403,
                    )
                return (
                    {**entry, "etag": '"peer-a-v1"'},
                    True,
                    b"peer A private memory",
                )

            with (
                mock.patch.object(
                    team_context,
                    "_fetch_identity",
                    return_value=identity,
                ),
                mock.patch.object(
                    team_context,
                    "_get_context_response",
                    return_value=response,
                ),
                mock.patch.object(
                    team_context,
                    "_sync_peer",
                    side_effect=sync_peer,
                ),
            ):
                result = team_context.reconcile_team_context(root, force=True)

            self.assertEqual(result["status"], "unauthorized")
            self.assertFalse(
                (
                    root
                    / team_context.ADVERTISING_DIRECTORY
                    / "peer-a.md"
                ).exists()
            )
            state = json.loads(
                (
                    root
                    / "core"
                    / "interface_state"
                    / "team_context.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(state["peers"], {})
            self.assertEqual(state["managed_peer_ids"], [])


if __name__ == "__main__":
    unittest.main()
