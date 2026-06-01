import asyncio
import sqlite3
import tempfile
import unittest
import importlib.util
from pathlib import Path

if importlib.util.find_spec("aiosqlite") is not None:
    from database import Database


@unittest.skipIf(importlib.util.find_spec("aiosqlite") is None, "aiosqlite is not installed")
class DatabaseMigrationTests(unittest.TestCase):
    def test_existing_dashboard_rows_default_to_password_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "users.db")
            con = sqlite3.connect(db_path)
            con.execute(
                """
                CREATE TABLE users (
                    telegram_id INTEGER PRIMARY KEY,
                    default_dashboard_id INTEGER
                )
                """
            )
            con.execute(
                """
                CREATE TABLE dashboards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    dashboard_url TEXT NOT NULL,
                    alias TEXT
                )
                """
            )
            con.execute("INSERT INTO users (telegram_id, default_dashboard_id) VALUES (42, 1)")
            con.execute(
                """
                INSERT INTO dashboards (id, telegram_id, username, password, dashboard_url, alias)
                VALUES (1, 42, 'alice', 'secret', 'https://nezha.example', 'MAIN')
                """
            )
            con.commit()
            con.close()

            asyncio.run(Database(db_path).initialize())
            user = asyncio.run(Database(db_path).get_user(42))

            self.assertEqual(user["auth_type"], "password")
            self.assertEqual(user["username"], "alice")
            self.assertIsNone(user["api_token"])

    def test_token_dashboard_can_be_added_without_username_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "users.db")
            database = Database(db_path)
            asyncio.run(database.initialize())
            dashboard_id = asyncio.run(
                database.add_user(
                    42,
                    "",
                    "",
                    "https://nezha.example",
                    "TOKEN",
                    auth_type="token",
                    api_token="nzp_test_secret",
                )
            )

            dashboards = asyncio.run(database.get_all_dashboards(42))

            self.assertEqual(dashboard_id, dashboards[0]["id"])
            self.assertEqual(dashboards[0]["auth_type"], "token")
            self.assertEqual(dashboards[0]["api_token"], "nzp_test_secret")

    def test_update_dashboard_token_keeps_password_auth_parallel(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "users.db")
            database = Database(db_path)
            asyncio.run(database.initialize())
            dashboard_id = asyncio.run(
                database.add_user(
                    42,
                    "alice",
                    "secret",
                    "https://nezha.example",
                    "MAIN",
                )
            )

            asyncio.run(database.update_dashboard_token(42, dashboard_id, "nzp_parallel_secret"))
            dashboard = asyncio.run(database.get_dashboard(42, dashboard_id))

            self.assertEqual(dashboard["auth_type"], "password")
            self.assertEqual(dashboard["username"], "alice")
            self.assertEqual(dashboard["api_token"], "nzp_parallel_secret")


if __name__ == "__main__":
    unittest.main()
