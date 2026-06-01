import unittest
import asyncio

from llm_assistant import (
    AssistantRuntime,
    OpenAICompatibleClient,
    PendingExecution,
    SYSTEM_PROMPT,
    TOOLS,
    deserialize_pending_execution,
    heuristic_batch_plan,
    load_pending_execution_for_confirmation,
    redact_secret,
    serialize_pending_execution,
)


class LLMAssistantTests(unittest.TestCase):
    def test_pending_execution_round_trips_as_json(self):
        pending = PendingExecution(
            owner_telegram_id=42,
            dashboard_id=7,
            dashboard_alias="MAIN",
            command="apt-get update",
            server_ids=[1, 2],
            server_names=["hk-1", "hk-2"],
            source="group:hk",
        )

        restored = deserialize_pending_execution(serialize_pending_execution(pending))

        self.assertEqual(restored.owner_telegram_id, 42)
        self.assertEqual(restored.server_ids, [1, 2])
        self.assertEqual(restored.command, "apt-get update")

    def test_redact_secret_keeps_prefix_and_suffix_only(self):
        self.assertEqual(redact_secret("nzp_abcdefghijklmnopqrstuvwxyz"), "nzp_...wxyz")

    def test_prepare_batch_command_requires_parallel_api_token(self):
        class NoReadAPI:
            async def get_servers(self):
                raise AssertionError("server reads should not run without api_token")

        runtime = AssistantRuntime(
            database=None,
            telegram_id=42,
            dashboard={
                "id": 7,
                "alias": "MAIN",
                "auth_type": "password",
                "api_token": None,
            },
            api=NoReadAPI(),
        )

        result = asyncio.run(runtime.prepare_batch_command("uptime", query="hk"))

        self.assertIn("API Token", result["error"])
        self.assertIsNone(runtime.pending_execution)

    def test_heuristic_batch_plan_understands_name_before_group_word(self):
        calls = []

        class Runtime:
            pending_execution = None

            async def prepare_batch_command(
                self, command, query=None, group_name=None, status="online", **kwargs
            ):
                calls.append(
                    {
                        "command": command,
                        "query": query,
                        "group_name": group_name,
                        "status": status,
                        **kwargs,
                    }
                )
                return {"requires_confirmation": True}

        asyncio.run(heuristic_batch_plan(Runtime(), "在 hk 分组执行 `apt-get update`"))

        self.assertEqual(calls[0]["command"], "apt-get update")
        self.assertEqual(calls[0]["group_name"], "hk")
        self.assertEqual(calls[0]["status"], "online")

    def test_prepare_batch_command_targets_exact_server_ids(self):
        class API:
            async def get_servers(self):
                return {
                    "success": True,
                    "data": [
                        {"id": 11, "name": "hk-web-1", "last_active": "2026-06-01T08:00:00Z"},
                        {"id": 12, "name": "hk-db-1", "last_active": "2026-06-01T08:00:00Z"},
                    ],
                }

            async def get_server_groups(self):
                return {"success": True, "data": []}

        runtime = AssistantRuntime(
            database=None,
            telegram_id=42,
            dashboard={"id": 7, "alias": "MAIN", "api_token": "nzp_secret"},
            api=API(),
        )

        result = asyncio.run(
            runtime.prepare_batch_command("uptime", server_ids=[12], status="all")
        )

        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(runtime.pending_execution.server_ids, [12])
        self.assertEqual(runtime.pending_execution.server_names, ["hk-db-1"])
        self.assertEqual(runtime.pending_execution.match_summary, "ID 12 -> hk-db-1")

    def test_prepare_batch_command_rejects_ambiguous_server_name(self):
        class API:
            async def get_servers(self):
                return {
                    "success": True,
                    "data": [
                        {"id": 11, "name": "hk-web-1", "last_active": "2026-06-01T08:00:00Z"},
                        {"id": 12, "name": "hk-web-2", "last_active": "2026-06-01T08:00:00Z"},
                    ],
                }

            async def get_server_groups(self):
                return {"success": True, "data": []}

        runtime = AssistantRuntime(
            database=None,
            telegram_id=42,
            dashboard={"id": 7, "alias": "MAIN", "api_token": "nzp_secret"},
            api=API(),
        )

        result = asyncio.run(
            runtime.prepare_batch_command("uptime", server_names=["hk-web"], status="all")
        )

        self.assertIn("目标不唯一", result["error"])
        self.assertIsNone(runtime.pending_execution)

    def test_search_servers_reports_online_from_last_active(self):
        from datetime import datetime, timedelta, timezone

        class API:
            async def get_servers(self):
                return {
                    "success": True,
                    "data": [
                        {
                            "id": 11,
                            "name": "hk-web-1",
                            "last_active": (
                                datetime.now(timezone.utc) - timedelta(seconds=2)
                            ).isoformat(),
                        }
                    ],
                }

            async def get_server_groups(self):
                return {"success": True, "data": []}

        runtime = AssistantRuntime(
            database=None,
            telegram_id=42,
            dashboard={"id": 7, "alias": "MAIN", "api_token": "nzp_secret"},
            api=API(),
        )

        result = asyncio.run(runtime.search_servers(status="all"))

        self.assertEqual(result["online_count"], 1)
        self.assertEqual(result["offline_count"], 0)
        self.assertIs(result["servers"][0]["online"], True)
        self.assertEqual(result["servers"][0]["status"], "online")

    def test_system_prompt_and_tool_schema_constrain_status_accuracy(self):
        tool_text = str(TOOLS)

        self.assertIn("不要推断服务器在线状态", SYSTEM_PROMPT)
        self.assertIn("online_count", tool_text)
        self.assertIn("offline_count", tool_text)

    def test_pending_confirmation_rejects_wrong_user_or_dashboard(self):
        pending = PendingExecution(
            owner_telegram_id=42,
            dashboard_id=7,
            dashboard_alias="MAIN",
            command="uptime",
            server_ids=[11],
            server_names=["hk-web-1"],
            source="explicit-targets",
        )

        class DB:
            async def get_pending_execution(self, execution_id):
                return {
                    "id": execution_id,
                    "telegram_id": 42,
                    "dashboard_id": 7,
                    "payload": serialize_pending_execution(pending),
                    "created_at": pending.created_at,
                }

        ok = asyncio.run(load_pending_execution_for_confirmation(DB(), "abc", 42, 7))
        wrong_user = asyncio.run(
            load_pending_execution_for_confirmation(DB(), "abc", 99, 7)
        )
        wrong_dashboard = asyncio.run(
            load_pending_execution_for_confirmation(DB(), "abc", 42, 8)
        )

        self.assertIsInstance(ok["pending"], PendingExecution)
        self.assertIn("无权限", wrong_user["error"])
        self.assertIn("面板", wrong_dashboard["error"])

    def test_responses_payload_converts_chat_tools_and_tool_outputs(self):
        client = OpenAICompatibleClient(
            "https://api.openai.com/v1", "sk-test", "gpt-test", api_mode="responses"
        )
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "list servers"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "search_servers", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "search_servers",
                "content": '{"count":1}',
            },
        ]

        payload = client.build_responses_payload(messages, TOOLS)

        self.assertEqual(payload["model"], "gpt-test")
        self.assertEqual(payload["tools"][0]["type"], "function")
        self.assertIn("name", payload["tools"][0])
        self.assertEqual(payload["input"][2]["type"], "function_call")
        self.assertEqual(payload["input"][3]["type"], "function_call_output")

    def test_streaming_responses_payload_sets_stream_true(self):
        client = OpenAICompatibleClient(
            "https://api.openai.com/v1", "sk-test", "gpt-test", api_mode="responses_stream"
        )

        payload = client.build_responses_payload([{"role": "user", "content": "hi"}], TOOLS)

        self.assertTrue(payload["stream"])

    def test_responses_output_normalizes_to_chat_message_shape(self):
        client = OpenAICompatibleClient(
            "https://api.openai.com/v1", "sk-test", "gpt-test", api_mode="responses"
        )

        message = client.parse_responses_message(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "search_servers",
                        "arguments": '{"query":"hk"}',
                    },
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "ok"}],
                    },
                ]
            }
        )

        self.assertEqual(message["content"], "ok")
        self.assertEqual(message["tool_calls"][0]["id"], "call_1")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "search_servers")

    def test_responses_stream_events_normalize_to_chat_message_shape(self):
        client = OpenAICompatibleClient(
            "https://api.openai.com/v1", "sk-test", "gpt-test", api_mode="responses_stream"
        )

        message = client.parse_responses_stream_events(
            [
                {"type": "response.output_text.delta", "delta": "ok"},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "search_servers",
                        "arguments": '{"query":"hk"}',
                    },
                },
            ]
        )

        self.assertEqual(message["content"], "ok")
        self.assertEqual(message["tool_calls"][0]["id"], "call_1")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "search_servers")
        self.assertEqual(message["tool_calls"][0]["function"]["arguments"], '{"query":"hk"}')

    def test_responses_retries_with_stream_when_required(self):
        client = OpenAICompatibleClient(
            "https://api.openai.com/v1", "sk-test", "gpt-test", api_mode="auto"
        )
        posts = []

        class Response:
            def __init__(self, status, payload=None, lines=None):
                self.status = status
                self.payload = payload
                self.content = lines or []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def json(self, content_type=None):
                return self.payload

        class Session:
            def post(self, url, json, headers):
                posts.append(json)
                if len(posts) == 1:
                    return Response(
                        400,
                        {"error": {"message": "stream=true required for this endpoint"}},
                    )
                return Response(
                    200,
                    lines=[
                        b'data: {"type":"response.output_text.delta","delta":"ok"}\n',
                        b"data: [DONE]\n",
                    ],
                )

        message = asyncio.run(
            client.post_responses(
                Session(),
                {"Authorization": "Bearer sk-test"},
                [{"role": "user", "content": "hi"}],
                TOOLS,
            )
        )

        self.assertNotIn("stream", posts[0])
        self.assertTrue(posts[1]["stream"])
        self.assertEqual(message["content"], "ok")

    def test_responses_endpoint_base_url_is_normalized(self):
        client = OpenAICompatibleClient(
            "https://api.openai.com/v1/responses", "sk-test", "gpt-test"
        )

        self.assertEqual(client.base_url, "https://api.openai.com/v1")
        self.assertEqual(client.api_mode, "responses")


if __name__ == "__main__":
    unittest.main()
