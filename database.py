# database.py

import aiosqlite

class Database:
    def __init__(self, db_path):
        self.db_path = db_path

    async def initialize(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    dashboard_url TEXT NOT NULL
                )
            ''')
            await db.commit()

    async def add_user(self, telegram_id, username, password, dashboard_url):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT OR REPLACE INTO users (telegram_id, username, password, dashboard_url)
                VALUES (?, ?, ?, ?)
            ''', (telegram_id, username, password, dashboard_url))
            await db.commit()

    async def get_user(self, telegram_id):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('SELECT username, password, dashboard_url FROM users WHERE telegram_id = ?', (telegram_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {'username': row[0], 'password': row[1], 'dashboard_url': row[2]}
                else:
                    return None

    async def delete_user(self, telegram_id):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('DELETE FROM users WHERE telegram_id = ?', (telegram_id,))
            await db.commit()
