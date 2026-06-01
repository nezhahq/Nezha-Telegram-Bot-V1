import json
import re
import time
from dataclasses import asdict, dataclass

from server_utils import (
    attach_group_ids,
    filter_servers,
    server_is_online,
    sort_servers_for_display,
)


HISTORY_LIMIT = 12

SYSTEM_PROMPT = (
    "你是 Nezha Telegram Bot 的运维助手。你可以查询服务器和分组。"
    "不要推断服务器在线状态；必须使用 search_servers 返回的 explicit status、online、"
    "online_count、offline_count 字段说明在线/离线。如果字段缺失或不一致，就说未知，"
    "不要把未知说成离线。用户提到具体服务器 ID 或完整服务器名时，先用实时服务器列表确认目标，"
    "再把明确的 server_ids 或 server_names 传给 prepare_batch_command。"
    "不要把不明确的简称扩大成所有服务器。任何执行命令都只能调用 prepare_batch_command 准备预览，"
    "绝不能直接执行。准备命令前要总结目标依据，确认目标来自哪个面板、哪些 ID/名称或哪个分组，"
    "然后交给 Telegram 二次确认。优先用中文简洁回答。"
)


def redact_secret(value):
    if not value:
        return "未配置"
    if len(value) <= 8:
        return "***"
    if value.startswith("nzp_"):
        return f"{value[:4]}...{value[-4:]}"
    return f"{value[:3]}...{value[-4:]}"


@dataclass
class PendingExecution:
    owner_telegram_id: int
    dashboard_id: int
    dashboard_alias: str
    command: str
    server_ids: list
    server_names: list
    source: str
    match_summary: str = ""
    created_at: int = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = int(time.time())


def serialize_pending_execution(pending):
    return json.dumps(asdict(pending), ensure_ascii=False, separators=(",", ":"))


def deserialize_pending_execution(payload):
    data = json.loads(payload)
    return PendingExecution(**data)


def normalize_server_summary(server):
    online = server_is_online(server)
    return {
        "id": server.get("id"),
        "name": server.get("name"),
        "online": online,
        "status": "online" if online else "offline",
        "last_active": server.get("last_active"),
    }


def bounded_history(messages, limit=HISTORY_LIMIT):
    cleaned = []
    for message in messages or []:
        role = message.get("role")
        content = message.get("content")
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            cleaned.append({"role": role, "content": content.strip()[:4000]})
    return cleaned[-limit:]


async def load_pending_execution_for_confirmation(
    database, execution_id, telegram_id, dashboard_id=None
):
    row = await database.get_pending_execution(execution_id)
    if not row:
        return {"error": "确认已过期或不存在。"}
    if row["telegram_id"] != telegram_id:
        return {"error": "确认已过期或无权限。"}
    pending = deserialize_pending_execution(row["payload"])
    if pending.owner_telegram_id != telegram_id:
        return {"error": "确认已过期或无权限。"}
    if row["dashboard_id"] != pending.dashboard_id:
        return {"error": "确认记录面板不一致，已拒绝执行。"}
    if dashboard_id is not None and int(dashboard_id) != int(pending.dashboard_id):
        return {"error": "确认面板不匹配，已拒绝执行。"}
    return {"row": row, "pending": pending}


