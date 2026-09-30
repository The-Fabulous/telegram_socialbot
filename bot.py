import asyncio
import random
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from dotenv import load_dotenv
from gpt import SocialBridgeAI

load_dotenv()

bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()
ai = SocialBridgeAI()

user_states = {}       # user_id -> 'CAPTCHA' | 'AI_ONLY' | 'MATCHED'
captcha_answers = {}   # user_id -> int
user_history = {}      # user_id -> list of messages
user_pairs = {}        # user_id -> partner_user_id
waiting_pool = {}      # user_id -> интересы пользователя

reset_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🔄 Сбросить диалог")]],
    resize_keyboard=True,
)


def generate_captcha(user_id: int):
    num1 = random.randint(1, 10)
    num2 = random.randint(1, 10)
    operator = random.choice(["+", "-"])

    if operator == "+":
        answer = num1 + num2
    else:
        if num1 < num2:
            num1, num2 = num2, num1
        answer = num1 - num2

    captcha_answers[user_id] = answer
    return f"{num1} {operator} {num2}"


def clear_user_session(user_id: int):
    user_states[user_id] = "CAPTCHA"
    user_history[user_id] = []
    captcha_answers.pop(user_id, None)
    waiting_pool.pop(user_id, None)

    partner_id = user_pairs.pop(user_id, None)
    if partner_id:
        user_pairs.pop(partner_id, None)
        user_states[partner_id] = "AI_ONLY"
        user_history[partner_id] = []
        return partner_id
    return None


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    clear_user_session(user_id)

    captcha_expr = generate_captcha(user_id)

    await message.answer(
        f"🤖 **Проверка на робота**\n\n"
        f"Пожалуйста, реши пример, чтобы продолжить:\n"
        f"👉 `{captcha_expr} = ?`",
        reply_markup=types.ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )


@dp.message(Command("reset"))
@dp.message(lambda msg: msg.text == "🔄 Сбросить диалог")
async def cmd_reset(message: types.Message):
    user_id = message.from_user.id
    partner_id = clear_user_session(user_id)

    if partner_id:
        await bot.send_message(
            chat_id=partner_id,
            text="Собеседник перезапустил диалог. Ты снова общаешься с ИИ-ассистентом.",
            reply_markup=reset_keyboard
        )

    captcha_expr = generate_captcha(user_id)

    await message.answer(
        f"🧹 **Диалог сброшен!**\n\n"
        f"Пройди быструю проверку заново:\n"
        f"👉 `{captcha_expr} = ?`",
        reply_markup=types.ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )


@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    text = message.text.strip()

    if user_id not in user_states:
        user_states[user_id] = "CAPTCHA"
        captcha_expr = generate_captcha(user_id)
        await message.answer(f"Пожалуйста, пройди проверку: `{captcha_expr} = ?`", parse_mode="Markdown")
        return

    state = user_states[user_id]

    # 1. КАПЧА
    if state == "CAPTCHA":
        expected_answer = captcha_answers.get(user_id)

        try:
            user_answer = int(text)
        except ValueError:
            await message.answer("⚠️ Пожалуйста, введи ответ **числом**.")
            return

        if user_answer == expected_answer:
            user_states[user_id] = "AI_ONLY"
            captcha_answers.pop(user_id, None)

            user_name = message.from_user.first_name or message.from_user.username or "друг"

            await bot.send_chat_action(chat_id=message.chat.id, action="typing")
            welcome_message = await ai.generate_welcome(user_name)

            user_history[user_id].append({"role": "assistant", "content": welcome_message})

            await message.answer(
                f"✅ **Проверка пройдена!**\n\n{welcome_message}",
                reply_markup=reset_keyboard,
                parse_mode="Markdown"
            )
        else:
            new_expr = generate_captcha(user_id)
            await message.answer(
                f"❌ **Неправильно.** Попробуй ещё раз:\n👉 `{new_expr} = ?`",
                parse_mode="Markdown"
            )
        return

    # 2. РЕЖИМ MATCHED (Прямая пересылка человеку)
    if state == "MATCHED":
        partner_id = user_pairs.get(user_id)

        is_toxic = await ai.check_toxicity(text)

        if is_toxic:
            clear_user_session(user_id)

            await message.answer(
                "🛡️ **Диалог остановлен системой безопасности.**\n"
                "Ваше сообщение было распознано как недопустимое или агрессивное.",
                reply_markup=reset_keyboard,
                parse_mode="Markdown"
            )

            if partner_id:
                await bot.send_message(
                    chat_id=partner_id,
                    text="🛡️ Диалог был автоматически завершён нашей системой безопасности. "
                         "Ты снова общаешься с ИИ-ассистентом.",
                    reply_markup=reset_keyboard
                )
            return

        if partner_id:
            await bot.send_chat_action(chat_id=partner_id, action="typing")
            await asyncio.sleep(0.8)
            await bot.send_message(chat_id=partner_id, text=text)
        return

    # 3. РЕЖИМ AI_ONLY И ПОИСК СОБЕСЕДНИКА
    user_history[user_id].append({"role": "user", "content": text})

    user_msg_count = len([m for m in user_history[user_id] if m["role"] == "user"])
    print(f"[LOG] 📊 user_id={user_id} отправлено сообщений: {user_msg_count}/3")

    # Порог: 3 сообщения
    if user_msg_count >= 3 and user_id not in waiting_pool:
        print(f"[LOG] ⚡ Достигнут лимит (3 сообщения)! Извлекаем интересы и отправляем в пул ожидания...")
        interests = await ai.extract_interests(user_history[user_id])
        waiting_pool[user_id] = interests

        if len(waiting_pool) >= 2:
            matched_partner_id = await ai.find_best_match(user_id, waiting_pool)

            if matched_partner_id:
                user_pairs[user_id] = matched_partner_id
                user_pairs[matched_partner_id] = user_id

                user_states[user_id] = "MATCHED"
                user_states[matched_partner_id] = "MATCHED"

                waiting_pool.pop(user_id, None)
                waiting_pool.pop(matched_partner_id, None)

                # Консольный лог
                print(f"\n[LOG] 🔀 МЭТЧ УСПЕШЕН! Пользователь {user_id} соединён с {matched_partner_id}\n")

                # Плашка переключения в Telegram
                match_text = (
                    "🔀 **Режим диалога изменен!**\n\n"
                    "Вы успешно переключились на живого человека. "
                    "Все последующие сообщения будут отправляться вашему собеседнику."
                )

                await bot.send_message(chat_id=user_id, text=match_text, parse_mode="Markdown")
                await bot.send_message(chat_id=matched_partner_id, text=match_text, parse_mode="Markdown")
                return

    # Ответ от ИИ, пока второй человек ещё не найден
    if user_states[user_id] == "AI_ONLY":
        print(f"\n[LOG] 🤖 Вызван режим AI_ONLY для user_id={user_id}")
        print(f"[LOG] 📩 Сообщение пользователя: '{text}'")

        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        ai_response = await ai.get_comfort_response(user_history[user_id][:-1], text)

        user_history[user_id].append({"role": "assistant", "content": ai_response})
        await message.answer(ai_response, reply_markup=reset_keyboard)


async def main():
    print("🤖 Бот успешно запущен и готов к работе!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())