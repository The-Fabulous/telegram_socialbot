import asyncio
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, WebAppInfo
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip().rstrip("/")

from db import DB  # noqa: E402
from gpt import LANGS, TOPICS, SocialBridgeAI  # noqa: E402
from matching import CATALOG  # noqa: E402

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if BOT_TOKEN else None
dp = Dispatcher()
ai = SocialBridgeAI()
db = DB()
db.seed_catalog(TOPICS, CATALOG)  # справочники тем и подтем -> таблицы topics / subtopics

# ==========================================
# ХРАНИЛИЩЕ: кэш в памяти + запись в SQLite
# ==========================================
# Тип чата: AI — разговор с ИИ, HUMAN — живой собеседник, CLOSED — завершён.
# Только в памяти (не пишутся в БД): ver — версия чата (растёт при сбросе/закрытии/мэтче),
# busy — идёт запрос к ИИ.
chats = {}          # chat_id -> dict
user_chats = {}     # user_id -> [chat_id, ...]
user_settings = {}  # user_id -> {"lang": "auto", "push": True}
user_seen = {}      # user_id -> время последнего запроса из Mini App (чтобы знать, открыто ли приложение)
waiting_pool = {}   # chat_id (AI-чата) -> {"topic", "interests", "subs", "t"}
_next_id = 0
_tasks = set()
match_lock = asyncio.Lock()

MAX_CHATS = 10
ONLINE_WINDOW = 8   # секунд: если приложение опрашивало сервер недавно, считаем пользователя «в сети»
AVATARS = ["🦊", "🐼", "🦉", "🐯", "🐸", "🦄", "🐙", "🐧", "🦁", "🐨", "🐺", "🦋"]
INDEX_FILE = Path(__file__).parent / "webapp" / "index.html"
SESSION_COOKIE = "sb_session"
SESSION_AGE = 60 * 60 * 24 * 30
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, salt_hex, digest_hex = stored.split("$", 2)
        if kind != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def cfg_of(uid: int) -> dict:
    return user_settings.setdefault(uid, {"lang": "auto", "push": True})


def online(uid: int) -> bool:
    return time.time() - user_seen.get(uid, 0) < ONLINE_WINDOW


def restore():
    """Загружает чаты и настройки из БД после перезапуска."""
    global _next_id
    for c in db.load():
        c.update(ver=0, busy=False)
        chats[c["id"]] = c
        user_chats.setdefault(c["owner"], []).append(c["id"])
        _next_id = max(_next_id, c["id"])
    for uid, lang, push in db.load_users():
        user_settings[uid] = {"lang": lang if lang in LANGS else "auto", "push": bool(push)}
    print(f"[DB] Загружено чатов: {len(chats)}, пользователей: {len(user_settings)}")


def spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def save(c: dict):
    db.save_chat(c)


def add_msg(c: dict, role: str, content: str):
    c["history"].append({"role": role, "content": content})
    db.add_message(c["id"], role, content)


def clear_msgs(c: dict):
    c["history"] = []
    db.clear_messages(c["id"])


def new_chat(uid: int) -> int:
    global _next_id
    _next_id += 1
    user_chats.setdefault(uid, [])
    c = {
        "id": _next_id, "owner": uid, "type": "AI", "peer": None, "avatar": "🤖",
        "num": len(user_chats[uid]) + 1, "history": [], "unread": 0, "topic": None, "n": 0, "subs": [],
        "ver": 0, "busy": False,
    }
    chats[_next_id] = c
    user_chats[uid].append(_next_id)
    save(c)
    return _next_id


def own_chat(uid: int, cid):
    c = chats.get(cid) if isinstance(cid, int) else None
    return c if c and c["owner"] == uid else None