class OpenAICompatibleClient:
    def __init__(self, base_url, api_key, model, timeout=60, api_mode="auto"):
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        api_mode = (api_mode or "auto").lower()
        if self.base_url.endswith("/responses"):
            self.base_url = self.base_url[: -len("/responses")]
            if api_mode == "auto":
                api_mode = "responses"
        elif self.base_url.endswith("/chat/completions"):
            self.base_url = self.base_url[: -len("/chat/completions")]
            if api_mode == "auto":
                api_mode = "chat"
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout
        self.api_mode = api_mode if api_mode in {"auto", "chat", "responses", "responses_stream"} else "auto"

    async def chat(self, messages, tools):
        import aiohttp

        headers = {"Authorization": f"Bearer {self.api_key}"}
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            return await self.chat_with_session(session, headers, messages, tools)

    async def chat_with_session(self, session, headers, messages, tools):
        if self.api_mode in {"responses", "responses_stream"}:
            return await self.post_responses(
                session,
                headers,
                messages,
                tools,
                stream=self.api_mode == "responses_stream",
            )
        if self.api_mode == "auto" and self.prefers_responses_endpoint():
            return await self.post_responses(session, headers, messages, tools)
        try:
            return await self.post_chat_completions(session, headers, messages, tools)
        except RuntimeError as exc:
            if self.api_mode == "chat" or not self.should_retry_with_responses(exc):
                raise
            return await self.post_responses(session, headers, messages, tools)

    def prefers_responses_endpoint(self):
        text = f"{self.base_url} {self.model}".lower()
        compact = re.sub(r"[\s_\-]+", " ", text)
        return "codex" in compact or "codex channel" in compact

    async def post_chat_completions(self, session, headers, messages, tools):
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        async with session.post(
            f"{self.base_url}/chat/completions", json=payload, headers=headers
        ) as resp:
            data = await self.read_json_response(resp)
            if data is None:
                detail = await self.read_response_text(resp)
                message = "chat/completions returned empty or non-JSON response"
                if detail:
                    message = f"{message}: {detail[:200]}"
                raise RuntimeError(message)
            if resp.status >= 400:
                message = data.get("error", {}).get("message") if isinstance(data, dict) else None
                raise RuntimeError(message or f"LLM 请求失败: HTTP {resp.status}")
            return data["choices"][0]["message"]

    async def post_responses(self, session, headers, messages, tools, stream=None):
        stream = self.api_mode == "responses_stream" if stream is None else stream
        payload = self.build_responses_payload(messages, tools, stream=stream)
        async with session.post(
            f"{self.base_url}/responses", json=payload, headers=headers
        ) as resp:
            if stream and resp.status < 400:
                return await self.parse_responses_stream(resp)
            data = await self.read_json_response(resp)
            if resp.status >= 400:
                message = data.get("error", {}).get("message") if isinstance(data, dict) else None
                if not stream and self.should_retry_with_streaming_responses(message):
                    return await self.post_responses(
                        session, headers, messages, tools, stream=True
                    )
                raise RuntimeError(message or f"LLM Responses 请求失败: HTTP {resp.status}")
            if data is None:
                detail = await self.read_response_text(resp)
                message = "LLM Responses returned empty or non-JSON response"
                if detail:
                    message = f"{message}: {detail[:200]}"
                raise RuntimeError(message)
            return self.parse_responses_message(data)

    async def read_json_response(self, resp):
        try:
            return await resp.json(content_type=None)
        except (json.JSONDecodeError, ValueError):
            return None

    async def read_response_text(self, resp):
        text_reader = getattr(resp, "text", None)
        if not text_reader:
            return ""
        try:
            return await text_reader()
        except Exception:
            return ""

    def should_retry_with_responses(self, exc):
        text = str(exc).lower()
        return (
            "chat/completions" in text
            or "chat completions" in text
            or "endpoint not supported" in text
            or "/v1/chat/completions" in text
            or "non-json" in text
            or "empty" in text
            or "expecting value" in text
            or "codex channel" in text
        )

    def should_retry_with_streaming_responses(self, message):
        text = str(message or "").lower()
        compact = re.sub(r"\s+", " ", text)
        return (
            "stream required" in compact
            or "stream=true required" in compact
            or "streaming required" in compact
            or ("stream" in compact and "required" in compact)
        )

    def build_responses_payload(self, messages, tools, stream=None):
        stream = self.api_mode == "responses_stream" if stream is None else stream
        payload = {
            "model": self.model,
            "input": self.convert_messages_to_responses_input(messages),
            "tools": self.convert_tools_to_responses(tools),
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        if stream:
            payload["stream"] = True
        return payload

    def convert_tools_to_responses(self, tools):
        response_tools = []
        for tool in tools:
            function = tool.get("function") or {}
            response_tools.append(
                {
                    "type": "function",
                    "name": function.get("name"),
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {"type": "object", "properties": {}}),
                }
            )
        return response_tools

    def convert_messages_to_responses_input(self, messages):
        items = []
        for message in messages:
            role = message.get("role")
            if role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.get("tool_call_id"),
                        "output": message.get("content") or "",
                    }
                )
                continue
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                for call in tool_calls:
                    function = call.get("function") or {}
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call.get("id"),
                            "name": function.get("name"),
                            "arguments": function.get("arguments") or "{}",
                        }
                    )
                continue
            content = message.get("content")
            if content:
                items.append({"role": role, "content": content})
        return items

    def parse_responses_message(self, data):
        output = data.get("output") or []
        tool_calls = []
        text_parts = self.extract_responses_text_parts(data)
        for item in output:
            if item.get("type") == "function_call":
                tool_calls.append(self.normalize_responses_tool_call(item))
        return {
            "role": "assistant",
            "content": "\n".join(part for part in text_parts if part),
            "tool_calls": tool_calls,
        }

    def extract_responses_text_parts(self, value):
        if not isinstance(value, dict):
            return []
        text_parts = []
        output_text = value.get("output_text")
        if isinstance(output_text, str) and output_text:
            text_parts.append(output_text)
        output = value.get("output")
        if isinstance(output, list):
            for item in output:
                text_parts.extend(self.extract_responses_text_parts(item))
        content = value.get("content")
        if isinstance(content, list):
            for part in content:
                text_parts.extend(self.extract_responses_text_parts(part))
        elif isinstance(content, str) and content:
            text_parts.append(content)
        if value.get("type") in {"output_text", "text", "input_text"}:
            text = value.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
        text = value.get("text")
        if not text_parts and isinstance(text, str) and text:
            text_parts.append(text)
        return text_parts

    def extract_responses_stream_text_parts(self, event):
        text_parts = []
        for key in ("delta", "text", "output_text"):
            value = event.get(key)
            if isinstance(value, str) and value:
                text_parts.append(value)
            elif isinstance(value, dict):
                text_parts.extend(self.extract_responses_text_parts(value))
        for key in ("part", "content_part", "item", "message", "response"):
            value = event.get(key)
            if isinstance(value, dict):
                text_parts.extend(self.extract_responses_text_parts(value))
        output = event.get("output")
        if isinstance(output, list):
            text_parts.extend(self.extract_responses_text_parts({"output": output}))
        return text_parts

    def normalize_responses_tool_call(self, item):
        return {
            "id": item.get("call_id") or item.get("id"),
            "type": "function",
            "function": {
                "name": item.get("name"),
                "arguments": item.get("arguments") or "{}",
            },
        }

    async def parse_responses_stream(self, resp):
        events = []
        buffer = []
        if hasattr(resp.content, "__aiter__"):
            async for raw_line in resp.content:
                self._consume_responses_stream_line(events, buffer, raw_line)
        else:
            for raw_line in resp.content:
                self._consume_responses_stream_line(events, buffer, raw_line)
        if buffer:
            self._append_responses_stream_event(events, "\n".join(buffer))
        return self.parse_responses_stream_events(events)

    def _consume_responses_stream_line(self, events, buffer, raw_line):
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        for part in line.splitlines():
            part = part.strip()
            if not part:
                if buffer:
                    self._append_responses_stream_event(events, "\n".join(buffer))
                    buffer.clear()
                continue
            if part.startswith("data:"):
                buffer.append(part[5:].strip())
        if buffer and line.endswith("\n"):
            self._append_responses_stream_event(events, "\n".join(buffer))
            buffer.clear()

    def _append_responses_stream_event(self, events, payload):
        if not payload or payload == "[DONE]":
            return
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            return

    def parse_responses_stream_events(self, events):
        tool_calls = []
        text_parts = []
        final_message = None
        seen_event_types = []
        for event in events:
            event_type = event.get("type")
            if event_type:
                seen_event_types.append(event_type)
            if event_type == "response.output_text.delta":
                text_parts.extend(self.extract_responses_stream_text_parts(event))
                continue
            if event_type == "response.output_text.done":
                done_text = self.extract_responses_stream_text_parts(event)
                if done_text:
                    text_parts = done_text
                continue
            if event_type == "response.content_part.added":
                text_parts.extend(self.extract_responses_stream_text_parts(event))
                continue
            if event_type == "response.output_item.done":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    tool_calls.append(self.normalize_responses_tool_call(item))
                else:
                    text_parts.extend(self.extract_responses_text_parts(item))
                continue
            if event_type == "response.completed":
                response = event.get("response") or {}
                final_message = self.parse_responses_message(response)
                continue
            if event_type in {"response.error", "error"}:
                error = event.get("error") or event
                message = error.get("message") if isinstance(error, dict) else None
                raise RuntimeError(message or "LLM Responses streaming failed")
        if final_message:
            final_tool_calls = final_message.get("tool_calls") or []
            final_call_ids = {call.get("id") for call in final_tool_calls}
            for call in tool_calls:
                if call.get("id") not in final_call_ids:
                    final_tool_calls.append(call)
            final_message["tool_calls"] = final_tool_calls
            if final_message.get("content") or final_message.get("tool_calls"):
                return final_message
        return {
            "role": "assistant",
            "content": "".join(part for part in text_parts if part),
            "tool_calls": tool_calls,
            "debug": f"events={','.join(seen_event_types[-8:])}" if seen_event_types else "events=none",
        }


