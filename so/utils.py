import requests

  # Example group chat ID for approvals

# def send_telegram_message(message: str):
#     url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
#     payload = {
#         "chat_id": TELEGRAM_CHAT_ID,
#         "text": message,
#         "parse_mode": "Markdown"
#     }
#     try:
#         requests.post(url, data=payload, timeout=10)
#     except requests.exceptions.RequestException as e:
#         print("Telegram error:", e)


import requests
from django.conf import settings

def send_telegram_message(chat_id, text):
    token = settings.TELEGRAM_BOT_TOKEN
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=payload, timeout=5)
    except Exception as e:
        print("Telegram error:", e)