async def push(uid: int, text: str):
    """Уведомление в Telegram, только если пользователь не отключил его и приложение сейчас закрыто
    (когда открыто — в самом приложении появляется всплывающая карточка)."""
    telegram_id = db.telegram_id_for_user(uid)
    if not bot or not telegram_id or not WEBAPP_URL.startswith("https://") or not cfg_of(uid)["push"] or online(uid):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть чаты", web_app=WebAppInfo(url=WEBAPP_URL))]])
    try:
        await bot.send_message(telegram_id, text, reply_markup=kb)
    except Exception as e:
        print(f"[ERROR] push {uid}: {e}")


def snapshot(uid: int) -> dict:
    out = []
    for cid in user_chats.get(uid, []):
        c = chats[cid]
        out.append({
            "id": cid, "num": c["num"], "type": c["type"], "avatar": c["avatar"],
            "topic": c["topic"], "unread": c["unread"], "subs": c["subs"], "n": c["n"],
            "messages": [{"me": m["role"] in ("user", "me"), "text": m["content"]} for m in c["history"][-80:]],
        })
    account = db.get_user(uid) or {"id": uid, "email": None, "name": None, "telegram_id": None}
    account.pop("password_hash", None)
    return {"chats": out, "topics": TOPICS, "langs": LANGS, "settings": cfg_of(uid),
            "catalog": CATALOG, "account": account}


# ==========================================
# ЛОГИКА ЧАТОВ
# ==========================================

async def close_human_chat(cid: int, partner_text: str):
    c = chats[cid]
    c["type"] = "CLOSED"
    c["ver"] += 1
    waiting_pool.pop(cid, None)
    save(c)
    pc = chats.get(c["peer"]) if c["peer"] else None
    if pc and pc["type"] == "HUMAN":
        pc["type"], pc["unread"] = "CLOSED", 0
        pc["ver"] += 1
        save(pc)
        await push(pc["owner"], f"{partner_text}\nЧат {pc['num']} завершён.")


async def try_match(c: dict) -> bool:
    """Поиск собеседника. Модель вызывается БЕЗ блокировки, замок держим только на проверках и записи пары."""
    uid, cid, ver = c["owner"], c["id"], c["ver"]
    if cid in waiting_pool:     # уже в очереди — интересы не пересчитываем, время ожидания сохраняем
        interests, t0 = waiting_pool[cid]["interests"], waiting_pool[cid]["t"]
    else:
        interests = await ai.extract_interests(user_messages=c["history"], topic=c["topic"], user_id=uid, chat_id=uid)
        t0 = time.time()

    async with match_lock:
        if c["type"] != "AI" or c["ver"] != ver:
            return False
        waiting_pool[cid] = {"topic": c["topic"], "interests": interests, "subs": list(c["subs"]), "t": t0}
        candidates = {
            k: dict(v) for k, v in waiting_pool.items()
            if k in chats and chats[k]["type"] == "AI" and (k == cid or chats[k]["owner"] != uid)
        }
    if len(candidates) < 2:
        return False

    best = await ai.find_best_match(target_user_id=cid, waiting_candidates=candidates, chat_id=uid)
    if best is None or best == cid:
        return False

    async with match_lock:
        pc = chats.get(best)
        if (not pc or pc["owner"] == uid or pc["type"] != "AI" or c["type"] != "AI" or c["ver"] != ver
                or best not in waiting_pool or cid not in waiting_pool):
            return False  # пока ждали модель, кто-то из двоих уже занят

        c["type"], pc["type"] = "HUMAN", "HUMAN"
        c["peer"], pc["peer"] = best, cid
        c["avatar"], pc["avatar"] = random.sample(AVATARS, 2)
        c["unread"] = pc["unread"] = 0
        c["ver"] += 1
        pc["ver"] += 1
        clear_msgs(c)
        clear_msgs(pc)
        waiting_pool.pop(cid, None)
        waiting_pool.pop(best, None)
        save(c)
        save(pc)
        db.add_match(cid, best, c["topic"])   # история мэтчей
        new_chat(uid)               # у каждого появляется свободный ИИ-чат для следующего поиска
        new_chat(pc["owner"])
        print(f"[LOG] 🔀 МЭТЧ: чат {cid} (user {uid}) <-> чат {best} (user {pc['owner']})")

    txt = f"🔀 Нашёлся собеседник по теме «{TOPICS.get(pc['topic'], '')}»!"
    await push(pc["owner"], f"{txt} Это Чат {pc['num']}.")
    await push(uid, f"{txt} Это Чат {c['num']}.")
    return True


