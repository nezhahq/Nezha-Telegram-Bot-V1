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

    def test_password_post_requests_include_csrf_token(self):
        api = NezhaAPI("https://nezha.example", "alice", "secret")
        api.token = "jwt_token"
        api.csrf_token = "csrf.signed"

        headers = api.build_request_headers("POST")

        self.assertEqual(headers["Authorization"], "Bearer jwt_token")
        self.assertEqual(headers["X-CSRF-Token"], "csrf.signed")

    def test_password_get_requests_do_not_include_csrf_token(self):
        api = NezhaAPI("https://nezha.example", "alice", "secret")
        api.token = "jwt_token"
        api.csrf_token = "csrf.signed"

        headers = api.build_request_headers("GET")

        self.assertEqual(headers["Authorization"], "Bearer jwt_token")
        self.assertNotIn("X-CSRF-Token", headers)

    def test_api_token_post_requests_do_not_need_csrf_token(self):
        api = NezhaAPI(
            "https://nezha.example",
            auth_type="token",
            api_token="nzp_test_secret",
        )
        api.csrf_token = "csrf.signed"

        headers = api.build_request_headers("POST")

        self.assertEqual(headers["Authorization"], "Bearer nzp_test_secret")
        self.assertNotIn("X-CSRF-Token", headers)

    def test_service_history_uses_latest_history_endpoint(self):
        calls = []

        class API(NezhaAPI):
            async def request(self, method, endpoint, **kwargs):
                calls.append((method, endpoint, kwargs))
                return {"success": True, "data": []}

        api = API("https://nezha.example", "alice", "secret")

        import asyncio

        result = asyncio.run(api.get_service_histories(12, period="7d"))

        self.assertTrue(result["success"])
        self.assertEqual(calls, [("GET", "/service/12/history", {"params": {"period": "7d"}})])

    def test_mcp_exec_output_prefers_structured_content(self):
        api = NezhaAPI("https://nezha.example", auth_type="token", api_token="nzp_test_secret")

        text = api.format_mcp_exec_output(
            {
                "structuredContent": {
                    "exit_code": 2,
                    "stdout": "ok\n",
                    "stderr": "warning\n",
                },
                "content": [{"text": '{"exit_code":2,"stdout":"fallback"}'}],
            }
        )

        self.assertIn("exit_code=2", text)
        self.assertIn("stdout:\nok", text)
        self.assertIn("stderr:\nwarning", text)

    def test_validate_api_token_uses_mcp_whoami_not_inventory_list(self):
        calls = []

        class API(NezhaAPI):
            async def mcp_call(self, tool_name, arguments=None, request_id=1):
                calls.append((tool_name, arguments, request_id))
                return {"structuredContent": {"token_id": 1, "scopes": ["nezha:server:exec"]}}

            async def get_servers(self):
                raise AssertionError("API token validation should not require inventory read")

        api = API("https://nezha.example", auth_type="token", api_token="nzp_test_secret")

        import asyncio

        self.assertTrue(asyncio.run(api.validate_api_token()))
        self.assertEqual(calls, [("meta.whoami", {}, 1)])


if __name__ == "__main__":
    unittest.main()
