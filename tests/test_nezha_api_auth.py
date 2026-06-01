import unittest
import importlib.util

if importlib.util.find_spec("aiohttp") is not None:
    from nezha_api import NezhaAPI


@unittest.skipIf(importlib.util.find_spec("aiohttp") is None, "aiohttp is not installed")
class NezhaAPIAuthTests(unittest.TestCase):
    def test_password_auth_starts_without_bearer_token(self):
        api = NezhaAPI("https://nezha.example", "alice", "secret")
        self.assertEqual(api.auth_type, "password")
        self.assertIsNone(api.token)

    def test_api_token_auth_uses_supplied_bearer_without_login(self):
        api = NezhaAPI(
            "https://nezha.example",
            auth_type="token",
            api_token="nzp_test_secret",
        )
        self.assertEqual(api.auth_type, "token")
        self.assertEqual(api.token, "nzp_test_secret")

    def test_password_auth_can_keep_parallel_api_token_without_read_bearer(self):
        api = NezhaAPI(
            "https://nezha.example",
            "alice",
            "secret",
            auth_type="password",
            api_token="nzp_parallel_secret",
        )

        self.assertEqual(api.auth_type, "password")
        self.assertEqual(api.api_token, "nzp_parallel_secret")
        self.assertIsNone(api.token)


if __name__ == "__main__":
    unittest.main()