async def moderate(c: dict, text: str):
    """Проверка токсичности в фоне: сообщение уже доставлено, чат закроется, если оно агрессивное."""
    try:
        if await ai.check_toxicity(text=text, user_id=c["owner"], chat_id=c["owner"]) and c["type"] == "HUMAN":
            await close_human_chat(c["id"], "🛡️ Диалог завершён системой безопасности.")
    except Exception as e:
        print(f"[ERROR] moderate: {e}")


async def send_human(c: dict, text: str):
    pc = chats.get(c["peer"])
    if not pc or pc["type"] != "HUMAN":
        c["type"] = "CLOSED"
        c["ver"] += 1
        save(c)
        return {"notice": "Собеседник недоступен, чат завершён."}

    add_msg(c, "me", text)
    add_msg(pc, "partner", text)
    pc["unread"] += 1
    save(pc)
    spawn(moderate(c, text))
    if pc["unread"] == 1:  # первое непрочитанное — напоминаем в Telegram, если приложение закрыто
        await push(pc["owner"], f"💬 Новое сообщение в Чат {pc['num']}")


# ==========================================
# HTTP API ДЛЯ MINI APP
# ==========================================

async def h_state(uid, b):
    c = own_chat(uid, b.get("read"))
    if c and c["unread"]:
        c["unread"] = 0
        save(c)


async def h_new(uid, b):
    if len(user_chats[uid]) >= MAX_CHATS:
        return {"notice": f"Максимум {MAX_CHATS} чатов. Заверши ненужные."}
    new_chat(uid)


async def h_settings(uid, b):
    s = cfg_of(uid)
    if b.get("lang") in LANGS:
        s["lang"] = b["lang"]
    if isinstance(b.get("push"), bool):
        s["push"] = b["push"]
    db.save_user(uid, s["lang"], s["push"])


async def h_profile(uid, b):
    current = db.get_user(uid) or {}
    name = str(b.get("name") or "").strip()[:60]
    bio = str(b.get("bio") or "").strip()[:180]
    avatar = b.get("avatar", current.get("avatar"))
    if not name:
        return {"notice": "Имя не может быть пустым."}
    if avatar is not None:
        if not isinstance(avatar, str) or len(avatar) > 350_000 or not re.match(
                r"^data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+$", avatar):
            return {"notice": "Не удалось сохранить аватар. Выбери PNG, JPEG или WebP."}
    db.update_profile(uid, name, bio, avatar)


async def h_topic(uid, b):
    c, key = own_chat(uid, b.get("chat")), b.get("key")
    if not c or c["type"] != "AI" or c["topic"] or key not in TOPICS or c["busy"]:
        return
    c["busy"] = True
    try:
        c["topic"] = key
        save(c)
        ver = c["ver"]
        res = await ai.start_topic(topic_key=key, lang=cfg_of(uid)["lang"], user_id=uid, chat_id=uid)
        if c["ver"] == ver:
            add_msg(c, "assistant", res["reply"])
    finally:
        c["busy"] = False


