"""Firestore implementation of the storage API used by bot.py.

Credentials are read from FIREBASE_CREDENTIALS (preferred) or
GOOGLE_APPLICATION_CREDENTIALS.  Service-account JSON must never be committed.
"""

import asyncio
import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore


class FirebaseDB:
    def __init__(self):
        credential_path = (
            os.getenv("FIREBASE_CREDENTIALS")
            or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        )
        if not firebase_admin._apps:
            if credential_path:
                path = Path(credential_path).expanduser()
                if not path.is_file():
                    raise RuntimeError(f"Firebase credentials not found: {path}")
                firebase_admin.initialize_app(credentials.Certificate(str(path)))
            else:
                firebase_admin.initialize_app()
        self.client = firestore.client()
        self.q: asyncio.Queue = asyncio.Queue()
        self._task = None
        self._cache_lock = threading.RLock()
        self._session_cache = {}
        self._user_cache = {}
        self._session_cache_ttl = max(60, int(os.getenv("SESSION_CACHE_TTL", "21600")))
        self._user_cache_ttl = max(60, int(os.getenv("USER_CACHE_TTL", "21600")))
        self._telegram_user_cache = {}
        self._message_load_limit = max(20, int(os.getenv("MESSAGE_LOAD_LIMIT", "80")))
        self._last_write_error = None

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _counter(self, name: str) -> int:
        ref = self.client.collection("meta").document("counters")
        transaction = self.client.transaction()

        @firestore.transactional
        def increment(tx):
            snapshot = ref.get(transaction=tx)
            value = int((snapshot.to_dict() or {}).get(name, 0)) + 1
            tx.set(ref, {name: value}, merge=True)
            return value

        return increment(transaction)

    def next_chat_id(self) -> int:
        return self._counter("chat_id")

    def seed_catalog(self, topics: dict, catalog: dict):
        payload = json.dumps({"topics": topics, "catalog": catalog}, ensure_ascii=False, sort_keys=True)
        version = hashlib.sha256(payload.encode()).hexdigest()
        meta_ref = self.client.collection("meta").document("catalog")
        current = meta_ref.get()
        if current.exists and (current.to_dict() or {}).get("version") == version:
            return
        batch = self.client.batch()
        operations = 0
        for key, title in topics.items():
            batch.set(self.client.collection("topics").document(str(key)), {"title": title})
            operations += 1
        for topic_key, groups in catalog.items():
            for group, items in groups.items():
                for sid, title in items.items():
                    ref = (self.client.collection("topics").document(str(topic_key))
                           .collection("subtopics").document(str(sid)))
                    batch.set(ref, {"group": group, "title": title})
                    operations += 1
                    if operations >= 450:
                        batch.commit()
                        batch, operations = self.client.batch(), 0
        if operations:
            batch.set(meta_ref, {"version": version, "updated_at": self._now()})
            batch.commit()
        else:
            meta_ref.set({"version": version, "updated_at": self._now()})

    def load(self) -> list:
        result = []
        for snapshot in self.client.collection("chats").stream():
            data = snapshot.to_dict() or {}
            chat = {
                "id": int(snapshot.id), "owner": int(data["owner"]),
                "type": data.get("type", "AI"), "peer": data.get("peer"),
                "avatar": data.get("avatar", "🤖"), "num": int(data.get("num", 1)),
                "unread": int(data.get("unread", 0)), "topic": data.get("topic"),
                "n": int(data.get("n", 0)), "subs": list(data.get("subs", [])), "history": [],
            }
            # The UI only exposes the recent conversation.  Bounding this query keeps
            # restarts from reading the complete lifetime of every chat.
            messages = (snapshot.reference.collection("messages")
                        .order_by("created_at", direction=firestore.Query.DESCENDING)
                        .limit(self._message_load_limit).stream())
            chat["history"] = list(reversed([
                {"role": item.get("role"), "content": item.get("content", "")}
                for item in (message.to_dict() or {} for message in messages)
            ]))
            result.append(chat)
        return sorted(result, key=lambda item: item["id"])

    def load_users(self) -> list:
        return [
            (int(snapshot.id), data.get("lang", "auto"), bool(data.get("push", True)))
            for snapshot in self.client.collection("users").stream()
            for data in [snapshot.to_dict() or {}]
        ]

    def create_web_user(self, email: str, password_hash: str, display_name: str):
        normalized = email.strip().lower()
        index_ref = self.client.collection("email_index").document(hashlib.sha256(normalized.encode()).hexdigest())
        counter_ref = self.client.collection("meta").document("counters")
        transaction = self.client.transaction()

        @firestore.transactional
        def create(tx):
            if index_ref.get(transaction=tx).exists:
                return None
            counters = counter_ref.get(transaction=tx).to_dict() or {}
            uid = int(counters.get("user_id", 0)) + 1
            user_ref = self.client.collection("users").document(str(uid))
            tx.set(counter_ref, {"user_id": uid}, merge=True)
            tx.set(user_ref, {"email": normalized, "password_hash": password_hash,
                              "display_name": display_name, "bio": None, "avatar": None,
                              "telegram_id": None, "lang": "auto", "push": False,
                              "created_at": self._now()})
            tx.set(index_ref, {"uid": uid, "email": normalized})
            return uid

        uid = create(transaction)
        return self.get_user(uid) if uid is not None else None

    def get_user(self, uid: int):
        if uid is None:
            return None
        now = time.monotonic()
        with self._cache_lock:
            cached = self._user_cache.get(int(uid))
            if cached and cached[0] > now:
                return dict(cached[1])
        snapshot = self.client.collection("users").document(str(uid)).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        user = {"id": int(snapshot.id), "email": data.get("email"),
                "password_hash": data.get("password_hash"), "name": data.get("display_name"),
                "bio": data.get("bio"), "avatar": data.get("avatar"),
                "telegram_id": data.get("telegram_id")}
        with self._cache_lock:
            self._user_cache[user["id"]] = (now + self._user_cache_ttl, dict(user))
        return user

    def update_profile(self, uid: int, name: str, bio: str, avatar):
        ref = self.client.collection("users").document(str(uid))
        ref.update({"display_name": name, "bio": bio, "avatar": avatar})
        with self._cache_lock:
            self._user_cache.pop(int(uid), None)
        return self.get_user(uid)

    def get_user_by_email(self, email: str):
        normalized = email.strip().lower()
        key = hashlib.sha256(normalized.encode()).hexdigest()
        snapshot = self.client.collection("email_index").document(key).get()
        return self.get_user((snapshot.to_dict() or {}).get("uid")) if snapshot.exists else None

    def get_or_create_telegram_user(self, telegram_id: int, name: str = ""):
        telegram_id = int(telegram_id)
        now = time.monotonic()
        with self._cache_lock:
            cached = self._telegram_user_cache.get(telegram_id)
            if cached and cached[0] > now:
                cached_uid = cached[1]
            else:
                self._telegram_user_cache.pop(telegram_id, None)
                cached_uid = None
        if cached_uid is not None:
            user = self.get_user(cached_uid)
            if user:
                return user

        index_ref = self.client.collection("telegram_index").document(str(telegram_id))
        counter_ref = self.client.collection("meta").document("counters")
        transaction = self.client.transaction()

        @firestore.transactional
        def create(tx):
            existing = index_ref.get(transaction=tx)
            if existing.exists:
                return int(existing.get("uid"))
            counters = counter_ref.get(transaction=tx).to_dict() or {}
            uid = int(counters.get("user_id", 0)) + 1
            user_ref = self.client.collection("users").document(str(uid))
            tx.set(counter_ref, {"user_id": uid}, merge=True)
            tx.set(user_ref, {"email": None, "password_hash": None,
                              "display_name": name or None, "bio": None, "avatar": None,
                              "telegram_id": telegram_id, "lang": "auto", "push": True,
                              "created_at": self._now()})
            tx.set(index_ref, {"uid": uid})
            return uid

        uid = create(transaction)
        with self._cache_lock:
            self._telegram_user_cache[telegram_id] = (
                time.monotonic() + self._user_cache_ttl, int(uid)
            )
            if len(self._telegram_user_cache) > 20_000:
                self._telegram_user_cache = {
                    key: value for key, value in self._telegram_user_cache.items()
                    if value[0] > now
                }
        return self.get_user(uid)

    def create_session(self, uid: int, lifetime: int = 60 * 60 * 24 * 30) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = self._token_hash(token)
        expires_at = int(time.time()) + lifetime
        self.client.collection("sessions").document(token_hash).set({
            "user_id": uid, "expires_at": expires_at, "created_at": self._now()
        })
        self.get_user(uid)  # warm the user cache for the first authenticated request
        with self._cache_lock:
            self._session_cache[token_hash] = (
                min(time.monotonic() + self._session_cache_ttl,
                    time.monotonic() + max(0, expires_at - int(time.time()))),
                expires_at, int(uid),
            )
        return token

    def user_from_session(self, token: str):
        if not token:
            return None
        token_hash = self._token_hash(token)
        now_mono, now = time.monotonic(), int(time.time())
        with self._cache_lock:
            cached = self._session_cache.get(token_hash)
            if cached and cached[0] > now_mono and cached[1] > now:
                return self.get_user(cached[2])
            self._session_cache.pop(token_hash, None)
        snapshot = self.client.collection("sessions").document(token_hash).get()
        data = snapshot.to_dict() or {}
        expires_at = int(data.get("expires_at", 0))
        if not snapshot.exists or expires_at <= now:
            if snapshot.exists:
                snapshot.reference.delete()
            return None
        user = self.get_user(data.get("user_id"))
        with self._cache_lock:
            self._session_cache[token_hash] = (
                min(now_mono + self._session_cache_ttl, now_mono + expires_at - now),
                expires_at, int(data["user_id"]),
            )
        return user

    def delete_session(self, token: str):
        if token:
            token_hash = self._token_hash(token)
            with self._cache_lock:
                self._session_cache.pop(token_hash, None)
            self.client.collection("sessions").document(token_hash).delete()

    def revoke_user_sessions(self, uid: int):
        """Administrative revocation; local cache is invalidated immediately."""
        for snapshot in self.client.collection("sessions").where("user_id", "==", uid).stream():
            snapshot.reference.delete()
        with self._cache_lock:
            self._session_cache = {
                key: value for key, value in self._session_cache.items()
                if value[2] != uid
            }

    def telegram_id_for_user(self, uid: int):
        user = self.get_user(uid)
        return user.get("telegram_id") if user else None

    def _put(self, operation: str, *args):
        self.q.put_nowait((operation, args))

    def save_chat(self, chat: dict):
        data = {key: chat.get(key) for key in
                ("owner", "type", "peer", "avatar", "num", "unread", "topic", "n", "subs")}
        self._put("save_chat", chat["id"], data)

    def add_message(self, chat_id: int, role: str, content: str):
        self._put("add_message", secrets.token_urlsafe(16), chat_id, role, content)

    def clear_messages(self, chat_id: int):
        self._put("clear_messages", chat_id)

    def save_user(self, uid: int, lang: str, push: bool):
        self._put("save_user", uid, lang, push)

    def add_match(self, chat_a: int, chat_b: int, topic):
        self._put("add_match", secrets.token_urlsafe(16), chat_a, chat_b, topic)

    def _write(self, batch: list):
        for operation, args in batch:
            if operation == "save_chat":
                cid, data = args
                self.client.collection("chats").document(str(cid)).set(data, merge=True)
            elif operation == "add_message":
                message_id, cid, role, content = args
                (self.client.collection("chats").document(str(cid)).collection("messages")
                 .document(message_id).set(
                    {"role": role, "content": content, "created_at": self._now()}))
            elif operation == "clear_messages":
                cid, = args
                ref = self.client.collection("chats").document(str(cid)).collection("messages")
                for snapshot in ref.stream():
                    snapshot.reference.delete()
            elif operation == "save_user":
                uid, lang, push = args
                self.client.collection("users").document(str(uid)).set(
                    {"lang": lang, "push": bool(push)}, merge=True)
            elif operation == "add_match":
                match_id, chat_a, chat_b, topic = args
                self.client.collection("matches").document(match_id).set(
                    {"chat_a": chat_a, "chat_b": chat_b, "topic": topic,
                     "matched_at": self._now()})
        self._last_write_error = None

    async def _run(self):
        while True:
            first = await self.q.get()
            if first is None:
                break
            batch = [first]
            while not self.q.empty():
                item = self.q.get_nowait()
                if item is None:
                    await self._write_with_retry(batch)
                    return
                batch.append(item)
            await self._write_with_retry(batch)

    async def _write_with_retry(self, batch: list):
        for attempt in range(4):
            try:
                await asyncio.to_thread(self._write, batch)
                return
            except Exception as error:
                self._last_write_error = repr(error)
                print(f"[ERROR] Firestore write attempt {attempt + 1}: {error!r}")
                if attempt < 3:
                    await asyncio.sleep(2 ** attempt)

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self.q.put_nowait(None)
            await self._task

    def health(self) -> dict:
        return {"backend": "firestore", "writer_running": bool(self._task and not self._task.done()),
                "queued_writes": self.q.qsize(), "last_write_error": self._last_write_error}
