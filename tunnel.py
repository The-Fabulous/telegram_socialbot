import time
import urllib.request
import json

print("Проверка локального сервера FastAPI на порту 8000...")
try:
    response = urllib.request.urlopen("http://localhost:8000/docs")
    print(" FastAPI сервер успешно запущен и отвечает!")
except Exception as e:
    print(f" ВНИМАНИЕ: FastAPI сервер не отвечает на порту 8000! Ошибка: {e}")
    print("Убедись, что сначала ты запустил свой бот/сервер (python bot.py в другом окне).")

print("\nЧтобы получить ссылку для Telegram Mini App за 1 секунду:")
print("1. Скачай крошечную утилиту bore с официального сайта или используй готовый вариант.")
print("2. Или просто открой в браузере сайт: https://pinggy.io и следуй инструкции.").\tunnel.py