import requests
import json

# --- CONFIGURATION ---
# Paste the EXACT same token that worked for you in the Graph API Explorer
ACCESS_TOKEN = "EAAQptligo50BPxZCBoRIIoopsfBzTeTBZCOrc1XGa9xC0tGikkpp5ajjM1inCFemKZCtAiRnpWWzZB1kJq6WqgMwL7RZC8MqHynBShSCM7Y3mpm7HfKOhbyTvNCqy5A6QwK43NVfENklGeHFYEAhRVh8aljVeNkPDOGfWZChs6ytvctfBKPZCtlNgA41ZCvDqZB0t"

PHONE_NUMBER_ID = "623707730818076"
RECIPIENT_NUMBER = "971542677947"
TEMPLATE_NAME = "license_expired_utility"
API_VERSION = "v22.0"

# --- DO NOT CHANGE ANYTHING BELOW THIS LINE ---

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
        "language": {
            "code": "en"
        },
        "components": [
            {
                "type": "BODY",
                "parameters": [
                    {"type": "text", "text": "Test Salesman"},
                    {"type": "text", "text": "Test Customer Inc."}
                ]
            }
        ]
    }
}

print("--- Sending Standalone Test Request ---")
print(f"URL: {url}")
print(f"Payload: {json.dumps(payload, indent=2)}")
print("------------------------------------")

try:
    response = requests.post(url, headers=headers, data=json.dumps(payload))
    
    print(f"\n--- Response ---")
    print(f"Status Code: {response.status_code}")
    print(f"Response Body: {response.text}")
    print("----------------")

    if response.status_code == 200:
        print("\nSUCCESS! The message was sent from the standalone script.")
    else:
        print("\nFAILURE! The error persists even outside of Django.")

except requests.exceptions.RequestException as e:
    print(f"\nAn error occurred: {e}")