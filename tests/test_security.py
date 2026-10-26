import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from aiohttp.test_utils import make_mocked_request

_temp_dir = tempfile.TemporaryDirectory()
os.environ["DB_BACKEND"] = "sqlite"
os.environ["DB_PATH"] = os.path.join(_temp_dir.name, "test.db")
os.environ["BOT_TOKEN"] = ""
os.environ["BOT_ENABLED"] = "false"
os.environ["OPENAI_API_KEY"] = "test-only"

import bot

SlidingWindowLimiter = bot.SlidingWindowLimiter
check_password = bot.check_password
hash_password = bot.hash_password


def tearDownModule():
    bot.db.conn.close()
    _temp_dir.cleanup()


class PasswordTests(unittest.TestCase):
    def test_password_hash_is_salted_and_verifiable(self):
        first = hash_password("correct horse battery staple")
        second = hash_password("correct horse battery staple")
        self.assertNotEqual(first, second)
        self.assertTrue(check_password("correct horse battery staple", first))
        self.assertFalse(check_password("wrong", first))


class RateLimiterTests(unittest.TestCase):
    def test_limit_is_enforced_per_key(self):
        limiter = SlidingWindowLimiter()
        self.assertTrue(limiter.allow("a", 2, 60))
        self.assertTrue(limiter.allow("a", 2, 60))
        self.assertFalse(limiter.allow("a", 2, 60))
        self.assertTrue(limiter.allow("b", 2, 60))


class RequestSecurityTests(unittest.TestCase):
    def test_csrf_requires_matching_cookie_and_header(self):
        good = make_mocked_request("POST", "/api/state", headers={
            "Cookie": f"{bot.CSRF_COOKIE}=token", "X-CSRF-Token": "token"
        })
        bad = make_mocked_request("POST", "/api/state", headers={
            "Cookie": f"{bot.CSRF_COOKIE}=token", "X-CSRF-Token": "other"
        })
        self.assertTrue(bot.csrf_valid(good))
        self.assertFalse(bot.csrf_valid(bad))

    def test_session_cookie_is_secure_and_http_only_behind_tls_proxy(self):
        from aiohttp import web

        request = make_mocked_request("GET", "/", headers={"X-Forwarded-Proto": "https"})
        response = web.Response()
        bot.set_session_cookie(response, "session-token", request)
        cookie = response.cookies[bot.SESSION_COOKIE]

        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")

    def test_second_process_cannot_acquire_application_lock(self):
        previous_lock_path = os.environ.get("INSTANCE_LOCK_FILE")
        with tempfile.TemporaryDirectory() as directory:
            os.environ["INSTANCE_LOCK_FILE"] = os.path.join(directory, "helper-bot.lock")
            bot.acquire_instance_lock()
            try:
                result = subprocess.run(
                    [sys.executable, "bot.py"],
                    capture_output=True, text=True, env=os.environ.copy(), timeout=30,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("only one process is supported", result.stderr)
            finally:
                bot._instance_lock.close()
                bot._instance_lock = None
                if previous_lock_path is None:
                    os.environ.pop("INSTANCE_LOCK_FILE", None)
                else:
                    os.environ["INSTANCE_LOCK_FILE"] = previous_lock_path

    def test_chat_access_is_owner_scoped(self):
        old = bot.chats
        try:
            bot.chats = {7: {"id": 7, "owner": 10}}
            self.assertIsNotNone(bot.own_chat(10, 7))
            self.assertIsNone(bot.own_chat(11, 7))
        finally:
            bot.chats = old


class FirestoreCacheTests(unittest.TestCase):
    def test_cached_session_needs_no_firestore_read(self):
        from firebase_db import FirebaseDB

        database = FirebaseDB.__new__(FirebaseDB)
        database._cache_lock = threading.RLock()
        database._session_cache_ttl = 21600
        database._user_cache_ttl = 21600
        token = "session-token"
        token_hash = database._token_hash(token)
        database._session_cache = {
            token_hash: (time.monotonic() + 60, int(time.time()) + 60, 12)
        }
        database._user_cache = {
            12: (time.monotonic() + 60, {"id": 12, "email": "user@example.com"})
        }
        database.client = None

        self.assertEqual(database.user_from_session(token)["id"], 12)

    def test_cached_telegram_identity_avoids_firestore_and_is_user_scoped(self):
        from firebase_db import FirebaseDB

        database = FirebaseDB.__new__(FirebaseDB)
        database._cache_lock = threading.RLock()
        database._user_cache_ttl = 21600
        database._telegram_user_cache = {
            101: (time.monotonic() + 60, 12),
            202: (time.monotonic() + 60, 34),
        }
        database.get_user = lambda uid: {"id": uid}
        database.client = None

        self.assertEqual(database.get_or_create_telegram_user(101)["id"], 12)
        self.assertEqual(database.get_or_create_telegram_user(202)["id"], 34)

    def test_logout_invalidates_cached_session(self):
        from unittest.mock import Mock
        from firebase_db import FirebaseDB

        database = FirebaseDB.__new__(FirebaseDB)
        database._cache_lock = threading.RLock()
        database._session_cache = {}
        token = "session-token"
        token_hash = database._token_hash(token)
        database._session_cache[token_hash] = (
            time.monotonic() + 60, int(time.time()) + 60, 12
        )
        database.client = Mock()
        session_ref = database.client.collection.return_value.document.return_value
        session_ref.get.return_value.exists = False
        session_ref.get.return_value.to_dict.return_value = {}

        database.delete_session(token)

        self.assertNotIn(token_hash, database._session_cache)
        self.assertIsNone(database.user_from_session(token))
        session_ref.delete.assert_called_once_with()

    def test_account_revocation_only_invalidates_that_users_sessions(self):
        from unittest.mock import Mock
        from firebase_db import FirebaseDB

        database = FirebaseDB.__new__(FirebaseDB)
        database._cache_lock = threading.RLock()
        database._session_cache = {
            "session-a": (time.monotonic() + 60, int(time.time()) + 60, 12),
            "session-b": (time.monotonic() + 60, int(time.time()) + 60, 34),
        }
        database.client = Mock()
        database.client.collection.return_value.where.return_value.stream.return_value = []

        database.revoke_user_sessions(12)

        self.assertEqual(set(database._session_cache), {"session-b"})
        database.client.collection.return_value.where.assert_called_once_with("user_id", "==", 12)


if __name__ == "__main__":
    unittest.main()
