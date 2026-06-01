# database.py
from pathlib import Path
import aiosqlite

class Database:
    def __init__(self, db_path):
        self.db_path = db_path

    async def initialize(self):

        db_file = Path(self.db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        # 创建数据库目录

        async with aiosqlite.connect(self.db_path) as db:
            # 创建用户表
            await db.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    default_dashboard_id INTEGER,
                    FOREIGN KEY (default_dashboard_id) REFERENCES dashboards (id)
                )
            ''')
            # 创建 dashboard 表
            await db.execute('''
                CREATE TABLE IF NOT EXISTS dashboards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER,
                    username TEXT NOT NULL DEFAULT '',
                    password TEXT NOT NULL DEFAULT '',
                    dashboard_url TEXT NOT NULL,
                    alias TEXT,
                    auth_type TEXT NOT NULL DEFAULT 'password',
                    api_token TEXT,
                    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
                )
            ''')
            await self._migrate(db)
            await db.execute('''
                CREATE TABLE IF NOT EXISTS llm_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    dashboard_id INTEGER NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'openai',
                    base_url TEXT NOT NULL DEFAULT 'https://api.openai.com/v1',
                    model TEXT NOT NULL DEFAULT 'gpt-4o-mini',
                    api_key TEXT,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(telegram_id, dashboard_id),
                    FOREIGN KEY (dashboard_id) REFERENCES dashboards (id)
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS pending_executions (
                    id TEXT PRIMARY KEY,
                    telegram_id INTEGER NOT NULL,
                    dashboard_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (dashboard_id) REFERENCES dashboards (id)
                )
            ''')
            await db.commit()

    async def _migrate(self, db):
        async with db.execute("PRAGMA table_info(dashboards)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "auth_type" not in columns:
            await db.execute("ALTER TABLE dashboards ADD COLUMN auth_type TEXT NOT NULL DEFAULT 'password'")
        if "api_token" not in columns:
            await db.execute("ALTER TABLE dashboards ADD COLUMN api_token TEXT")

    async def add_user(self, telegram_id, username, password, dashboard_url, alias=None, auth_type="password", api_token=None):
        async with aiosqlite.connect(self.db_path) as db:
            # 首先确保用户存在
            await db.execute('INSERT OR IGNORE INTO users (telegram_id) VALUES (?)', (telegram_id,))
            
            # 如果没有提供别名，使用 URL 的第一部分作为默认别名
            if not alias:
                try:
                    alias = dashboard_url.split('://')[1].split('.')[0].upper()
                except:
                    alias = "NEZHA"
            
            # 添加 dashboard
            await db.execute('''
                INSERT INTO dashboards (telegram_id, username, password, dashboard_url, alias, auth_type, api_token)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (telegram_id, username or "", password or "", dashboard_url, alias, auth_type, api_token))
            
            # 获取最后插入的 dashboard_id
            cursor = await db.execute('SELECT last_insert_rowid()')
            dashboard_id = (await cursor.fetchone())[0]
            
            # 如果用户还没有默认 dashboard，设置这个为默认
            await db.execute('''
                UPDATE users 
                SET default_dashboard_id = COALESCE(default_dashboard_id, ?) 
                WHERE telegram_id = ?
            ''', (dashboard_id, telegram_id))
            
            await db.commit()
            return dashboard_id

    async def update_alias(self, dashboard_id, alias):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE dashboards 
                SET alias = ?
                WHERE id = ?
            ''', (alias, dashboard_id))
            await db.commit()

    async def get_user(self, telegram_id):
        async with aiosqlite.connect(self.db_path) as db:
            # 获取用户的默认 dashboard
            async with db.execute('''
                SELECT d.username, d.password, d.dashboard_url, d.alias
                     , d.auth_type, d.api_token, d.id
                FROM users u 
                JOIN dashboards d ON d.id = u.default_dashboard_id 
                WHERE u.telegram_id = ?
            ''', (telegram_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {'username': row[0], 'password': row[1], 'dashboard_url': row[2], 'alias': row[3], 'auth_type': row[4] or 'password', 'api_token': row[5], 'id': row[6]}
                return None

    async def get_all_dashboards(self, telegram_id):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('''
                SELECT d.id, d.username, d.password, d.dashboard_url, d.alias,
                       d.auth_type, d.api_token,
                       CASE WHEN u.default_dashboard_id = d.id THEN 1 ELSE 0 END as is_default
                FROM dashboards d
                LEFT JOIN users u ON u.telegram_id = d.telegram_id
                WHERE d.telegram_id = ?
            ''', (telegram_id,)) as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        'id': row[0],
                        'username': row[1],
                        'password': row[2],
                        'dashboard_url': row[3],
                        'alias': row[4],
                        'auth_type': row[5] or 'password',
                        'api_token': row[6],
                        'is_default': bool(row[7])
                    }
                    for row in rows
                ]

    async def get_dashboard(self, telegram_id, dashboard_id):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('''
                SELECT d.id, d.username, d.password, d.dashboard_url, d.alias,
                       d.auth_type, d.api_token,
                       CASE WHEN u.default_dashboard_id = d.id THEN 1 ELSE 0 END as is_default
                FROM dashboards d
                LEFT JOIN users u ON u.telegram_id = d.telegram_id
                WHERE d.telegram_id = ? AND d.id = ?
            ''', (telegram_id, dashboard_id)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return {
                    'id': row[0],
                    'username': row[1],
                    'password': row[2],
                    'dashboard_url': row[3],
                    'alias': row[4],
                    'auth_type': row[5] or 'password',
                    'api_token': row[6],
                    'is_default': bool(row[7])
                }

    async def update_dashboard_token(self, telegram_id, dashboard_id, api_token):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE dashboards
                SET api_token = ?
                WHERE telegram_id = ? AND id = ?
            ''', (api_token, telegram_id, dashboard_id))
            await db.commit()

    async def get_llm_config(self, telegram_id, dashboard_id):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('''
                SELECT provider, base_url, model, api_key, enabled
                FROM llm_configs
                WHERE telegram_id = ? AND dashboard_id = ?
            ''', (telegram_id, dashboard_id)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return {
                    'provider': row[0],
                    'base_url': row[1],
                    'model': row[2],
                    'api_key': row[3],
                    'enabled': bool(row[4]),
                }

    async def upsert_llm_config(self, telegram_id, dashboard_id, provider='openai', base_url='https://api.openai.com/v1', model='gpt-4o-mini', api_key=None, enabled=True):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT INTO llm_configs (telegram_id, dashboard_id, provider, base_url, model, api_key, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id, dashboard_id) DO UPDATE SET
                    provider=excluded.provider,
                    base_url=excluded.base_url,
                    model=excluded.model,
                    api_key=COALESCE(excluded.api_key, llm_configs.api_key),
                    enabled=excluded.enabled
            ''', (telegram_id, dashboard_id, provider, base_url, model, api_key, 1 if enabled else 0))
            await db.commit()

    async def save_pending_execution(self, execution_id, telegram_id, dashboard_id, payload, created_at):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT OR REPLACE INTO pending_executions (id, telegram_id, dashboard_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (execution_id, telegram_id, dashboard_id, payload, created_at))
            await db.commit()

    async def get_pending_execution(self, execution_id):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('''
                SELECT id, telegram_id, dashboard_id, payload, created_at
                FROM pending_executions
                WHERE id = ?
            ''', (execution_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return {
                    'id': row[0],
                    'telegram_id': row[1],
                    'dashboard_id': row[2],
                    'payload': row[3],
                    'created_at': row[4],
                }

    async def delete_pending_execution(self, execution_id):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('DELETE FROM pending_executions WHERE id = ?', (execution_id,))
            await db.commit()

    async def set_default_dashboard(self, telegram_id, dashboard_id):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE users 
                SET default_dashboard_id = ?
                WHERE telegram_id = ?
            ''', (dashboard_id, telegram_id))
            await db.commit()

    async def delete_dashboard(self, telegram_id, dashboard_id):
        async with aiosqlite.connect(self.db_path) as db:
            # 检查是否是默认面板
            async with db.execute('''
                SELECT default_dashboard_id 
                FROM users 
                WHERE telegram_id = ?
            ''', (telegram_id,)) as cursor:
                row = await cursor.fetchone()
                is_default = row and row[0] == dashboard_id

            # 删除 dashboard
            await db.execute('''
                DELETE FROM dashboards 
                WHERE id = ? AND telegram_id = ?
            ''', (dashboard_id, telegram_id))
            await db.execute('DELETE FROM llm_configs WHERE dashboard_id = ? AND telegram_id = ?', (dashboard_id, telegram_id))
            await db.execute('DELETE FROM pending_executions WHERE dashboard_id = ? AND telegram_id = ?', (dashboard_id, telegram_id))
            
            # 检查是否还有其他面板
            async with db.execute('''
                SELECT id 
                FROM dashboards 
                WHERE telegram_id = ? 
                ORDER BY id ASC
            ''', (telegram_id,)) as cursor:
                remaining_dashboards = await cursor.fetchall()
                
            if not remaining_dashboards:
                # 如果没有面板了，删除用户
                await db.execute('DELETE FROM users WHERE telegram_id = ?', (telegram_id,))
            elif is_default and remaining_dashboards:
                # 如果删除的是默认面板且还有其他面板，设置第一个面板为默认
                new_default_id = remaining_dashboards[0][0]
                await db.execute('''
                    UPDATE users 
                    SET default_dashboard_id = ? 
                    WHERE telegram_id = ?
                ''', (new_default_id, telegram_id))
            
            await db.commit()
            return bool(remaining_dashboards)  # 返回是否还有其他面板

    async def delete_user(self, telegram_id):
        async with aiosqlite.connect(self.db_path) as db:
            # 删除用户的所有 dashboard
            await db.execute('DELETE FROM dashboards WHERE telegram_id = ?', (telegram_id,))
            await db.execute('DELETE FROM llm_configs WHERE telegram_id = ?', (telegram_id,))
            await db.execute('DELETE FROM pending_executions WHERE telegram_id = ?', (telegram_id,))
            # 删除用户
            await db.execute('DELETE FROM users WHERE telegram_id = ?', (telegram_id,))
            await db.commit()
