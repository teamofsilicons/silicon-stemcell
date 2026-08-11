import unittest

from interface.agent import live as glass_agent


class GlassAgentAuthenticationTests(unittest.TestCase):
    def test_status_socket_url_never_contains_the_permanent_key(self):
        url = glass_agent.ws_url("https://glass.example")
        self.assertEqual(url, "wss://glass.example/ws/glass/agent/")
        self.assertNotIn("silicon_key", url)

    def test_both_supported_websocket_exception_shapes_detect_auth_rejection(self):
        direct = RuntimeError("rejected")
        direct.status_code = 403
        self.assertTrue(glass_agent.is_authentication_rejection(direct))

        nested = RuntimeError("rejected")
        nested.response = type("Response", (), {"status_code": 401})()
        self.assertTrue(glass_agent.is_authentication_rejection(nested))
        self.assertFalse(glass_agent.is_authentication_rejection(RuntimeError("offline")))

    def test_configured_key_spellings_are_trimmed_consistently(self):
        self.assertEqual(
            glass_agent.glass_api_key({"api_key": "  primary-key \n"}),
            "primary-key",
        )
        self.assertEqual(
            glass_agent.glass_api_key(
                {"api_key": "", "silicon_api_key": "\tlegacy-key  "}
            ),
            "legacy-key",
        )

if __name__ == "__main__":
    unittest.main()