class AssistantRuntime:
    def __init__(self, database, telegram_id, dashboard, api):
        self.database = database
        self.telegram_id = telegram_id
        self.dashboard = dashboard
        self.api = api
        self.pending_execution = None

    async def list_dashboard_context(self):
        dashboards = await self.database.get_all_dashboards(self.telegram_id)
        return {
            "current_dashboard_id": self.dashboard["id"],
            "dashboards": [
                {
                    "id": d["id"],
                    "alias": d.get("alias"),
                    "url": d.get("dashboard_url"),
                    "auth_type": d.get("auth_type", "password"),
                    "is_default": d.get("is_default", False),
                }
                for d in dashboards
            ],
        }

    async def list_server_groups(self):
        data = await self.api.get_server_groups()
        if data and data.get("success"):
            return {"groups": data.get("data") or []}
        return {"groups": [], "warning": "无法读取服务器分组，可能缺少 inventory:read 权限。"}

    async def search_servers(self, query=None, group_name=None, status="all", high_load=False, high_traffic=False):
        servers = await self._servers_with_groups()
        group_id = None
        source = query or "all"
        if group_name:
            groups = await self.list_server_groups()
            for item in groups.get("groups", []):
                group = item.get("group") or {}
                if group_name.lower() in str(group.get("name", "")).lower():
                    group_id = group.get("id")
                    source = f"group:{group.get('name')}"
                    break
        result = filter_servers(
            servers,
            query=query,
            status=status,
            group_id=group_id,
            high_load=high_load,
            high_traffic=high_traffic,
        )
        normalized = [normalize_server_summary(s) for s in result]
        return {
            "servers": normalized,
            "count": len(result),
            "online_count": sum(1 for s in normalized if s["online"]),
            "offline_count": sum(1 for s in normalized if not s["online"]),
            "source": source,
            "status_basis": "last_active within threshold",
        }

    async def get_server_detail(self, server_id):
        return await self.api.get_server_detail(int(server_id))

    async def prepare_batch_command(
        self,
        command,
        query=None,
        group_name=None,
        status="online",
        server_ids=None,
        server_names=None,
    ):
        if not command or not str(command).strip():
            return {"error": "命令为空，无法准备执行。"}
        if not self.dashboard.get("api_token"):
            return {"error": "MCP 命令执行需要 API Token，请先在面板设置中绑定 nzp_ Token。"}
        if server_ids or server_names:
            result = await self.resolve_server_targets(
                server_ids=server_ids, server_names=server_names
            )
            if result.get("error"):
                return result
        else:
            result = await self.search_servers(
                query=query, group_name=group_name, status=status or "online"
            )
        servers = result.get("servers") or []
        if not servers:
            return {"error": "没有找到匹配的服务器。"}
        pending = PendingExecution(
            owner_telegram_id=self.telegram_id,
            dashboard_id=self.dashboard["id"],
            dashboard_alias=self.dashboard.get("alias") or "NEZHA",
            command=str(command).strip(),
            server_ids=[int(s["id"]) for s in servers],
            server_names=[str(s["name"]) for s in servers],
            source=result.get("source") or query or group_name or "selected",
            match_summary=result.get("match_summary") or "",
        )
        self.pending_execution = pending
        return {
            "requires_confirmation": True,
            "dashboard": pending.dashboard_alias,
            "server_count": len(pending.server_ids),
            "server_names": pending.server_names[:20],
            "command": pending.command,
            "source": pending.source,
            "match_summary": pending.match_summary,
        }

    async def resolve_server_targets(self, server_ids=None, server_names=None):
        servers = await self._servers_with_groups()
        by_id = {int(s.get("id")): s for s in servers if s.get("id") is not None}
        selected = {}
        summaries = []

        for raw_id in server_ids or []:
            try:
                server_id = int(raw_id)
            except (TypeError, ValueError):
                return {"error": f"服务器 ID 无效：{raw_id}"}
            server = by_id.get(server_id)
            if not server:
                return {"error": f"没有找到服务器 ID：{server_id}"}
            selected[server_id] = server
            summaries.append(f"ID {server_id} -> {server.get('name')}")

        for raw_name in server_names or []:
            name = str(raw_name or "").strip()
            if not name:
                continue
            exact = [
                s for s in servers if str(s.get("name", "")).lower() == name.lower()
            ]
            matches = exact or [
                s for s in servers if name.lower() in str(s.get("name", "")).lower()
            ]
            if not matches:
                return {"error": f"没有找到服务器名称：{name}"}
            if len(matches) > 1:
                names = ", ".join(str(s.get("name")) for s in matches[:8])
                more = "" if len(matches) <= 8 else f" 等 {len(matches)} 台"
                return {
                    "error": f"目标不唯一：{name} 匹配 {names}{more}。请使用服务器 ID 或完整名称。"
                }
            server = matches[0]
            server_id = int(server["id"])
            selected[server_id] = server
            summaries.append(f"{name} -> {server.get('name')} (ID {server_id})")

        return {
            "servers": [normalize_server_summary(s) for s in selected.values()],
            "count": len(selected),
            "source": "explicit-targets",
            "match_summary": "\n".join(summaries),
        }

    async def _servers_with_groups(self):
        servers_data = await self.api.get_servers()
        servers = servers_data.get("data") if servers_data and servers_data.get("success") else []
        groups_data = await self.api.get_server_groups()
        groups = groups_data.get("data") if groups_data and groups_data.get("success") else []
        return sort_servers_for_display(attach_group_ids(servers, groups))


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dashboard_context",
            "description": "List dashboards bound by the Telegram user and the current dashboard.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_server_groups",
            "description": "List server groups and their server IDs for the current Nezha dashboard.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_servers",
            "description": (
                "Search/filter servers and return explicit server status. Use online, status, "
                "online_count, and offline_count exactly; do not infer status from missing fields."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "group_name": {"type": "string"},
                    "status": {"type": "string", "enum": ["all", "online", "offline"]},
                    "high_load": {"type": "boolean"},
                    "high_traffic": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_server_detail",
            "description": "Get full detail for one server by ID.",
            "parameters": {
                "type": "object",
                "properties": {"server_id": {"type": "integer"}},
                "required": ["server_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prepare_batch_command",
            "description": "Prepare a batch command execution preview. This never executes; Telegram confirmation is required. Prefer explicit server_ids or exact server_names when the user names specific servers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "query": {"type": "string"},
                    "group_name": {"type": "string"},
                    "server_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "server_names": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "status": {"type": "string", "enum": ["all", "online", "offline"]},
                },
                "required": ["command"],
            },
        },
    },
]


