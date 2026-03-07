#!/usr/bin/env python3
"""
auth_setup.py — One-time OAuth setup for google-docs-mcp
=========================================================
Run this if you're NOT using gog CLI for auth. Opens a browser for
Google OAuth consent, then saves a token file for the MCP server.

Usage:
    python3 auth_setup.py --credentials ~/credentials.json --out ~/.google_docs_token.json

Get credentials.json from:
    https://console.cloud.google.com/ → APIs & Services → Credentials
    → Create OAuth 2.0 Client ID → Desktop App → Download JSON
"""

import argparse
import json
import sys
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]


def main():
    parser = argparse.ArgumentParser(description="Google OAuth token setup for google-docs-mcp")
    parser.add_argument("--credentials", required=True, help="Path to credentials JSON from Google Cloud Console")
    parser.add_argument("--out", required=True, help="Output path for token file")
    args = parser.parse_args()

    creds_path = Path(args.credentials).expanduser()
    out_path = Path(args.out).expanduser()

    if not creds_path.exists():
        print(f"Error: credentials file not found: {creds_path}", file=sys.stderr)
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("Missing dependencies. Run: pip install google-auth-oauthlib", file=sys.stderr)
        sys.exit(1)

    creds_data = json.loads(creds_path.read_text())

    # Wrap in the expected format if bare
    if "installed" not in creds_data and "web" not in creds_data:
        creds_data = {"installed": creds_data}

    flow = InstalledAppFlow.from_client_config(creds_data, SCOPES)
    creds = flow.run_local_server(port=0)

    # Save in gog-compatible format
    token_data = {
        "email": "",  # will be filled after first use
        "client": "default",
        "services": ["docs", "drive"],
        "scopes": SCOPES,
        "refresh_token": creds.refresh_token,
        "_client_id": creds.client_id,
        "_client_secret": creds.client_secret,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(token_data, indent=2))
    out_path.chmod(0o600)

    # Also save credentials.json alongside token for docs_edit.py
    creds_out = out_path.parent / "credentials.json"
    if "installed" in creds_data:
        raw = creds_data["installed"]
    else:
        raw = creds_data.get("web", creds_data)

    creds_out.write_text(json.dumps({
        "client_id": raw.get("client_id", creds.client_id),
        "client_secret": raw.get("client_secret", creds.client_secret),
    }, indent=2))
    creds_out.chmod(0o600)

    print(f"✓ Token saved to: {out_path}")
    print(f"✓ Credentials saved to: {creds_out}")
    print()
    print("Set these environment variables:")
    print(f"  export GOOGLE_DOCS_TOKEN_FILE={out_path}")
    print()
    print("If using non-gog credentials.json:")
    print(f"  # Edit docs_edit.py: GOG_CREDENTIALS_PATH = Path(\"{creds_out}\")")


if __name__ == "__main__":
    main()
