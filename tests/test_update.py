import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from interface.release import updater as update
from helpers import state as state_store


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = (
            body
            if body is not None
            else ({"silicon_id": "test-silicon"} if status_code == 200 else {})
        )

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class SystemUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_paths = {
            "DOTENV_FILE": update.DOTENV_FILE,
            "ENV_PY_FILE": update.ENV_PY_FILE,
            "GLASS_CONFIG_FILE": update.GLASS_CONFIG_FILE,
            "SILICON_CONFIG_FILE": update.SILICON_CONFIG_FILE,
            "SILICON_INFO_FILE": update.SILICON_INFO_FILE,
            "UPDATE_STATE_FILE": update.UPDATE_STATE_FILE,
        }
        update.DOTENV_FILE = self.root / ".env"
        update.ENV_PY_FILE = self.root / "env.py"
        update.GLASS_CONFIG_FILE = self.root / ".glass.json"
        update.SILICON_CONFIG_FILE = self.root / "silicon.json"
        update.SILICON_INFO_FILE = self.root / "silicon.info"
        update.UPDATE_STATE_FILE = self.root / "state" / "system_update.json"
        self.old_environment = {
            key: os.environ.get(key)
            for key in (
                "GLASS_SERVER_URL",
                "SILICON_UPDATE_AUTH_KEY",
                "GLASS_API_KEY",
                "SILICON_STEMCELL_REPO",
            )
        }
        for key in self.old_environment:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.old_paths.items():
            setattr(update, key, value)
        for key, value in self.old_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_update_mismatch_records_read_only_availability_once(self):
        update.SILICON_INFO_FILE.write_text('{"version": "1.0.0"}\n', encoding="utf-8")
        latest = {
            "version": "1.1.0",
            "tag": "v1.1.0",
        }

        with mock.patch.object(
            update,
            "_fetch_latest_version",
            return_value=latest,
        ), mock.patch("builtins.print") as output:
            first = update.check_for_system_update(now=4000)
            second = update.check_for_system_update(now=8000)

        self.assertEqual(first, {})
        self.assertEqual(second, {})
        state = update._read_json(update.UPDATE_STATE_FILE, {})
        self.assertTrue(state["update_available"])
        self.assertEqual(state["last_notified_version"], "1.1.0")
        self.assertNotIn("apply_pid", state)
        rendered = " ".join(str(call) for call in output.call_args_list)
        self.assertEqual(rendered.count("silicon update <name>"), 1)
        self.assertNotIn("Stop the team", rendered)

    def test_forced_update_check_returns_status(self):
        update.SILICON_INFO_FILE.write_text('{"version": "1.0.0"}\n', encoding="utf-8")
        latest = {"version": "1.1.0"}
        with mock.patch.object(update, "_fetch_latest_version", return_value=latest):
            result = update.trigger_system_update_check(force=True)
        self.assertEqual(
            result,
            {
                "status": "available",
                "local_version": "1.0.0",
                "latest_version": "1.1.0",
                "update_available": True,
            },
        )

    def test_up_to_date_records_no_available_update(self):
        update.SILICON_INFO_FILE.write_text('{"version": "1.1.0"}\n', encoding="utf-8")
        latest = {"version": "1.1.0"}
        with mock.patch.object(update, "_fetch_latest_version", return_value=latest):
            update.check_for_system_update(now=4000)
        state = update._read_json(update.UPDATE_STATE_FILE, {})
        self.assertFalse(state["update_available"])

    def test_lower_published_tag_is_never_reported_as_an_update(self):
        update.SILICON_INFO_FILE.write_text('{"version": "2.0.0"}\n', encoding="utf-8")
        with mock.patch.object(
            update,
            "_fetch_latest_version",
            return_value={"version": "1.5.8", "tag": "v1.5.8"},
        ):
            result = update.trigger_system_update_check(force=True)

        self.assertEqual(result["status"], "up_to_date")
        self.assertFalse(result["update_available"])

    def test_legacy_two_part_local_version_can_see_a_newer_patch(self):
        update.SILICON_INFO_FILE.write_text('{"version": "1.5"}\n', encoding="utf-8")
        with mock.patch.object(
            update,
            "_fetch_latest_version",
            return_value={"version": "1.5.8", "tag": "v1.5.8"},
        ):
            result = update.trigger_system_update_check(force=True)

        self.assertEqual(result["status"], "available")
        self.assertTrue(result["update_available"])

    def test_update_check_redacts_credentials_from_state_and_console(self):
        exposed = "scs_live_" + "Q" * 43
        with mock.patch.object(
            update,
            "_fetch_latest_version",
            side_effect=RuntimeError(f"Authorization {exposed}\nsecond line"),
        ), mock.patch("builtins.print") as output:
            self.assertEqual(update.check_for_system_update(now=4000), {})

        state = update._read_json(update.UPDATE_STATE_FILE, {})
        self.assertNotIn(exposed, state["last_error"])
        self.assertNotIn("\n", state["last_error"])
        self.assertIn("[REDACTED SILICON KEY]", state["last_error"])
        rendered = " ".join(str(call) for call in output.call_args_list)
        self.assertNotIn(exposed, rendered)
        self.assertIn("[REDACTED SILICON KEY]", rendered)

    def test_update_check_redacts_legacy_unstructured_key(self):
        exposed = "legacy-key-with-no-modern-prefix"
        update.DOTENV_FILE.write_text(
            f"GLASS_API_KEY={exposed}\n",
            encoding="utf-8",
        )
        with mock.patch.object(
            update,
            "_fetch_latest_version",
            side_effect=RuntimeError(f"InvalidHeader({exposed})"),
        ), mock.patch("builtins.print") as output:
            self.assertEqual(update.check_for_system_update(now=4000), {})

        state = update._read_json(update.UPDATE_STATE_FILE, {})
        rendered = " ".join(str(call) for call in output.call_args_list)
        self.assertNotIn(exposed, state["last_error"])
        self.assertNotIn(exposed, rendered)
        self.assertIn("[REDACTED SILICON KEY]", state["last_error"])

    def test_fetch_latest_selects_highest_strict_stable_git_tag(self):
        advertised = "\n".join(
            [
                f"{'1' * 40}\trefs/tags/v1.9.0",
                f"{'2' * 40}\trefs/tags/v1.10.0",
                f"{'3' * 40}\trefs/tags/v1.10.0^{{}}",
                f"{'4' * 40}\trefs/tags/v2.0",
                f"{'5' * 40}\trefs/tags/v9.0.0-rc1",
            ]
        )
        completed = update.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=advertised,
            stderr="",
        )
        with mock.patch.object(
            update.shutil,
            "which",
            return_value="/usr/bin/git",
        ), mock.patch.object(
            update.subprocess,
            "run",
            return_value=completed,
        ) as run, mock.patch.object(
            update.requests,
            "get",
        ) as get:
            latest = update._fetch_latest_version()

        self.assertEqual(latest["version"], "1.10.0")
        self.assertEqual(latest["tag"], "v1.10.0")
        self.assertEqual(latest["revision"], "3" * 40)
        get.assert_not_called()
        self.assertEqual(
            run.call_args.args[0],
            [
                "git",
                "ls-remote",
                "--tags",
                "https://github.com/teamofsilicons/silicon-stemcell.git",
            ],
        )
        self.assertEqual(run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_git_environment_ignores_ambient_redirects_and_helpers(self):
        hostile = {
            "GIT_CONFIG_PARAMETERS": "'url.https://evil.invalid/.insteadOf' 'https://github.com/'",
            "GIT_DIR": "/tmp/attacker.git",
            "GIT_EXEC_PATH": "/tmp/attacker-bin",
            "GIT_SSL_NO_VERIFY": "1",
            "GIT_TEMPLATE_DIR": "/tmp/attacker-template",
            "GIT_CONFIG_KEY_99": "url.https://evil.invalid/.insteadOf",
            "GIT_CONFIG_VALUE_99": "https://github.com/",
            "GIT_ASKPASS": "/tmp/attacker-askpass",
        }
        with mock.patch.dict(os.environ, hostile, clear=False):
            environment = update._git_environment()

        for key in hostile:
            self.assertNotIn(key, environment)
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "7")
        self.assertEqual(environment["GIT_CONFIG_KEY_4"], "http.sslVerify")
        self.assertEqual(environment["GIT_CONFIG_VALUE_4"], "true")
        self.assertEqual(environment["GIT_CONFIG_KEY_5"], "protocol.allow")
        self.assertEqual(environment["GIT_CONFIG_VALUE_5"], "never")
        self.assertEqual(
            environment["GIT_CONFIG_KEY_6"],
            "protocol.https.allow",
        )
        self.assertEqual(environment["GIT_CONFIG_VALUE_6"], "always")

    def test_fetch_latest_returns_none_without_a_stable_tag(self):
        completed = update.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"{'1' * 40}\trefs/tags/v2.0.0-rc1\n",
            stderr="",
        )
        with mock.patch.object(
            update.shutil,
            "which",
            return_value="/usr/bin/git",
        ), mock.patch.object(
            update.subprocess,
            "run",
            return_value=completed,
        ):
            self.assertIsNone(update._fetch_latest_version())

    def test_fetch_latest_rejects_invalid_repository_before_git(self):
        os.environ["SILICON_STEMCELL_REPO"] = "https://attacker.invalid/repo"
        with mock.patch.object(
            update.subprocess,
            "run",
        ) as run, self.assertRaisesRegex(RuntimeError, "owner/repository"):
            update._fetch_latest_version()
        run.assert_not_called()

    def test_authenticated_rotation_stages_and_persists_client_generated_key(self):
        update.DOTENV_FILE.write_text(
            "GLASS_SERVER_URL=https://glass.example\nGLASS_API_KEY=scs_live_existing\n",
            encoding="utf-8",
        )
        update.GLASS_CONFIG_FILE.write_text(
            '{"server_url":"https://glass.example","api_key":"scs_live_existing"}\n',
            encoding="utf-8",
        )
        update.SILICON_CONFIG_FILE.write_text(
            '{"name":"Silicon","glass":{"api_key":"scs_live_existing"}}\n',
            encoding="utf-8",
        )
        update.ENV_PY_FILE.write_text(
            'GLASS_API_KEY = "scs_live_existing"\n'
            'SILICON_UPDATE_AUTH_KEY = "scs_live_old_duplicate"\n',
            encoding="utf-8",
        )
        replacement = "scs_live_" + "R" * 43

        def accept_rotation(*_args, **_kwargs):
            staged = update.DOTENV_FILE.read_text(encoding="utf-8")
            self.assertIn(f"{update.PENDING_AUTH_KEY_NAME}={replacement}", staged)
            self.assertEqual(stat.S_IMODE(update.DOTENV_FILE.stat().st_mode), 0o600)
            return FakeResponse(201, {"rotated": True})

        with mock.patch.object(update.secrets, "token_urlsafe", return_value="R" * 43), mock.patch.object(
            update.requests, "post", side_effect=accept_rotation
        ) as post, mock.patch.object(
            update.requests, "get", return_value=FakeResponse(200)
        ) as get:
            result = update._rotate_auth_key()

        self.assertEqual(result, replacement)
        self.assertEqual(post.call_args.kwargs["headers"], {"X-Silicon-Key": "scs_live_existing"})
        self.assertEqual(post.call_args.kwargs["json"], {"replacement_key": replacement})
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertNotIn("password", post.call_args.kwargs["json"])
        self.assertEqual(get.call_args.kwargs["headers"], {"X-Silicon-Key": replacement})
        self.assertTrue(get.call_args.args[0].endswith(update.AUTH_IDENTITY_PATH))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        dotenv = update.DOTENV_FILE.read_text(encoding="utf-8")
        self.assertNotIn("GLASS_API_KEY=", dotenv)
        self.assertNotIn("SILICON_UPDATE_AUTH_KEY=", dotenv)
        self.assertNotIn(update.PENDING_AUTH_KEY_NAME, dotenv)
        self.assertEqual(stat.S_IMODE(update.DOTENV_FILE.stat().st_mode), 0o600)
        self.assertEqual(list(update.DOTENV_FILE.parent.glob(".*.tmp")), [])
        self.assertEqual(
            json.loads(update.GLASS_CONFIG_FILE.read_text(encoding="utf-8"))["api_key"],
            replacement,
        )
        self.assertNotIn(
            "api_key",
            json.loads(
                update.SILICON_CONFIG_FILE.read_text(encoding="utf-8")
            )["glass"],
        )
        self.assertEqual(
            update.ENV_PY_FILE.read_text(encoding="utf-8"),
            'GLASS_API_KEY = ""\n',
        )
        for path in (
            update.DOTENV_FILE,
            update.GLASS_CONFIG_FILE,
            update.SILICON_CONFIG_FILE,
            update.ENV_PY_FILE,
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_rotation_recovers_when_post_response_is_lost_after_commit(self):
        update.DOTENV_FILE.write_text(
            "GLASS_SERVER_URL=https://glass.example\nGLASS_API_KEY=scs_live_existing\n",
            encoding="utf-8",
        )
        update.SILICON_CONFIG_FILE.write_text(
            json.dumps(
                {
                    "address": "legacy-worker",
                    "glass": {
                        "silicon_id": "silicon-legacy",
                        "api_key": "scs_live_existing",
                    },
                }
            ),
            encoding="utf-8",
        )
        replacement = "scs_live_" + "S" * 43
        with mock.patch.object(update.secrets, "token_urlsafe", return_value="S" * 43), mock.patch.object(
            update.requests, "post", side_effect=update.requests.Timeout("response lost")
        ), mock.patch.object(
            update.requests,
            "get",
            return_value=FakeResponse(200, {"silicon_id": "silicon-legacy"}),
        ) as get:
            result = update._rotate_auth_key()

        self.assertEqual(result, replacement)
        self.assertEqual(get.call_args.kwargs["headers"], {"X-Silicon-Key": replacement})
        dotenv = update.DOTENV_FILE.read_text(encoding="utf-8")
        self.assertNotIn("GLASS_API_KEY=", dotenv)
        self.assertNotIn(update.PENDING_AUTH_KEY_NAME, dotenv)
        canonical = json.loads(
            update.GLASS_CONFIG_FILE.read_text(encoding="utf-8")
        )
        self.assertEqual(canonical["api_key"], replacement)
        self.assertEqual(canonical["server_url"], "https://glass.example")
        self.assertEqual(canonical["silicon_id"], "silicon-legacy")
        self.assertEqual(canonical["silicon_username"], "legacy-worker")
        self.assertEqual(update._auth_key(), replacement)
        self.assertNotIn(
            "api_key",
            json.loads(
                update.SILICON_CONFIG_FILE.read_text(encoding="utf-8")
            )["glass"],
        )

    def test_restarted_rotation_promotes_committed_pending_key_without_rotating_again(self):
        replacement = "scs_live_" + "U" * 43
        update.DOTENV_FILE.write_text(
            "GLASS_SERVER_URL=https://glass.example\n"
            "GLASS_API_KEY=scs_live_existing\n"
            f"{update.PENDING_AUTH_KEY_NAME}={replacement}\n",
            encoding="utf-8",
        )
        with mock.patch.object(update.requests, "get", return_value=FakeResponse(200)), mock.patch.object(
            update.requests, "post"
        ) as post:
            result = update._rotate_auth_key()

        self.assertEqual(result, replacement)
        post.assert_not_called()
        dotenv = update.DOTENV_FILE.read_text(encoding="utf-8")
        self.assertNotIn("GLASS_API_KEY=", dotenv)
        self.assertNotIn(update.PENDING_AUTH_KEY_NAME, dotenv)

    def test_rotation_rejected_by_glass_keeps_existing_key(self):
        update.DOTENV_FILE.write_text(
            "GLASS_SERVER_URL=https://glass.example\nGLASS_API_KEY=scs_live_existing\n",
            encoding="utf-8",
        )
        def authentication_truth(*_args, **kwargs):
            key = kwargs["headers"]["X-Silicon-Key"]
            return FakeResponse(200 if key == "scs_live_existing" else 401)

        with mock.patch.object(update.secrets, "token_urlsafe", return_value="T" * 43), mock.patch.object(
            update.requests, "post", return_value=FakeResponse(401)
        ), mock.patch.object(
            update.requests, "get", side_effect=authentication_truth
        ), self.assertRaisesRegex(update.UpdateAuthenticationError, "owner.*reprovision"):
            update._rotate_auth_key()

        dotenv = update.DOTENV_FILE.read_text(encoding="utf-8")
        self.assertIn("GLASS_API_KEY=scs_live_existing", dotenv)
        self.assertNotIn(update.PENDING_AUTH_KEY_NAME, dotenv)

    def test_rotation_does_not_trust_success_until_candidate_authenticates(self):
        update.DOTENV_FILE.write_text(
            "GLASS_SERVER_URL=https://glass.example\nGLASS_API_KEY=scs_live_existing\n",
            encoding="utf-8",
        )

        def authentication_truth(*_args, **kwargs):
            key = kwargs["headers"]["X-Silicon-Key"]
            return FakeResponse(200 if key == "scs_live_existing" else 401)

        with mock.patch.object(update.secrets, "token_urlsafe", return_value="V" * 43), mock.patch.object(
            update.requests, "post", return_value=FakeResponse(201, {"rotated": True})
        ), mock.patch.object(
            update.requests, "get", side_effect=authentication_truth
        ), self.assertRaisesRegex(RuntimeError, "replacement key is not active"):
            update._rotate_auth_key()

        dotenv = update.DOTENV_FILE.read_text(encoding="utf-8")
        self.assertIn("GLASS_API_KEY=scs_live_existing", dotenv)
        self.assertNotIn(update.PENDING_AUTH_KEY_NAME, dotenv)

    def test_authenticated_requests_never_follow_redirects(self):
        update.DOTENV_FILE.write_text(
            "GLASS_SERVER_URL=https://glass.example\nGLASS_API_KEY=scs_live_existing\n",
            encoding="utf-8",
        )
        with mock.patch.object(update.secrets, "token_urlsafe", return_value="W" * 43), mock.patch.object(
            update.requests, "post", return_value=FakeResponse(307)
        ) as post, self.assertRaisesRegex(update.UpdateAuthenticationError, "redirected"):
            update._rotate_auth_key()

        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertIn(update.PENDING_AUTH_KEY_NAME, update.DOTENV_FILE.read_text(encoding="utf-8"))

    def test_remote_plain_http_is_rejected_before_staging_or_sending_key(self):
        update.DOTENV_FILE.write_text(
            "GLASS_SERVER_URL=http://glass.example\nGLASS_API_KEY=scs_live_existing\n",
            encoding="utf-8",
        )
        with mock.patch.object(update.requests, "post") as post, self.assertRaisesRegex(
            update.UpdateAuthenticationError,
            "non-HTTPS",
        ):
            update._rotate_auth_key()

        post.assert_not_called()
        self.assertNotIn(
            update.PENDING_AUTH_KEY_NAME,
            update.DOTENV_FILE.read_text(encoding="utf-8"),
        )

    def test_localhost_trailing_dot_is_supported_for_local_development(self):
        update.DOTENV_FILE.write_text(
            "GLASS_SERVER_URL=http://localhost.:8000\n",
            encoding="utf-8",
        )
        self.assertEqual(
            update._authenticated_server_url(),
            "http://localhost.:8000",
        )

    def test_pending_journal_repairs_interrupted_multi_file_persistence(self):
        replacement = "scs_live_" + "X" * 43
        update.DOTENV_FILE.write_text(
            "GLASS_SERVER_URL=https://glass.example\n"
            "GLASS_API_KEY=scs_live_existing\n"
            f"{update.PENDING_AUTH_KEY_NAME}={replacement}\n",
            encoding="utf-8",
        )
        update.GLASS_CONFIG_FILE.write_text(
            '{"server_url":"https://glass.example","api_key":"scs_live_existing"}\n',
            encoding="utf-8",
        )
        update.SILICON_CONFIG_FILE.write_text("{malformed", encoding="utf-8")

        with mock.patch.object(update.requests, "get", return_value=FakeResponse(200)), self.assertRaisesRegex(
            RuntimeError,
            "malformed credential file",
        ):
            update._recover_pending_auth_key("scs_live_existing")

        self.assertEqual(
            json.loads(update.GLASS_CONFIG_FILE.read_text(encoding="utf-8"))["api_key"],
            replacement,
        )
        self.assertIn(update.PENDING_AUTH_KEY_NAME, update.DOTENV_FILE.read_text(encoding="utf-8"))

        update.SILICON_CONFIG_FILE.write_text(
            '{"glass":{"api_key":"scs_live_existing"}}\n',
            encoding="utf-8",
        )
        with mock.patch.object(update.requests, "get", return_value=FakeResponse(200)):
            recovered = update._recover_pending_auth_key("scs_live_existing")
        self.assertEqual(recovered, replacement)
        self.assertNotIn(update.PENDING_AUTH_KEY_NAME, update.DOTENV_FILE.read_text(encoding="utf-8"))
        self.assertNotIn(
            "api_key",
            json.loads(
                update.SILICON_CONFIG_FILE.read_text(encoding="utf-8")
            )["glass"],
        )

    def test_infrastructure_404_is_not_accepted_as_key_authentication(self):
        replacement = "scs_live_" + "Y" * 43
        update.DOTENV_FILE.write_text(
            "GLASS_SERVER_URL=https://glass.example\n"
            "GLASS_API_KEY=scs_live_existing\n"
            f"{update.PENDING_AUTH_KEY_NAME}={replacement}\n",
            encoding="utf-8",
        )
        with mock.patch.object(
            update.requests,
            "get",
            return_value=FakeResponse(404),
        ) as get, self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            update._recover_pending_auth_key("scs_live_existing")

        self.assertTrue(get.call_args.args[0].endswith(update.AUTH_IDENTITY_PATH))
        dotenv = update.DOTENV_FILE.read_text(encoding="utf-8")
        self.assertIn(f"{update.PENDING_AUTH_KEY_NAME}={replacement}", dotenv)
        self.assertIn("GLASS_API_KEY=scs_live_existing", dotenv)

    def test_identity_probe_must_match_configured_silicon(self):
        replacement = "scs_live_" + "I" * 43
        update.DOTENV_FILE.write_text(
            "GLASS_SERVER_URL=https://glass.example\n"
            "GLASS_API_KEY=scs_live_existing\n"
            f"{update.PENDING_AUTH_KEY_NAME}={replacement}\n",
            encoding="utf-8",
        )
        update.GLASS_CONFIG_FILE.write_text(
            json.dumps(
                {
                    "server_url": "https://glass.example",
                    "silicon_username": "expected-silicon",
                    "api_key": "scs_live_existing",
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            update.requests,
            "get",
            return_value=FakeResponse(200, {"silicon_id": "another-silicon"}),
        ), self.assertRaisesRegex(update.UpdateAuthenticationError, "wrong Silicon identity"):
            update._recover_pending_auth_key("scs_live_existing")

        self.assertIn(
            f"{update.PENDING_AUTH_KEY_NAME}={replacement}",
            update.DOTENV_FILE.read_text(encoding="utf-8"),
        )

    def test_canonical_glass_file_wins_over_stale_inherited_environment(self):
        update.GLASS_CONFIG_FILE.write_text(
            '{"server_url":"https://glass.example","api_key":"scs_live_rotated"}\n',
            encoding="utf-8",
        )
        update.DOTENV_FILE.write_text(
            "GLASS_SERVER_URL=https://stale-glass.example\n"
            "GLASS_API_KEY=scs_live_stale_file\n",
            encoding="utf-8",
        )
        os.environ["SILICON_UPDATE_AUTH_KEY"] = "scs_live_stale_parent"
        os.environ["GLASS_API_KEY"] = "scs_live_stale_parent"
        os.environ["GLASS_SERVER_URL"] = "https://stale-parent.example"
        self.assertEqual(update._auth_key(), "scs_live_rotated")
        self.assertEqual(update._server_url(), "https://glass.example")

        with mock.patch.object(
            update.requests,
            "get",
            return_value=FakeResponse(200),
        ) as get:
            update._get_identity_with_key(update._auth_key())
        self.assertEqual(
            get.call_args.args[0],
            "https://glass.example/api/v1/silicons/me",
        )
        self.assertEqual(
            get.call_args.kwargs["headers"],
            {"X-Silicon-Key": "scs_live_rotated"},
        )

    def test_auth_key_lock_serializes_local_rotation(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        def first():
            with state_store.file_lock(update.UPDATE_STATE_FILE.parent / update.AUTH_KEY_LOCK_NAME):
                first_entered.set()
                release_first.wait(timeout=2)

        def second():
            first_entered.wait(timeout=2)
            with state_store.file_lock(update.UPDATE_STATE_FILE.parent / update.AUTH_KEY_LOCK_NAME):
                second_entered.set()

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        first_thread.start()
        second_thread.start()
        self.assertTrue(first_entered.wait(timeout=1))
        self.assertFalse(second_entered.wait(timeout=0.05))
        release_first.set()
        self.assertTrue(second_entered.wait(timeout=1))
        first_thread.join(timeout=1)
        second_thread.join(timeout=1)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())

    def test_rotation_cli_redacts_key_material_from_failures(self):
        exposed = "scs_live_" + "Z" * 43
        with mock.patch.object(
            update,
            "_rotate_auth_key",
            side_effect=RuntimeError(f"invalid header contained {exposed}\nsecond line"),
        ), mock.patch("builtins.print") as output:
            result = update.main(["rotate-key"])

        self.assertEqual(result, 1)
        rendered = " ".join(str(call) for call in output.call_args_list)
        self.assertNotIn(exposed, rendered)
        self.assertIn("[REDACTED SILICON KEY]", rendered)
        self.assertNotIn("second line\n", rendered)


class GlassAgentUpdateCommandTest(unittest.TestCase):
    """The agent reports release status but never mutates a running instance."""

    def _run(self, result, action="update"):
        from interface.agent import live as glass_agent

        command = {"command": action}
        with mock.patch.object(
            update,
            "trigger_system_update_check",
            return_value=result,
        ) as check:
            status, detail = glass_agent.execute_command(
                command, Path("/tmp/x"), "worker"
            )
        return status, detail, check, command

    def test_live_update_is_refused_with_host_transaction_instruction(self):
        status, detail, check, command = self._run(
            {
                "status": "available",
                "local_version": "1.4",
                "latest_version": "1.5",
                "update_available": True,
            }
        )

        self.assertEqual(status, "failed")
        self.assertIn("silicon update <name>", detail)
        self.assertNotIn("stop the team", detail.lower())
        self.assertNotIn("_agent_reexec", command)
        check.assert_called_once_with(force=True)

    def test_up_to_date_check_does_not_restart(self):
        status, detail, check, command = self._run(
            {
                "status": "up_to_date",
                "local_version": "1.5",
                "latest_version": "1.5",
                "update_available": False,
            },
            action="update_check",
        )
        self.assertEqual(status, "done")
        self.assertEqual(detail, "already on 1.5")
        self.assertNotIn("_agent_reexec", command)
        check.assert_called_once_with(force=True)


if __name__ == "__main__":
    unittest.main()
