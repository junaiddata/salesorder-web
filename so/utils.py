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



def get_client_ip(request):
    """
    Returns the real client IP, respecting proxies/load balancers.
    """
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        # Can be "client, proxy1, proxy2"
        ip = x_forwarded_for.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip



def label_network(ip_address: str) -> str:
    """
    Very simple example: adjust ranges to match your real network.
    """
    if not ip_address:
        return ""

    # Example patterns – replace with your real subnets
    if ip_address.startswith("10.10."):
        return "DIP Office Network"
    if ip_address.startswith("10.20."):
        return "RAS Office Network"
    if ip_address.startswith("192.168."):
        return "Local LAN / Home"

    return "Public / External Network"



from user_agents import parse as ua_parse

def parse_device_info(user_agent_str: str):
    """
    Takes a raw User-Agent string and returns:
    (device_type, os_name, browser_name)
    """

    if not user_agent_str:
        return ("Unknown", "Unknown", "Unknown")

    ua = ua_parse(user_agent_str)

    # -------------------------
    # DEVICE TYPE
    # -------------------------
    if ua.is_mobile:
        device_type = "Mobile"
    elif ua.is_tablet:
        device_type = "Tablet"
    elif ua.is_pc:
        device_type = "PC"
    elif ua.is_bot:
        device_type = "Bot"
    else:
        device_type = "Other"

    # -------------------------
    # OPERATING SYSTEM
    # -------------------------
    os_name = ua.os.family  # e.g. Windows, Android, macOS
    os_version = ua.os.version_string  # e.g. 10, 14, 13.3
    device_os = f"{os_name} {os_version}".strip()

    # -------------------------
    # BROWSER
    # -------------------------
    browser_name = ua.browser.family  # e.g. Chrome, Safari
    browser_version = ua.browser.version_string  # e.g. 124.0
    device_browser = f"{browser_name} {browser_version}".strip()

    return (device_type, device_os, device_browser)