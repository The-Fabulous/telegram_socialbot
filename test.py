import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

key = os.getenv("GEMINI_API_KEY", "").strip()

client = OpenAI(
    api_key=key,
    base_url="https://api.groq.com/openai/v1",
)

model = "qwen/qwen3.8-27b"

print(f"🚀 Пробуем отправить запрос к модель: {model}")

try:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Привет! Ответь на русском языке: ты работаешь?"}],
    )
    print("\n✅ УСПЕХ! Ответ от модели:")
    print(response.choices[0].message.content)
except Exception as e:
    print("\n❌ Ошибка:", e)