async def run_assistant_message(
    database,
    telegram_id,
    dashboard,
    api,
    llm_config,
    user_text,
    chat_id=None,
    message_thread_id=0,
):
    runtime = AssistantRuntime(database, telegram_id, dashboard, api)
    chat_id = int(chat_id if chat_id is not None else telegram_id)
    message_thread_id = int(message_thread_id or 0)
    if not llm_config or not llm_config.get("api_key"):
        result = await heuristic_batch_plan(runtime, user_text)
        if isinstance(result, PendingExecution):
            return {"pending_execution": result}
        if isinstance(result, dict) and result.get("error"):
            return {"text": result["error"]}
        return {"text": "请先在 /dashboard 设置页配置 LLM API Key，或明确写出要执行的分组和命令。"}

    client = OpenAICompatibleClient(
        llm_config.get("base_url"),
        llm_config.get("api_key"),
        llm_config.get("model") or "gpt-4o-mini",
        api_mode=llm_config.get("provider") or "auto",
    )
    history = bounded_history(
        await database.get_llm_history(
            telegram_id, chat_id, message_thread_id, dashboard["id"]
        )
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
    messages.append({"role": "user", "content": user_text})
    for _ in range(5):
        message = await client.chat(messages, TOOLS)
        calls = message.get("tool_calls") or []
        if not calls:
            text = message.get("content")
            if not text:
                debug = message.get("debug")
                debug_suffix = f", {debug}" if debug else ""
                text = (
                    "没有可展示的回复。"
                    f" endpoint={client.api_mode}, tool_calls={len(calls)}{debug_suffix}"
                )
            await database.save_llm_history(
                telegram_id,
                chat_id,
                message_thread_id,
                dashboard["id"],
                bounded_history(
                    [*history, {"role": "user", "content": user_text}, {"role": "assistant", "content": text}]
                ),
            )
            return {"text": text}
        messages.append(message)
        for call in calls:
            name = call.get("function", {}).get("name")
            arguments = call.get("function", {}).get("arguments") or "{}"
            args = json.loads(arguments)
            result = await call_runtime_tool(runtime, name, args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
            if runtime.pending_execution:
                await database.save_llm_history(
                    telegram_id,
                    chat_id,
                    message_thread_id,
                    dashboard["id"],
                    bounded_history(
                        [
                            *history,
                            {"role": "user", "content": user_text},
                            {
                                "role": "assistant",
                                "content": (
                                    f"已准备在 {runtime.pending_execution.dashboard_alias} "
                                    f"执行 {runtime.pending_execution.command}，等待确认。"
                                ),
                            },
                        ]
                    ),
                )
                return {"pending_execution": runtime.pending_execution}
    return {"text": "助手工具调用次数过多，请缩小目标范围后再试。"}


async def call_runtime_tool(runtime, name, args):
    if name == "list_dashboard_context":
        return await runtime.list_dashboard_context()
    if name == "list_server_groups":
        return await runtime.list_server_groups()
    if name == "search_servers":
        return await runtime.search_servers(**args)
    if name == "get_server_detail":
        return await runtime.get_server_detail(**args)
    if name == "prepare_batch_command":
        return await runtime.prepare_batch_command(**args)
    return {"error": f"unknown tool: {name}"}


async def heuristic_batch_plan(runtime, text):
    if not re.search(r"(执行|运行|安装|install|apt|yum|dnf|apk)", text, re.I):
        return None
    command = None
    quoted = re.search(r"[`“\"']([^`”\"']+)[`”\"']", text)
    if quoted:
        command = quoted.group(1)
    elif "apt" in text:
        command = text[text.find("apt") :].strip()
    elif "yum" in text:
        command = text[text.find("yum") :].strip()
    elif "dnf" in text:
        command = text[text.find("dnf") :].strip()
    elif "apk" in text:
        command = text[text.find("apk") :].strip()
    group_name = None
    server_ids = [int(item) for item in re.findall(r"(?:id|ID)\s*[:：#]?\s*(\d+)", text)]
    group_match = re.search(
        r"([A-Za-z0-9_\-\u4e00-\u9fff]+)\s*(?:分组|group)", text, re.I
    )
    if not group_match:
        group_match = re.search(
            r"(?:分组|group)\s*[:：]?\s*([A-Za-z0-9_\-\u4e00-\u9fff]+)", text, re.I
        )
    if group_match:
        group_name = group_match.group(1)
    if command:
        result = await runtime.prepare_batch_command(
            command, group_name=group_name, server_ids=server_ids, status="online"
        )
        return runtime.pending_execution or result
    return None
