"""Real HTTP authentication with Pages-style bearer sessions and Render cookies."""

import os
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import ClientTimeout, CookieJar
from aiohttp.test_utils import TestClient, TestServer


class ApiTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "BOT_TOKEN": "", "OPENAI_API_KEY": "test-only",
            "DB_PATH": ":memory:",
            "ALLOWED_ORIGINS": "https://the-fabulous.github.io",
        })
        self.environment.start()
        import bot
        from db import DB

        self.bot = bot
        self.old_db = bot.db
        bot.db = DB(os.path.join(self.directory.name, "accounts.db"))
        bot.db.seed_catalog(bot.TOPICS, bot.CATALOG)
        self.client = TestClient(TestServer(bot.create_app()), cookie_jar=CookieJar(unsafe=True),
                                 timeout=ClientTimeout(total=10))
        await self.client.start_server()
        self.origin = {"Origin": "https://the-fabulous.github.io"}

    async def asyncTearDown(self):
        await self.client.close()
        self.bot.db.conn.close()
        self.bot.db = self.old_db
        self.environment.stop()
        self.directory.cleanup()

    async def register(self, transport=None):
        body = {"email": "transport@example.com", "password": "test-password-123", "name": "Test"}
        if transport:
            body["session_transport"] = transport
        response = await self.client.post("/api/auth/register", json=body, headers=self.origin)
        self.assertEqual(response.status, 200)
        return response, await response.json()

    async def test_preflight_only_allows_known_origin_and_headers(self):
        headers = {**self.origin, "Access-Control-Request-Method": "POST",
                   "Access-Control-Request-Headers": "authorization,content-type,x-init-data"}
        response = await self.client.options("/api/auth/login", headers=headers)
        self.assertEqual(response.status, 204)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], self.origin["Origin"])
        response = await self.client.options("/api/auth/login", headers={**headers, "Origin": "https://evil.example"})
        self.assertEqual(response.status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        response = await self.client.options("/api/auth/login", headers={**headers, "Access-Control-Request-Headers": "x-unexpected"})
        self.assertEqual(response.status, 403)

    async def test_bearer_login_restoration_and_logout_without_cookies(self):
        response, data = await self.register("bearer")
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("password", data)
        token = data["session_token"]
        self.assertTrue(token)
        stored = self.bot.db.get_user_by_email("transport@example.com")
        self.assertNotEqual(stored["password_hash"], "test-password-123")
        response = await self.client.post("/api/auth/me", json={}, headers=self.origin)
        self.assertEqual(response.status, 401)
        response = await self.client.post("/api/auth/login", json={
            "email": "transport@example.com", "password": "incorrect", "session_transport": "bearer",
        }, headers=self.origin)
        self.assertEqual(response.status, 401)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], self.origin["Origin"])
        self.assertNotIn("session_token", await response.json())
        headers = {**self.origin, "Authorization": "Bearer " + token}
        response = await self.client.post("/api/auth/me", json={}, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["account"]["name"], "Test")

        # A new DB connection, as after a server restart, resolves the same session.
        from db import DB
        self.bot.db.conn.close()
        self.bot.db = DB(os.path.join(self.directory.name, "accounts.db"))
        response = await self.client.post("/api/auth/me", json={}, headers=headers)
        self.assertEqual(response.status, 200)
        response = await self.client.post("/api/auth/logout", json={}, headers=headers)
        self.assertEqual(response.status, 200)
        response = await self.client.post("/api/auth/me", json={}, headers=headers)
        self.assertEqual(response.status, 401)

        response = await self.client.post("/api/auth/login", json={
            "email": "transport@example.com", "password": "test-password-123", "session_transport": "bearer",
        }, headers=self.origin)
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["session_token"])

    async def test_cookie_login_still_works_and_bad_bearer_does_not_fall_back(self):
        response, data = await self.register()
        self.assertNotIn("session_token", data)
        self.assertIn("Set-Cookie", response.headers)
        response = await self.client.post("/api/auth/me", json={})
        self.assertEqual(response.status, 200)
        response = await self.client.post("/api/auth/me", json={}, headers={"Authorization": "Bearer invalid"})
        self.assertEqual(response.status, 401)
        response = await self.client.post("/api/auth/logout", json={})
        self.assertEqual(response.status, 200)
        response = await self.client.post("/api/auth/me", json={})
        self.assertEqual(response.status, 401)

    async def test_disallowed_origin_cannot_create_accounts(self):
        response = await self.client.post("/api/auth/register", json={
            "email": "blocked@example.com", "password": "test-password-123",
        }, headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status, 403)
        self.assertIsNone(self.bot.db.get_user_by_email("blocked@example.com"))
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), None)


if __name__ == "__main__":
    unittest.main()
