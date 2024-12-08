import aiohttp
import asyncio
import logging

class NezhaAPI:
    def __init__(self, dashboard_url, username, password):
        self.base_url = dashboard_url.rstrip('/') + '/api/v1'
        self.username = username
        self.password = password
        self.token = None
        self.session = aiohttp.ClientSession()
        self.lock = asyncio.Lock()

    async def close(self):
        await self.session.close()

    async def authenticate(self):
        async with self.lock:
            if self.token is not None:
                return
            login_url = f'{self.base_url}/login'
            payload = {
                'username': self.username,
                'password': self.password
            }
            async with self.session.post(login_url, json=payload) as resp:
                data = await resp.json()
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

        async with self.session.request(method, url, **kwargs) as resp:
            if resp.status == 401:
                self.token = None
                return await self.request(method, endpoint, **kwargs)
            elif resp.status == 200:
                return await resp.json()
            else:
                logging.error(f'API 请求失败：{resp.status}')
                return None

    async def get_overview(self):
        data = await self.request('GET', '/server')
        return data

    async def get_services(self):
        data = await self.request('GET', '/service')
        return data

    async def get_servers(self):
        data = await self.request('GET', '/server')
        return data

    async def get_cron_jobs(self):
        data = await self.request('GET', '/cron')
        return data

    async def run_cron_job(self, cron_id):
        endpoint = f'/cron/{cron_id}/manual'
        data = await self.request('GET', endpoint)
        return data

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
