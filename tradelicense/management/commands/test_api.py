# in tradelicense/management/commands/test_api.py

from django.core.management.base import BaseCommand
import requests
import json

class Command(BaseCommand):
    help = 'Runs an isolated API call test from within Django'

    def handle(self, *args, **kwargs):
        self.stdout.write("--- Starting Isolated Django Command Test ---")
        
        # --- CONFIGURATION ---
        # We are using the settings from your Django settings.py file
        from django.conf import settings
        ACCESS_TOKEN = settings.WHATSAPP_ACCESS_TOKEN
        
        PHONE_NUMBER_ID = settings.WHATSAPP_PHONE_NUMBER_ID
        API_VERSION = settings.WHATSAPP_API_VERSION
        RECIPIENT_NUMBER = "971542677947"
        TEMPLATE_NAME = "license_expired_utility"

        # --- EXACT WORKING CODE FROM STANDALONE SCRIPT ---
        url = f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages"
        
        headers = {
            'Authorization': f'Bearer {ACCESS_TOKEN}',
            'Content-Type': 'application/json',
        }

        payload = {
            "messaging_product": "whatsapp",
            "to": RECIPIENT_NUMBER,
            "type": "template",
            "template": {
                "name": TEMPLATE_NAME,
                "language": {"code": "en"},
                "components": [{"type": "BODY", "parameters": [
                    {"type": "text", "text": "Django Test Salesman"},
                    {"type": "text", "text": "Django Test Customer"}
                ]}]
            }
        }

        try:
            # We add a print here to be 100% sure we are using the right library
            self.stdout.write(f"Using requests library from: {requests.__file__}")

            response = requests.post(url, headers=headers, data=json.dumps(payload))
            
            self.stdout.write(self.style.SUCCESS("\n--- Response ---"))
            self.stdout.write(f"Status Code: {response.status_code}")
            self.stdout.write(f"Response Body: {response.text}")
            self.stdout.write(self.style.SUCCESS("----------------"))

            if response.status_code == 200:
                self.stdout.write(self.style.SUCCESS("\nSUCCESS! The message was sent from inside Django."))
            else:
                self.stdout.write(self.style.ERROR("\nFAILURE! The error persists even in an isolated command."))

        except requests.exceptions.RequestException as e:
            self.stdout.write(self.style.ERROR(f"\nAn error occurred: {e}"))