async def h_send(uid, b):
    c, text = own_chat(uid, b.get("chat")), str(b.get("text") or "").strip()[:2000]
    if not c or not text or c["type"] == "CLOSED":
        return
    if c["type"] == "HUMAN":
        return await send_human(c, text)
    if c["busy"]:
        return {"notice": "Подожди, ИИ ещё отвечает на предыдущее сообщение."}

    lang = cfg_of(uid)["lang"]
    c["busy"] = True
    try:
        add_msg(c, "user", text)
        ver = c["ver"]

        if not c["topic"]:  # тема ещё не выбрана — пробуем понять её из текста
            res = await ai.start_topic(text=text, lang=lang, user_id=uid, chat_id=uid)
            if c["ver"] != ver:
                return
            if res["topic"]:
                c["topic"] = res["topic"]
                save(c)
            add_msg(c, "assistant", res["reply"])
            return

        c["n"] += 1
        save(c)
        if c["n"] >= 3 and c["id"] not in waiting_pool and await try_match(c):
            return
        if c["type"] != "AI" or c["ver"] != ver:
            return

        reply = await ai.get_comfort_response(history=c["history"][:-1], user_text=text,
                                              user_id=uid, chat_id=uid, lang=lang)
        if c["ver"] != ver:      # чат сбросили/сматчили, пока модель думала — ответ устарел
            return
        if not reply:
            return {"notice": "ИИ сейчас не отвечает. Попробуй ещё раз чуть позже."}
        add_msg(c, "assistant", reply)
    finally:
        c["busy"] = False


async def h_close(uid, b):
    c = own_chat(uid, b.get("chat"))
    if not c:
        return
    if c["type"] == "HUMAN":
        await close_human_chat(c["id"], "Собеседник завершил диалог.")
    elif c["type"] == "AI":
        c["ver"] += 1
        c["topic"], c["n"], c["subs"] = None, 0, []
        clear_msgs(c)
        waiting_pool.pop(c["id"], None)
        save(c)


async def h_subs(uid, b):
    c, sid = own_chat(uid, b.get("chat")), b.get("sub")
    ids = {i for g in CATALOG.get(c["topic"], {}).values() for i in g} if c else set()
    if not c or c["type"] != "AI" or sid not in ids:
        return
    was = sid in c["subs"]
    c["subs"] = [s for s in c["subs"] if s != sid and not (sid.startswith("lvl") and s.startswith("lvl"))]
    if not was:
        c["subs"].append(sid)
    save(c)
    if c["id"] in waiting_pool:
        waiting_pool[c["id"]]["subs"] = list(c["subs"])


async def matchmaker():
    """Раз в 20 секунд перепроверяет тех, кто ждёт: порог подбора со временем снижается."""
    while True:
        await asyncio.sleep(20)
        for cid in list(waiting_pool):
            c = chats.get(cid)
            if c and c["type"] == "AI" and not c["busy"]:
                try:
                    await try_match(c)
                except Exception as e:
                    print(f"[ERROR] matchmaker: {e!r}")


HANDLERS = {"state": h_state, "new": h_new, "topic": h_topic, "send": h_send,
            "close": h_close, "settings": h_settings, "profile": h_profile, "subs": h_subs}


def verify_init_data(init_data: str):
    """Проверка подписи Telegram: возвращает dict пользователя или None."""
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        got_hash = parsed.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        ok = hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got_hash)
        if not ok or time.time() - int(parsed.get("auth_date", 0)) > 86400:
            return None
        return json.loads(parsed["user"])
    except Exception:
        return None


def request_user(request: web.Request) -> dict | None:
    """Поддерживает обычную веб-сессию и старый вход из Telegram Mini App."""
    init_data = request.headers.get("X-Init-Data", "")
    if init_data and BOT_TOKEN:
        telegram_user = verify_init_data(init_data)
        if telegram_user:
            name = " ".join(filter(None, (telegram_user.get("first_name"), telegram_user.get("last_name"))))
            return db.get_or_create_telegram_user(telegram_user["id"], name)
    return db.user_from_session(request.cookies.get(SESSION_COOKIE, ""))


def set_session_cookie(response: web.StreamResponse, token: str, request: web.Request):
    forwarded_https = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_AGE, httponly=True,
                        secure=request.secure or forwarded_https, samesite="Lax", path="/")


