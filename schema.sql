-- Social Bridge: схема БД (версия 4). Открывается в DBeaver как есть.
PRAGMA foreign_keys = ON;

-- Справочники ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS topics(
  key   TEXT PRIMARY KEY,                    -- 'music', 'anime', ...
  title TEXT NOT NULL                        -- '🎵 Музыка'
);

CREATE TABLE IF NOT EXISTS subtopics(
  topic_key TEXT NOT NULL REFERENCES topics(key) ON DELETE CASCADE,
  id        TEXT NOT NULL,                   -- 'rap', 'kpop', 'lvl2', ...
  grp       TEXT NOT NULL,                   -- 'Жанры', 'Платформа', 'Уровень'
  title     TEXT NOT NULL,
  PRIMARY KEY (topic_key, id)                -- 'drama' есть и у кино, и у сериалов
);

-- Пользователи -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS users(
  uid        INTEGER PRIMARY KEY,            -- внутренний id пользователя
  email      TEXT UNIQUE COLLATE NOCASE,
  password_hash TEXT,
  display_name TEXT,
  bio        TEXT,
  avatar     TEXT,
  telegram_id INTEGER UNIQUE,
  lang       TEXT    NOT NULL DEFAULT 'auto',
  push       INTEGER NOT NULL DEFAULT 1 CHECK (push IN (0,1)),
  created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Чаты -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chats(
  id         INTEGER PRIMARY KEY,
  owner      INTEGER NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  type       TEXT    NOT NULL CHECK (type IN ('AI','HUMAN','CLOSED')),
  peer       INTEGER REFERENCES chats(id) ON DELETE SET NULL
                     DEFERRABLE INITIALLY DEFERRED,   -- чат собеседника
  avatar     TEXT    NOT NULL,
  num        INTEGER NOT NULL,               -- «Чат 3» в интерфейсе
  unread     INTEGER NOT NULL DEFAULT 0 CHECK (unread >= 0),
  topic      TEXT    REFERENCES topics(key),
  n          INTEGER NOT NULL DEFAULT 0,     -- сколько сообщений до поиска пары
  created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chats_owner ON chats(owner);

-- Выбранные подтемы чата (связь многие-ко-многим вместо JSON в колонке)
CREATE TABLE IF NOT EXISTS chat_subs(
  chat_id   INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  topic_key TEXT    NOT NULL,
  sub_id    TEXT    NOT NULL,
  PRIMARY KEY (chat_id, sub_id),
  FOREIGN KEY (topic_key, sub_id) REFERENCES subtopics(topic_key, id)
);

-- Сообщения --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  role       TEXT    NOT NULL CHECK (role IN ('user','assistant','me','partner')),
  content    TEXT    NOT NULL,
  created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);

-- История мэтчей (раньше нигде не хранилась) -----------------------------
CREATE TABLE IF NOT EXISTS matches(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_a     INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  chat_b     INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  topic      TEXT    REFERENCES topics(key),
  matched_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (chat_a <> chat_b)
);

CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  expires_at INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
