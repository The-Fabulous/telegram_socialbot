import asyncio
import html
import os
import random

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден. Проверь файл .env")

from gpt import SocialBridgeAI

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
ai = SocialBridgeAI()

chats = {}
user_chats = {}
user_active = {}
user_states = {}
captcha_answers = {}
waiting_pool = {}  # chat_id -> {"topic": str, "interests": str}
_next_chat_id = 0

match_lock = asyncio.Lock()
MAX_CHATS = 10
AVATARS = ["🦊", "🐼", "🦉", "🐯", "🐸", "🦄", "🐙", "🐧", "🦁", "🐨", "🐺", "🦋"]

RESET_BUTTON = "🔄 Сбросить диалог"
CHATS_BUTTON = "📂 Мои чаты"
LIST_TITLE = "📂 Твои чаты"

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=CHATS_BUTTON), KeyboardButton(text=RESET_BUTTON)]],
    resize_keyboard=True,
)


def new_chat(user_id: int, ctype: str = "AI") -> int:
    global _next_chat_id
    _next_chat_id += 1
    cid = _next_chat_id
    user_chats.setdefault(user_id, [])
    chats[cid] = {
        "id": cid,
        "owner": user_id,
        "type": ctype,
        "peer": None,
        "avatar": "🤖",
        "num": len(user_chats[user_id]) + 1,
        "history": [],
        "unread": 0,
    }
    user_chats[user_id].append(cid)
    return cid


def title(c: dict) -> str:
    base = f"Чат {c['num']}"
    if c["type"] == "AI":
        return f"{base} 🤖 ИИ"
    if c["type"] == "HUMAN":
        return f"{base} {c['avatar']} Собеседник"
    return f"{base} 🔒 {c['avatar']}"


def ensure_ai_chat(user_id: int) -> int:
    for cid in user_chats.get(user_id, []):
        if chats[cid]["type"] == "AI":
            return cid
    return new_chat(user_id, "AI")


def chats_markup(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    active = user_active.get(user_id)
    for cid in user_chats.get(user_id, []):
        c = chats[cid]
        mark = "▶️ " if cid == active else ""
        unread = f"  🔴 {c['unread']}" if c["unread"] else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{title(c)}{unread}", callback_data=f"open:{cid}")])
    rows.append([InlineKeyboardButton(text="➕ Новый чат с ИИ", callback_data="new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def open_button(cid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Открыть {title(chats[cid])}", callback_data=f"open:{cid}")]
    ])


def render_history(c: dict, limit: int = 5) -> str:
    lines = []
    for m in c["history"][-limit:]:
        role = m["role"]
        who = "Ты" if role in ("user", "me") else (c["avatar"] if role == "partner" else "🤖")
        lines.append(f"<b>{who}:</b> {html.escape(m['content'])}")
    return "\n".join(lines) if lines else "<i>Пока пусто — напиши что-нибудь.</i>"


async def safe_send(chat_id: int, text: str, **kwargs):
    try:
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as e:
        print(f"[ERROR] Не удалось отправить сообщение в {chat_id}: {e}")
        return None


async def show_chats(user_id: int, chat_id: int):
    await safe_send(chat_id, f"{LIST_TITLE}\nВыбери, куда переключиться:", reply_markup=chats_markup(user_id))


async def start_ai_chat(user_id: int, tg_chat_id: int, user_name: str, header: str = ""):
    cid = ensure_ai_chat(user_id) if not header.startswith("➕") else new_chat(user_id, "AI")
    user_active[user_id] = cid
    await bot.send_chat_action(chat_id=tg_chat_id, action="typing")
    welcome = await ai.generate_welcome(user_name=user_name, user_id=user_id, chat_id=tg_chat_id)
    chats[cid]["history"].append({"role": "assistant", "content": welcome})
    prefix = f"{header}\n\n" if header else ""
    await safe_send(tg_chat_id, f"{prefix}{html.escape(welcome)}", reply_markup=main_keyboard)