async def auth_register(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    email = str(body.get("email") or "").strip().lower()[:254]
    password = str(body.get("password") or "")
    name = str(body.get("name") or "").strip()[:60]
    if not EMAIL_RE.match(email):
        return web.json_response({"error": "Укажи корректный email."}, status=400)
    if len(password) < 8 or len(password) > 128:
        return web.json_response({"error": "Пароль должен содержать от 8 до 128 символов."}, status=400)
    user = db.create_web_user(email, hash_password(password), name or email.split("@", 1)[0])
    if not user:
        return web.json_response({"error": "Аккаунт с таким email уже существует."}, status=409)
    token = db.create_session(user["id"], SESSION_AGE)
    response = web.json_response({"ok": True})
    set_session_cookie(response, token, request)
    return response


async def auth_login(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    email = str(body.get("email") or "").strip().lower()[:254]
    password = str(body.get("password") or "")
    user = db.get_user_by_email(email)
    if not user or not check_password(password, user.get("password_hash") or ""):
        return web.json_response({"error": "Неверный email или пароль."}, status=401)
    token = db.create_session(user["id"], SESSION_AGE)
    response = web.json_response({"ok": True})
    set_session_cookie(response, token, request)
    return response


async def auth_logout(request: web.Request):
    db.delete_session(request.cookies.get(SESSION_COOKIE, ""))
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


async def auth_me(request: web.Request):
    user = request_user(request)
    if not user:
        return web.json_response({"authenticated": False}, status=401)
    safe = {k: user.get(k) for k in ("id", "email", "name", "telegram_id")}
    return web.json_response({"authenticated": True, "account": safe})


def api(fn):
    async def wrapper(request: web.Request):
        user = request_user(request)
        if not user:
            return web.json_response({"error": "auth"}, status=401)
        uid = user["id"]
        user_seen[uid] = time.time()
        if not user_chats.get(uid):
            new_chat(uid)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            extra = await fn(uid, body)
        except Exception as e:
            print(f"[ERROR] {fn.__name__}: {e!r}")
            extra = {"notice": "Что-то пошло не так. Попробуй ещё раз."}
        return web.json_response({**snapshot(uid), **(extra or {})})
    return wrapper


async def index(request: web.Request):
    return web.Response(text=INDEX_FILE.read_text(encoding="utf-8"), content_type="text/html",
                        headers={"Cache-Control": "no-cache"})


# ==========================================
# TELEGRAM-БОТ (только вход в Mini App)
# ==========================================

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not WEBAPP_URL.startswith("https://"):
        await message.answer("Веб-сервис пока не опубликован. Укажи публичный WEBAPP_URL на сервере.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Открыть Social Bridge", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await message.answer(
        "👋 <b>Привет!</b> Все чаты живут в приложении, жми кнопку ниже.\n"
        "Она же всегда доступна слева от поля ввода («Чаты»).",
        reply_markup=kb,
    )


async def main():
    restore()
    db.start()
    spawn(matchmaker())

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/api/auth/register", auth_register)
    app.router.add_post("/api/auth/login", auth_login)
    app.router.add_post("/api/auth/logout", auth_logout)
    app.router.add_post("/api/auth/me", auth_me)
    for name, fn in HANDLERS.items():
        app.router.add_post(f"/api/{name}", api(fn))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()

    print(f"🌐 Веб-сервис запущен на порту {os.getenv('PORT', '8080')}")
    try:
        if bot:
            await bot.delete_webhook(drop_pending_updates=True)
            if WEBAPP_URL.startswith("https://"):
                await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Чаты", web_app=WebAppInfo(url=WEBAPP_URL)))
            print("🤖 Telegram-бот подключён")
            await dp.start_polling(bot)
        else:
            print("ℹ️ BOT_TOKEN не задан: работаем как самостоятельный веб-сервис")
            await asyncio.Event().wait()
    finally:
        await db.stop()   # дописываем в БД всё, что ещё в очереди
        if bot:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
