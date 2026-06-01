import unittest
import asyncio

from llm_assistant import (
    AssistantRuntime,
    PendingExecution,
    deserialize_pending_execution,
    heuristic_batch_plan,
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

            async def prepare_batch_command(self, command, query=None, group_name=None, status="online"):
                calls.append(
                    {
                        "command": command,
                        "query": query,
                        "group_name": group_name,
                        "status": status,
                    }
                )
                return {"requires_confirmation": True}

        asyncio.run(heuristic_batch_plan(Runtime(), "在 hk 分组执行 `apt-get update`"))

        self.assertEqual(calls[0]["command"], "apt-get update")
        self.assertEqual(calls[0]["group_name"], "hk")
        self.assertEqual(calls[0]["status"], "online")


if __name__ == "__main__":
    unittest.main()
