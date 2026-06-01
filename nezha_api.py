import aiohttp
import asyncio
import logging

class NezhaAPI:
    def __init__(self, dashboard_url, username=None, password=None, auth_type="password", api_token=None, timeout=30):
        self.base_url = dashboard_url.rstrip('/') + '/api/v1'
        self.dashboard_url = dashboard_url.rstrip('/')
        self.username = username
        self.password = password
        self.auth_type = auth_type or "password"
        self.api_token = api_token
        self.token = api_token if self.auth_type == "token" else None
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session = None
        self.lock = asyncio.Lock()

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def authenticate(self):
        if self.auth_type == "token":
            if not self.api_token:
                raise Exception('API Token 未配置。')
            self.token = self.api_token
            return
        async with self.lock:
            if self.token is not None:
                return
            login_url = f'{self.base_url}/login'
            payload = {
                'username': self.username,
                'password': self.password
            }
            session = await self.get_session()
            async with session.post(login_url, json=payload) as resp:
                data = await resp.json(content_type=None)
                if data.get('success'):
                    self.token = data['data']['token']
                else:
                    raise Exception('认证失败，请检查用户名和密码。')

    async def request(self, method, endpoint, **kwargs):
        await self.authenticate()
        url = f'{self.base_url}{endpoint}'
        headers = kwargs.get('headers', {})
        headers['Authorization'] = f'Bearer {self.token}'
        kwargs['headers'] = headers

        session = await self.get_session()
        async with session.request(method, url, **kwargs) as resp:
            if resp.status == 401 and self.auth_type == "password":
                self.token = None
                return await self.request(method, endpoint, **kwargs)
            elif resp.status == 200:
                return await resp.json(content_type=None)
            elif resp.status in (401, 403):
                data = await resp.json(content_type=None)
                message = data.get('error') or data.get('message') or '认证失败或权限不足。'
                raise Exception(message)
            else:
                logging.error('API 请求失败：%s %s', resp.status, endpoint)
                return None

    async def validate_auth(self):
        data = await self.get_servers()
        return bool(data and data.get('success'))

    async def get_overview(self):
        data = await self.request('GET', '/server')
        return data

    async def get_services(self):
        data = await self.request('GET', '/service')
        return data

    async def get_servers(self):
        data = await self.request('GET', '/server')
        return data

    async def get_server_groups(self):
        data = await self.request('GET', '/server-group')
        return data

    async def get_cron_jobs(self):
        data = await self.request('GET', '/cron')
        return data

    async def run_cron_job(self, cron_id):
        endpoint = f'/cron/{cron_id}/manual'
        data = await self.request('POST', endpoint)
        if data is None:
            data = await self.request('GET', endpoint)
        return data

    async def mcp_call(self, tool_name, arguments=None, request_id=1):
        if not self.api_token:
            raise Exception('MCP 需要 API Token 认证，请先在面板设置中绑定 nzp_ Token。')
        url = f'{self.dashboard_url}/mcp'
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments or {}
            }
        }
        headers = {"Authorization": f"Bearer {self.api_token}"}
        session = await self.get_session()
        async with session.post(url, json=payload, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise Exception(data.get("error", {}).get("message") or f"MCP 请求失败：HTTP {resp.status}")
            if data.get("error"):
                raise Exception(data["error"].get("message", "MCP 调用失败"))
            result = data.get("result") or {}
            if result.get("isError"):
                text = ""
                content = result.get("content") or []
                if content:
                    text = content[0].get("text", "")
                raise Exception(text or "MCP 工具执行失败")
            return result

    async def execute_command_mcp(self, server_id, command, timeout_seconds=60):
        return await self.mcp_call(
            "server.exec",
            {
                "server_id": int(server_id),
                "cmd": "sh",
                "args": ["-c", command],
                "timeout_seconds": min(max(int(timeout_seconds), 1), 300),
                "max_output_bytes": 65536,
            },
        )

    async def search_servers(self, query):
        servers = await self.get_servers()
        if servers and servers.get('success'):
            result = []
            for server in servers['data']:
                if query.lower() in server['name'].lower():
                    result.append(server)
            return result
        return []

    async def get_server_detail(self, server_id):
        servers = await self.get_servers()
        if servers and servers.get('success'):
            for server in servers['data']:
                if server['id'] == server_id:
                    return server
        return None

    async def get_services_status(self):
        data = await self.request('GET', '/service')
        return data

    async def get_service_histories(self, server_id):
        endpoint = f'/service/{server_id}'
        data = await self.request('GET', endpoint)
        return data

    async def get_alert_rules(self):
        data = await self.request('GET', '/alert-rule')
        return data
