"""
OAuth2 Setup Script for Gmail API

Run this script to authenticate with your Google account and generate
a token.json that includes BOTH gmail.send and gmail.compose scopes.

Usage:
    python scripts/setup_oauth.py

Requires credentials.json in the project root (downloaded from Google
Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client IDs).
"""

import os
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
]

TOKEN_PATH = Path("token.json")
CREDS_PATH = Path("credentials.json")


def main():
    if not CREDS_PATH.exists():
        print("ERROR: credentials.json not found in the project root.")
        print("Download it from Google Cloud Console → OAuth 2.0 Client IDs.")
        return

    # If token exists, check whether it already has both scopes
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
            has_compose = creds.scopes and "https://www.googleapis.com/auth/gmail.compose" in creds.scopes
            if has_compose and creds.valid:
                print("token.json already has all required scopes and is valid. Nothing to do.")
                return
            if has_compose and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                TOKEN_PATH.write_text(creds.to_json())
                print("token.json refreshed successfully.")
                return
            print("Existing token.json needs re-authorization. Deleting and re-authorizing...")
        except Exception:
            print("Existing token.json is invalid or revoked. Deleting and re-authorizing...")
        TOKEN_PATH.unlink(missing_ok=True)

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json())
    print("token.json created with gmail.send + gmail.compose scopes.")
    print("You can now use 'Draft in Gmail' from the bot.")


if __name__ == "__main__":
    main()
