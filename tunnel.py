import time
import requests

def get_tunnel():
    print("Создаем туннель через бесплатный облачный сервис...")
    try:
        # Используем публичный API для создания быстрого туннеля
        response = requests.get("https://localtunnel.me/?port=8000")
        data = response.json()
        print("\n" + "="*50)
        print(f"ТВОЯ ССЫЛКА ДЛЯ .env: {data.get('url')}")
        print("="*50 + "\n")
    except Exception as e:
        print(f"Ошибка: {e}")
        # Запасной вариант через другой публичный сервис
        try:
            res = requests.get("https://api.ipify.org?format=json")
            print(f"Твой IP: {res.json().get('ip')}")
        except:
            pass

if __name__ == "__main__":
    get_tunnel()
    print("Не закрывай это окно! Нажми Ctrl+C для выхода.")
    while True:
        time.sleep(1)