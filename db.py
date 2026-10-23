import asyncio
import hashlib
import os
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = os.getenv("DB_PATH", "data.db")
SCHEMA_VERSION = 4
SCHEMA_FILE = Path(__file__).with_name("schema.sql")


class DB:
    """SQLite с внешними ключами и фоновой записью: вызывающий код только кладёт запись в очередь."""

    def __init__(self, path: str = DB_PATH):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self.q: asyncio.Queue = asyncio.Queue()
        self._task = None

    # ---------- схема и миграции ----------

    def _migrate(self):
        ver = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if ver >= SCHEMA_VERSION:
            return
        has_old = self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='chats'").fetchone()
        if not has_old:                                   # чистая база
            self.conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        else:                                             # база v1 (без внешних ключей, подтемы в JSON)
            if ver < 2:
                self._upgrade_v1()
            self._upgrade_v2()
            self._upgrade_v3()
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    def _upgrade_v1(self):
        import json
        self.conn.commit()
        self.conn.close()
        backup = self.path + ".bak-v1"
        if not os.path.exists(backup):
            shutil.copy2(self.path, backup)               # копия на случай, если что-то пойдёт не так
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        c = self.conn
        c.execute("PRAGMA foreign_keys=OFF")
        try:
            c.execute("SELECT subs FROM chats LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE chats ADD COLUMN subs TEXT NOT NULL DEFAULT '[]'")
        for t in ("chats", "messages", "users"):
            c.execute(f"ALTER TABLE {t} RENAME TO {t}_old")
        c.execute("DROP INDEX IF EXISTS idx_messages_chat")
        c.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        c.execute("PRAGMA foreign_keys=OFF")        # schema.sql включает ключи, а при переносе данных они мешают
        c.execute("INSERT INTO users(uid,lang,push) SELECT uid,lang,push FROM users_old")
        c.execute("INSERT OR IGNORE INTO users(uid) SELECT DISTINCT owner FROM chats_old")   # владельцы без строки в users
        c.execute("""INSERT INTO chats(id,owner,type,peer,avatar,num,unread,topic,n)
                     SELECT id,owner,type,peer,avatar,num,unread,topic,n FROM chats_old""")
        c.execute("UPDATE chats SET peer=NULL WHERE peer IS NOT NULL AND peer NOT IN (SELECT id FROM chats)")
        c.execute("""INSERT INTO messages(id,chat_id,role,content)
                     SELECT id,chat_id,role,content FROM messages_old WHERE chat_id IN (SELECT id FROM chats)""")
        self._old_subs = [(cid, topic, json.loads(subs or "[]"))
                          for cid, topic, subs in c.execute("SELECT id,topic,subs FROM chats_old")]
        # темы/подтемы ещё не заполнены (их кладёт seed_catalog) — подтемы чатов дольём после seed
        c.commit()
        for t in ("chats", "messages", "users"):
            c.execute(f"DROP TABLE {t}_old")
        c.commit()
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=NORMAL")

    def _upgrade_v2(self):
        """Добавляет веб-аккаунты и сессии, сохраняя Telegram id старых пользователей."""
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(users)")}
        for name, definition in (
            ("email", "TEXT"),
            ("password_hash", "TEXT"),
            ("display_name", "TEXT"),
            ("telegram_id", "INTEGER"),
        ):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        self.conn.execute("UPDATE users SET telegram_id=uid WHERE telegram_id IS NULL AND email IS NULL")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email COLLATE NOCASE)")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions(
              token_hash TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
              expires_at INTEGER NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        """)

    def _upgrade_v3(self):
        """Добавляет редактируемое описание и пользовательский аватар."""
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(users)")}
        if "bio" not in columns:
            self.conn.execute("ALTER TABLE users ADD COLUMN bio TEXT")
        if "avatar" not in columns:
            self.conn.execute("ALTER TABLE users ADD COLUMN avatar TEXT")

    def seed_catalog(self, topics: dict, catalog: dict):
        """Заполняет справочники topics/subtopics из кода (вызывается при старте, безопасно повторять)."""
        with self.conn:
            for key, title in topics.items():
                self.conn.execute(
                    "INSERT INTO topics(key,title) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET title=excluded.title",
                    (key, title))
            for tkey, groups in catalog.items():
                for grp, items in groups.items():
                    for sid, title in items.items():
                        self.conn.execute(
                            "INSERT INTO subtopics(topic_key,id,grp,title) VALUES(?,?,?,?) "
                            "ON CONFLICT(topic_key,id) DO UPDATE SET grp=excluded.grp, title=excluded.title",
                            (tkey, sid, grp, title))
            old = getattr(self, "_old_subs", [])           # подтемы из миграции v1
            for cid, topic, subs in old:
                for sid in subs:
                    self.conn.execute("INSERT OR IGNORE INTO chat_subs(chat_id,topic_key,sub_id) "
                                      "SELECT ?,?,? WHERE EXISTS(SELECT 1 FROM subtopics WHERE topic_key=? AND id=?)",
                                      (cid, topic, sid, topic, sid))
            self._old_subs = []
            # старые чаты с темой, которой нет в справочнике, не должны ломать внешний ключ
            self.conn.execute("UPDATE chats SET topic=NULL WHERE topic IS NOT NULL AND topic NOT IN (SELECT key FROM topics)")

    # ---------- загрузка при старте ----------

    def load(self) -> list:
        chats = {}
        rows = self.conn.execute("SELECT id,owner,type,peer,avatar,num,unread,topic,n FROM chats ORDER BY id")
        for cid, owner, ctype, peer, avatar, num, unread, topic, n in rows:
            chats[cid] = {"id": cid, "owner": owner, "type": ctype, "peer": peer, "avatar": avatar,
                          "num": num, "unread": unread, "topic": topic, "n": n, "subs": [], "history": []}
        for cid, sid in self.conn.execute("SELECT chat_id, sub_id FROM chat_subs ORDER BY rowid"):
            if cid in chats:
                chats[cid]["subs"].append(sid)
        for cid, role, content in self.conn.execute("SELECT chat_id, role, content FROM messages ORDER BY id"):
            if cid in chats:
                chats[cid]["history"].append({"role": role, "content": content})
        return list(chats.values())

    def load_users(self) -> list:
        return self.conn.execute("SELECT uid, lang, push FROM users").fetchall()

    # ---------- веб-аккаунты и сессии ----------

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create_web_user(self, email: str, password_hash: str, display_name: str) -> dict | None:
        try:
            with self.lock:
                with self.conn:
                    cur = self.conn.execute(
                        "INSERT INTO users(email,password_hash,display_name,push) VALUES(?,?,?,0)",
                        (email.lower(), password_hash, display_name),
                    )
                return self.get_user(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def get_user(self, uid: int) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT uid,email,password_hash,display_name,bio,avatar,telegram_id FROM users WHERE uid=?", (uid,)
            ).fetchone()
        if not row:
            return None
        return dict(zip(("id", "email", "password_hash", "name", "bio", "avatar", "telegram_id"), row))

    def update_profile(self, uid: int, name: str, bio: str, avatar: str | None) -> dict | None:
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "UPDATE users SET display_name=?, bio=?, avatar=? WHERE uid=?",
                    (name, bio, avatar, uid),
                )
            return self.get_user(uid)

    def get_user_by_email(self, email: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT uid FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
            return self.get_user(row[0]) if row else None

    def get_or_create_telegram_user(self, telegram_id: int, name: str = "") -> dict:
        with self.lock:
            row = self.conn.execute("SELECT uid FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
            if row:
                return self.get_user(row[0])
            with self.conn:
                try:
                    cur = self.conn.execute(
                        "INSERT INTO users(telegram_id,display_name) VALUES(?,?)", (telegram_id, name or None)
                    )
                    uid = cur.lastrowid
                except sqlite3.IntegrityError:
                    uid = self.conn.execute("SELECT uid FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()[0]
            return self.get_user(uid)

    def create_session(self, uid: int, lifetime: int = 60 * 60 * 24 * 30) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self.lock:
            with self.conn:
                self.conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
                self.conn.execute(
                    "INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
                    (self._token_hash(token), uid, now + lifetime),
                )
        return token

    def user_from_session(self, token: str) -> dict | None:
        if not token:
            return None
        with self.lock:
            row = self.conn.execute(
                "SELECT user_id FROM sessions WHERE token_hash=? AND expires_at>?",
                (self._token_hash(token), int(time.time())),
            ).fetchone()
            return self.get_user(row[0]) if row else None

    def delete_session(self, token: str):
        if token:
            with self.lock:
                with self.conn:
                    self.conn.execute("DELETE FROM sessions WHERE token_hash=?", (self._token_hash(token),))

    def telegram_id_for_user(self, uid: int) -> int | None:
        with self.lock:
            row = self.conn.execute("SELECT telegram_id FROM users WHERE uid=?", (uid,)).fetchone()
        return row[0] if row else None

    # ---------- запись (через очередь) ----------

    def _put(self, *stmts: tuple):
        """Одна «запись» = несколько SQL-команд, которые выполнятся вместе, в порядке очереди."""
        self.q.put_nowait(stmts)

    def save_chat(self, c: dict):
        # UPSERT, а не INSERT OR REPLACE: REPLACE удалил бы строку и каскадом стёр бы сообщения чата
        stmts = [
            ("INSERT OR IGNORE INTO users(uid) VALUES(?)", (c["owner"],)),
            ("""INSERT INTO chats(id,owner,type,peer,avatar,num,unread,topic,n) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET owner=excluded.owner, type=excluded.type, peer=excluded.peer,
                  avatar=excluded.avatar, num=excluded.num, unread=excluded.unread, topic=excluded.topic, n=excluded.n""",
             (c["id"], c["owner"], c["type"], c["peer"], c["avatar"], c["num"], c["unread"], c["topic"], c["n"])),
            ("DELETE FROM chat_subs WHERE chat_id=?", (c["id"],)),
        ]
        if c["topic"]:
            for sid in c["subs"]:
                stmts.append(("INSERT OR IGNORE INTO chat_subs(chat_id,topic_key,sub_id) VALUES(?,?,?)",
                              (c["id"], c["topic"], sid)))
        self._put(*stmts)

    def add_message(self, chat_id: int, role: str, content: str):
        self._put(("INSERT INTO messages(chat_id, role, content) VALUES(?,?,?)", (chat_id, role, content)))

    def clear_messages(self, chat_id: int):
        self._put(("DELETE FROM messages WHERE chat_id=?", (chat_id,)))

    def save_user(self, uid: int, lang: str, push: bool):
        self._put(("""INSERT INTO users(uid,lang,push) VALUES(?,?,?)
                      ON CONFLICT(uid) DO UPDATE SET lang=excluded.lang, push=excluded.push""",
                   (uid, lang, int(push))))

    def add_match(self, chat_a: int, chat_b: int, topic):
        self._put(("INSERT INTO matches(chat_a,chat_b,topic) VALUES(?,?,?)", (chat_a, chat_b, topic)))

    # ---------- фоновый писатель ----------

    def _write(self, batch: list):
        with self.lock:
            for stmts in batch:                  # каждая запись — своя транзакция: одна плохая не валит остальные
                sql = ""
                try:
                    with self.conn:
                        for sql, params in stmts:
                            self.conn.execute(sql, params)
                except sqlite3.Error as e:
                    print(f"[ERROR] запись в БД: {e} | {sql.strip()[:70]}")

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
