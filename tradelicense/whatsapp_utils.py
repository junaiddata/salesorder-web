import requests
import json
import re
from django.conf import settings

def send_whatsapp_template_message(to_number, template_name, params):
    """
    Sends a WhatsApp message using a Meta template for numbered placeholders.
    """
    api_url = f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    
    headers = {
        'Authorization': f'Bearer {settings.WHATSAPP_ACCESS_TOKEN}',
        'Content-Type': 'application/json',
    }
    
    clean_to_number = re.sub(r'\D', '', str(to_number))
    parameters_list = [{"type": "text", "text": str(p)} for p in params]

    payload = {
        "messaging_product": "whatsapp",
        "to": clean_to_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {
                "code": "en"  # <<< THE ONLY CHANGE IS HERE
            },
            "components": [
                {
                    "type": "BODY",
                    "parameters": parameters_list
                }
            ]
        }
    }

    # Debugging prints
    print("--- Sending WhatsApp Request ---")
    print(f"URL: {api_url}")
    print(f"Recipient: {clean_to_number}")
    print(f"Template: {template_name}")
    print(f"Payload: {json.dumps(payload, indent=2)}")
    print("-----------------------------")

    try:
        response = requests.post(api_url, headers=headers, data=json.dumps(payload))
        if response.status_code >= 400:
            print(f"Error from Meta API: {response.status_code} - {response.text}")
        response.raise_for_status()
        print(f"Message sent successfully to {clean_to_number}: {response.json()}")
        return True, response.json()
    except requests.exceptions.RequestException as e:
        print(f"Failed to send message to {clean_to_number}: {e}")
        return False, str(e)