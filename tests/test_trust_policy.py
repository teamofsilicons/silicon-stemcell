import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core.interface as interface
from core import trust


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.content = b"{}" if body is not None else b""

    def json(self):
        return self._body


def policy(level="high", *, team_revision=2, silicon_revision=3):
    return {
        "schema_version": 1,
        "source_silicon": {"id": "source-si", "name": "Source Silicon"},
        "team": {"id": "team-1", "slug": "alpha"},
        "default_level": "very_low",
        "team_revision": team_revision,
        "silicon_revision": silicon_revision,
        "revision": f"{team_revision}:{silicon_revision}",
        "entries": [
            {
                "target": {"kind": "carbon", "id": "alice", "name": "Alice"},
                "base_level": "ok",
                "override_level": level,
                "override_revision": 1,
                "effective_level": level,
                "effective_source": "silicon_override",
                "central_carbon": False,
            },
            {
                "target": {
                    "kind": "silicon",
                    "id": "peer-si",
                    "name": "Peer Silicon",
                },
                "base_level": "low",
                "override_level": None,
                "override_revision": 0,
                "effective_level": "low",
                "effective_source": "team_base",
                "central_carbon": False,
            },
        ],
    }


class TrustPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_data_root = trust.DATA_ROOT
        self.old_contacts = interface.CONTACTS_FILE
        self.old_backup = interface.CONTACTS_BACKUP_FILE
        trust.DATA_ROOT = self.root
        interface.CONTACTS_FILE = self.root / "core/interface_state/contacts.json"
        interface.CONTACTS_BACKUP_FILE = (
            self.root / "core/interface_state/contacts_backup.json"
        )

    def tearDown(self):
        trust.DATA_ROOT = self.old_data_root
        interface.CONTACTS_FILE = self.old_contacts
        interface.CONTACTS_BACKUP_FILE = self.old_backup
        self.tmp.cleanup()

    def test_confirmed_policy_updates_typed_contacts_and_unknowns_fail_closed(self):
        interface.upsert_contact("carbon", "alice", room_id="room-a")
        interface.upsert_contact("silicon", "peer-si", room_id="room-s")
        interface.upsert_contact("carbon", "unknown", room_id="room-u")

        snapshot = policy()
        snapshot["entries"][0]["central_carbon"] = True
        snapshot["entries"][0]["effective_level"] = "ultimate"
        snapshot["entries"][0]["effective_source"] = "central_carbon"
        validated = trust._validate_policy(snapshot)
        changed = trust._apply_confirmed_policy(
            self.root,
            snapshot,
        )["changed_contacts"]

        self.assertEqual(changed, 2)
        self.assertEqual(interface.get_contact("alice")["trust_level"], "ultimate")
        self.assertTrue(interface.get_contact("alice")["is_central_carbon"])
        self.assertEqual(interface.get_central_contact_id(), "alice")
        self.assertEqual(interface.get_contact("peer-si")["trust_level"], "low")
        self.assertEqual(interface.get_contact("unknown")["trust_level"], "very_low")
        self.assertFalse(interface.get_contact("unknown")["is_central_carbon"])
        self.assertNotEqual(
            validated["entries"]["carbon:alice"],
            validated["entries"]["silicon:peer-si"],
        )

    def test_bootstrap_fetch_applies_and_acknowledges_glass_revision(self):
        interface.upsert_contact("carbon", "alice", room_id="room-a")
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path.endswith("trust-bootstrap"):
                return FakeResponse(
                    body={
                        "result": {"bootstrapped": True},
                        "policy": policy("very_high"),
                    }
                )
            if path.endswith("trust-ack"):
                return FakeResponse(body={"applied": True})
            raise AssertionError(path)

        with mock.patch("core.trust.silicon_api_request", side_effect=request):
            result = trust.reconcile_trust_policy(
                self.root,
                force=True,
                reason="test",
            )

        self.assertEqual(result["status"], "updated")
        self.assertEqual(interface.get_contact("alice")["trust_level"], "very_high")
        self.assertEqual(
            [path for _method, path, _kwargs in calls],
            [
                "/api/v1/silicons/me/trust-bootstrap",
                "/api/v1/silicons/me/trust-ack",
            ],
        )
        self.assertEqual(calls[0][2]["json_body"], {"contacts": []})
        state = trust._load_state(self.root)
        self.assertTrue(state["server_bootstrapped"])
        self.assertEqual(state["revision"], "2:3")

    def test_local_change_is_not_applied_until_glass_commits(self):
        interface.upsert_contact("carbon", "alice", room_id="room-a")
        trust._apply_confirmed_policy(self.root, policy("ok", team_revision=1, silicon_revision=1))

        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path.endswith("trust-overrides"):
                return FakeResponse(
                    body=policy("high", team_revision=1, silicon_revision=2)
                )
            if path.endswith("trust-ack"):
                return FakeResponse(body={"applied": True})
            raise AssertionError(path)

        with mock.patch("core.trust.silicon_api_request", side_effect=request):
            result = trust.set_contact_trust(
                "carbon",
                "alice",
                "high",
                initiated_by_carbon_id="founder",
                root=self.root,
            )

        self.assertEqual(result["level"], "high")
        self.assertEqual(interface.get_contact("alice")["trust_level"], "high")
        mutation = calls[0][2]["json_body"]
        self.assertEqual(mutation["target_kind"], "carbon")
        self.assertEqual(mutation["target_id"], "alice")
        self.assertEqual(mutation["expected_revision"], 1)
        self.assertEqual(mutation["initiated_by_carbon_id"], "founder")

    def test_stale_local_change_refreshes_canonical_policy_without_overwriting(self):
        interface.upsert_contact("carbon", "alice", room_id="room-a")
        trust._apply_confirmed_policy(self.root, policy("ok"))

        response = FakeResponse(
            status_code=409,
            body={
                "detail": "The Silicon override changed; refresh and retry.",
                "policy": policy("very_high", team_revision=3, silicon_revision=4),
            },
        )
        with mock.patch(
            "core.trust.silicon_api_request",
            return_value=response,
        ):
            with self.assertRaises(trust.TrustSyncError):
                trust.set_contact_trust(
                    "carbon",
                    "alice",
                    "low",
                    root=self.root,
                )

        self.assertEqual(interface.get_contact("alice")["trust_level"], "very_high")

    def test_invalidation_keeps_last_confirmed_policy_until_new_revision_arrives(self):
        interface.upsert_contact("carbon", "alice", room_id="room-a")
        trust._apply_confirmed_policy(
            self.root,
            policy("high", team_revision=2, silicon_revision=3),
        )
        self.assertEqual(
            trust.cached_trust_level("carbon", "alice", root=self.root),
            "high",
        )

        trust.mark_trust_policy_invalidated(
            team_revision=3,
            silicon_revision=3,
            root=self.root,
        )

        self.assertTrue(trust.has_confirmed_policy(root=self.root))
        self.assertEqual(
            trust.cached_trust_level("carbon", "alice", root=self.root),
            "high",
        )
        pending = trust.confirmed_trust_policy_snapshot(root=self.root)
        self.assertEqual(pending["status"], "refresh_pending")
        self.assertEqual(
            next(entry for entry in pending["entries"] if entry["id"] == "alice")[
                "level"
            ],
            "high",
        )

        with mock.patch(
            "core.trust.silicon_api_request",
            return_value=FakeResponse(status_code=503),
        ):
            deferred = trust.reconcile_trust_policy(
                self.root,
                force=True,
                reason="test-refresh-failure",
            )
        self.assertEqual(deferred["status"], "deferred")
        self.assertEqual(
            trust.cached_trust_level("carbon", "alice", root=self.root),
            "high",
        )

        trust._apply_confirmed_policy(
            self.root,
            policy("very_high", team_revision=3, silicon_revision=3),
        )
        self.assertTrue(trust.has_confirmed_policy(root=self.root))
        self.assertEqual(
            trust.cached_trust_level("carbon", "alice", root=self.root),
            "very_high",
        )

    def test_pending_policy_must_refresh_before_a_trust_mutation(self):
        trust._apply_confirmed_policy(
            self.root,
            policy("high", team_revision=2, silicon_revision=3),
        )
        trust.mark_trust_policy_invalidated(
            team_revision=3,
            silicon_revision=3,
            root=self.root,
        )

        with mock.patch(
            "core.trust.reconcile_trust_policy",
            return_value={"status": "deferred"},
        ):
            with self.assertRaises(trust.TrustSyncError):
                trust.set_contact_trust(
                    "carbon",
                    "alice",
                    "low",
                    root=self.root,
                )

        self.assertEqual(
            trust.cached_trust_level("carbon", "alice", root=self.root),
            "high",
        )

    def test_snapshot_exposes_names_and_resolution_provenance(self):
        trust._apply_confirmed_policy(self.root, policy("high"))

        snapshot = trust.confirmed_trust_policy_snapshot(root=self.root)

        self.assertEqual(snapshot["status"], "current")
        alice = next(
            entry for entry in snapshot["entries"] if entry["id"] == "alice"
        )
        self.assertEqual(alice["name"], "Alice")
        self.assertEqual(alice["level"], "high")
        self.assertEqual(alice["source"], "silicon_override")


if __name__ == "__main__":
    unittest.main()
