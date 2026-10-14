import asyncio
import hashlib
import hmac
import json
import os
import random
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
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден. Проверь файл '.env' (с точкой).")
if not WEBAPP_URL.startswith("https://"):
    raise RuntimeError("WEBAPP_URL должен быть публичным https-адресом, например https://my-app.herokuapp.com")

from db import DB  # noqa: E402
from gpt import LANGS, TOPICS, SocialBridgeAI  # noqa: E402
from matching import CATALOG  # noqa: E402

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
ai = SocialBridgeAI()
db = DB()

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
    if not cfg_of(uid)["push"] or online(uid):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть чаты", web_app=WebAppInfo(url=WEBAPP_URL))]])
    try:
        await bot.send_message(uid, text, reply_markup=kb)
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
    return {"chats": out, "topics": TOPICS, "langs": LANGS, "settings": cfg_of(uid), "catalog": CATALOG}


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
            "close": h_close, "settings": h_settings, "subs": h_subs}


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


def api(fn):
    async def wrapper(request: web.Request):
        user = verify_init_data(request.headers.get("X-Init-Data", ""))
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
    for name, fn in HANDLERS.items():
        app.router.add_post(f"/api/{name}", api(fn))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()

    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Чаты", web_app=WebAppInfo(url=WEBAPP_URL)))
    print("🤖 Бот и Mini App запущены!")
    try:
        await dp.start_polling(bot)
    finally:
        await db.stop()   # дописываем в БД всё, что ещё в очереди


if __name__ == "__main__":
    asyncio.run(main())