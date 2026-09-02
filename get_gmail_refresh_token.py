"""
One-time script: obtain a Gmail OAuth refresh token for the Email Tracking
Agent (emailagent app). Run this ONCE, on a machine with a browser, logged in
as the mailbox that should be polled.

Usage:
    python get_gmail_refresh_token.py

Reads GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET from .env. Prints the refresh
token to paste into .env as GMAIL_REFRESH_TOKEN -- does NOT write .env for
you, so it can't clobber your other settings.
"""
import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']


def main():
    load_dotenv()
    client_id = os.getenv('GOOGLE_CLIENT_ID')
    client_secret = os.getenv('GOOGLE_CLIENT_SECRET')
    if not client_id or not client_secret:
        raise SystemExit('GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not found in .env -- add them first.')

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost:8080/"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    # Fixed port to match the "Authorized redirect URI" (http://localhost:8080/)
    # registered on the Web application OAuth client in Google Cloud Console.
    credentials = flow.run_local_server(port=8080)

    if not credentials.refresh_token:
        raise SystemExit(
            "No refresh token was returned. This usually means you've already "
            "granted access before -- go to https://myaccount.google.com/permissions, "
            "remove access for this app, and run this script again."
        )

    service = build('gmail', 'v1', credentials=credentials, cache_discovery=False)
    profile = service.users().getProfile(userId='me').execute()

    print('\n' + '=' * 70)
    print(f"Authorized mailbox: {profile['emailAddress']}")
    print('=' * 70)
    print('Add this line to .env:\n')
    print(f"GMAIL_REFRESH_TOKEN={credentials.refresh_token}")
    print()


if __name__ == '__main__':
    main()