async def close_human_chat(cid: int, partner_text: str):
    c = chats[cid]
    c["type"] = "CLOSED"
    waiting_pool.pop(cid, None)

    pc = chats.get(c["peer"]) if c["peer"] else None
    if pc and pc["type"] == "HUMAN":
        pc["type"] = "CLOSED"
        pc["unread"] = 0
        p = pc["owner"]
        note = f"{partner_text}\n🔒 <b>{title(pc)}</b> завершён."
        if user_active.get(p) == pc["id"]:
            user_active[p] = ensure_ai_chat(p)
            note += f"\nТы переключён в <b>{title(chats[user_active[p]])}</b>."
        await safe_send(p, note, reply_markup=main_keyboard)


def generate_captcha(user_id: int) -> str:
    num1, num2 = random.randint(1, 10), random.randint(1, 10)
    operator = random.choice(["+", "-"])
    if operator == "+":
        answer = num1 + num2
    else:
        if num1 < num2:
            num1, num2 = num2, num1
        answer = num1 - num2
    captcha_answers[user_id] = answer
    return f"{num1} {operator} {num2}"


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    for cid in list(user_chats.get(user_id, [])):
        if chats[cid]["type"] == "HUMAN":
            await close_human_chat(cid, "Собеседник покинул диалог.")
        waiting_pool.pop(cid, None)
        chats.pop(cid, None)
    user_chats[user_id] = []
    user_active.pop(user_id, None)
    user_states[user_id] = "CAPTCHA"

    expr = generate_captcha(user_id)
    await message.answer(
        "🤖 <b>Проверка на робота</b>\n\nРеши пример:\n👉 <code>{} = ?</code>".format(expr),
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(Command("chats"))
@dp.message(F.text == CHATS_BUTTON)
async def cmd_chats(message: types.Message):
    user_id = message.from_user.id
    if user_states.get(user_id) != "READY":
        await message.answer("Сначала пройди проверку: /start")
        return
    await show_chats(user_id, message.chat.id)


@dp.message(Command("reset"))
@dp.message(F.text == RESET_BUTTON)
async def cmd_reset(message: types.Message):
    user_id = message.from_user.id
    if user_states.get(user_id) != "READY":
        await message.answer("Сначала пройди проверку: /start")
        return

    c = chats[user_active[user_id]]
    if c["type"] == "HUMAN":
        label = title(c)
        await close_human_chat(c["id"], "Собеседник завершил диалог.")
        user_active[user_id] = ensure_ai_chat(user_id)
        await message.answer(f"🔒 <b>{label}</b> завершён.", reply_markup=main_keyboard)
    elif c["type"] == "AI":
        c["history"] = []
        waiting_pool.pop(c["id"], None)
        await message.answer(f"🧹 <b>{title(c)}</b> очищен.", reply_markup=main_keyboard)
    else:
        user_active[user_id] = ensure_ai_chat(user_id)
        await message.answer("Переключил тебя на ИИ.", reply_markup=main_keyboard)


@dp.callback_query(F.data.startswith("open:"))
async def cb_open(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    cid = int(callback.data.split(":")[1])
    if cid not in user_chats.get(user_id, []):
        await callback.answer("Чат не найден", show_alert=True)
        return

    c = chats[cid]
    user_active[user_id] = cid
    c["unread"] = 0
    await safe_send(callback.message.chat.id, f"💬 <b>{title(c)}</b>\n\n{render_history(c)}", reply_markup=main_keyboard)
    await callback.answer()


@dp.callback_query(F.data == "new")
async def cb_new(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_states.get(user_id) != "READY":
        await callback.answer("Сначала /start", show_alert=True)
        return
    if len(user_chats.get(user_id, [])) >= MAX_CHATS:
        await callback.answer("Лимит чатов исчерпан.", show_alert=True)
        return

    await callback.answer()
    name = callback.from_user.first_name or "друг"
    await start_ai_chat(user_id, callback.message.chat.id, name, header="➕ <b>Новый чат создан</b>")


async def try_match(c: dict, chat_id: int):
    uid, cid = c["owner"], c["id"]

    # ⚠️ КРИТИЧЕСКИ ВАЖНО: вызываем extract_interests, чтобы получить СЛОВАРЬ {"topic": ..., "interests": ...}
    info_dict = await ai.extract_interests(user_messages=c["history"], user_id=uid, chat_id=chat_id)

    async with match_lock:
        if c["type"] != "AI":
            return False

        waiting_pool[cid] = info_dict  # Сохраняем словарь

        candidates = {
            k: v for k, v in waiting_pool.items()
            if k in chats and chats[k]["type"] == "AI" and (k == cid or chats[k]["owner"] != uid)
        }
        if len(candidates) < 2:
            return False

        best = await ai.find_best_match(target_user_id=cid, waiting_candidates=candidates, chat_id=chat_id)
        if not best or best not in candidates or best == cid:
            return False

        pc = chats[best]
        if pc["type"] != "AI" or c["type"] != "AI":
            return False

        c["type"], pc["type"] = "HUMAN", "HUMAN"
        c["peer"], pc["peer"] = best, cid
        c["avatar"], pc["avatar"] = random.sample(AVATARS, 2)
        c["history"], pc["history"] = [], []
        c["unread"] = pc["unread"] = 0
        waiting_pool.pop(cid, None)
        waiting_pool.pop(best, None)

        new_chat(uid, "AI")
        new_chat(pc["owner"], "AI")

        print(f"[LOG] 🔀 МЭТЧ: чат {cid} <-> чат {best}")

        for chat_obj in (c, pc):
            text = "🔀 <b>Нашёлся собеседник! Общайтесь.</b>"
            owner = chat_obj["owner"]
            if user_active.get(owner) == chat_obj["id"]:
                await safe_send(owner, text, reply_markup=main_keyboard)
            else:
                await safe_send(owner, text, reply_markup=open_button(chat_obj["id"]))
        return True


@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text.strip() if message.text else ""

    if user_id not in user_states:
        user_states[user_id] = "CAPTCHA"
        user_chats.setdefault(user_id, [])
        expr = generate_captcha(user_id)
        await message.answer(f"Реши пример: <code>{expr} = ?</code>", reply_markup=ReplyKeyboardRemove())
        return

    if user_states[user_id] == "CAPTCHA":
        expected = captcha_answers.get(user_id)
        try:
            answer = int(text)
        except ValueError:
            await message.answer("⚠️ Введи ответ числом.")
            return

        if answer == expected:
            user_states[user_id] = "READY"
            captcha_answers.pop(user_id, None)
            name = message.from_user.first_name or "друг"
            await start_ai_chat(user_id, chat_id, name, header="✅ <b>Проверка пройдена!</b>")
        else:
            new_expr = generate_captcha(user_id)
            await message.answer(f"❌ Неправильно. <code>{new_expr} = ?</code>")
        return

    if not text:
        return

    cid = user_active.get(user_id)
    if cid is None or cid not in chats:
        cid = user_active[user_id] = ensure_ai_chat(user_id)
    c = chats[cid]

    if c["type"] == "CLOSED":
        await message.answer("🔒 Чат завершён.", reply_markup=chats_markup(user_id))
        return

    if c["type"] == "HUMAN":
        pc = chats.get(c["peer"])
        if not pc or pc["type"] != "HUMAN":
            c["type"] = "CLOSED"
            user_active[user_id] = ensure_ai_chat(user_id)
            await message.answer("Собеседник вышел.", reply_markup=main_keyboard)
            return

        pc["history"].append({"role": "partner", "content": text})
        c["history"].append({"role": "me", "content": text})

        p = pc["owner"]
        if user_active.get(p) == pc["id"]:
            await safe_send(p, text, parse_mode=None)
        else:
            pc["unread"] += 1
            if pc["unread"] == 1:
                await safe_send(p, f"💬 Новое сообщение", reply_markup=open_button(pc["id"]))
        return

    # AI Чат + Мэтчинг
    c["history"].append({"role": "user", "content": text})
    user_msg_count = sum(1 for m in c["history"] if m["role"] == "user")
    print(f"[LOG] 📊 User {user_id} | {title(c)} | Сообщений: {user_msg_count}/3")

    if user_msg_count >= 3 and cid not in waiting_pool:
        if await try_match(c, chat_id):
            return

    if c["type"] == "AI":
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await ai.get_comfort_response(
            history=c["history"][:-1], user_text=text, user_id=user_id, chat_id=chat_id
        )
        c["history"].append({"role": "assistant", "content": reply})
        if user_active.get(user_id) == cid:
            await message.answer(html.escape(reply), reply_markup=main_keyboard)
        else:
            c["unread"] += 1
            await safe_send(chat_id, f"💬 Ответ ИИ", reply_markup=open_button(cid))


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    print("🤖 Бот успешно запущен и готов к работе!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())