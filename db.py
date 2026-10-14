import asyncio
import json
import os
import sqlite3

DB_PATH = os.getenv("DB_PATH", "data.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
  id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, type TEXT NOT NULL, peer INTEGER,
  avatar TEXT NOT NULL, num INTEGER NOT NULL, unread INTEGER NOT NULL DEFAULT 0,
  topic TEXT, n INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
  role TEXT NOT NULL, content TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
CREATE TABLE IF NOT EXISTS users(
  uid INTEGER PRIMARY KEY, lang TEXT NOT NULL DEFAULT 'auto', push INTEGER NOT NULL DEFAULT 1);
"""


class DB:
    """SQLite с фоновой записью: вызывающий код только кладёт запись в очередь и не ждёт диск."""

    def __init__(self, path: str = DB_PATH):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        try:  # миграция: подтемы чата (в старой базе колонки ещё нет)
            self.conn.execute("ALTER TABLE chats ADD COLUMN subs TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass
        self.q: asyncio.Queue = asyncio.Queue()
        self._task = None

    # ---------- загрузка при старте ----------

    def load(self) -> list:
        chats = {}
        rows = self.conn.execute("SELECT id,owner,type,peer,avatar,num,unread,topic,n,subs FROM chats ORDER BY id")
        for cid, owner, ctype, peer, avatar, num, unread, topic, n, subs in rows:
            chats[cid] = {"id": cid, "owner": owner, "type": ctype, "peer": peer, "avatar": avatar,
                          "num": num, "unread": unread, "topic": topic, "n": n,
                          "subs": json.loads(subs or "[]"), "history": []}
        for cid, role, content in self.conn.execute("SELECT chat_id, role, content FROM messages ORDER BY id"):
            if cid in chats:
                chats[cid]["history"].append({"role": role, "content": content})
        return list(chats.values())

    def load_users(self) -> list:
        return self.conn.execute("SELECT uid, lang, push FROM users").fetchall()

    # ---------- запись (через очередь) ----------

    def _put(self, sql: str, params: tuple):
        self.q.put_nowait((sql, params))

    def save_chat(self, c: dict):
        self._put(
            "INSERT OR REPLACE INTO chats(id,owner,type,peer,avatar,num,unread,topic,n,subs) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (c["id"], c["owner"], c["type"], c["peer"], c["avatar"], c["num"], c["unread"], c["topic"], c["n"],
             json.dumps(c["subs"])),
        )

    def add_message(self, chat_id: int, role: str, content: str):
        self._put("INSERT INTO messages(chat_id, role, content) VALUES(?,?,?)", (chat_id, role, content))

    def clear_messages(self, chat_id: int):
        self._put("DELETE FROM messages WHERE chat_id=?", (chat_id,))

    def save_user(self, uid: int, lang: str, push: bool):
        self._put("INSERT OR REPLACE INTO users(uid,lang,push) VALUES(?,?,?)", (uid, lang, int(push)))

    # ---------- фоновый писатель ----------

    def _write(self, batch: list):
        with self.conn:  # одна транзакция на пачку
            for sql, params in batch:
                self.conn.execute(sql, params)

    async def _run(self):
        while True:
            batch = [await self.q.get()]
            while not self.q.empty():
                batch.append(self.q.get_nowait())
            stop = None in batch
            batch = [b for b in batch if b is not None]
            if batch:
                try:
                    await asyncio.to_thread(self._write, batch)
                except Exception as e:
                    print(f"[ERROR] запись в БД: {e}")
            if stop:
                break

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        """Дописывает всё, что осталось в очереди, и останавливается."""
        if self._task:
            self.q.put_nowait(None)
            await self._task