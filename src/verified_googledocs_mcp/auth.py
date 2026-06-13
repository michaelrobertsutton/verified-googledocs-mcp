"""OAuth flow and token cache for verified-googledocs-mcp.

Auth never runs inside the stdio server process. MCP clients spawn the server
headless (no TTY) with tool timeouts that a browser consent flow would blow
through. The `verified-googledocs-mcp auth` entry point runs the flow in a dedicated
terminal session *before* the server is added to client configuration.

Credential and token paths
--------------------------
Client secret:  ~/.config/verified-googledocs-mcp/credentials.json
                Override: env var VERIFIED_GOOGLEDOCS_MCP_CREDENTIALS

Token cache:    ~/.config/verified-googledocs-mcp/token.json
                (auto-refreshed; never commit this file)

Scopes
------
  https://www.googleapis.com/auth/documents
  https://www.googleapis.com/auth/drive
"""

from __future__ import annotations

import os
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES: list[str] = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

_CONFIG_DIR = Path.home() / ".config" / "verified-googledocs-mcp"
_DEFAULT_CREDENTIALS = _CONFIG_DIR / "credentials.json"
_DEFAULT_TOKEN = _CONFIG_DIR / "token.json"


def _credentials_path() -> Path:
    env = os.environ.get("VERIFIED_GOOGLEDOCS_MCP_CREDENTIALS")
    if env:
        return Path(env)
    return _DEFAULT_CREDENTIALS


def _token_path() -> Path:
    return _DEFAULT_TOKEN


def run_auth_flow() -> None:
    """Run the installed-app OAuth flow and cache the resulting token.

    Prints the authorization URL and waits for the user to complete the flow
    in their browser. Saves the token to ~/.config/verified-googledocs-mcp/token.json.

    Exits with a clear error if credentials.json is not found.
    """
    cred_path = _credentials_path()
    if not cred_path.exists():
        raise SystemExit(
            f"Credentials file not found: {cred_path}\n"
            "Download your OAuth client secret from the Google Cloud Console\n"
            f"and save it to {_DEFAULT_CREDENTIALS}\n"
            f"(or set VERIFIED_GOOGLEDOCS_MCP_CREDENTIALS to its path)."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), SCOPES)
    credentials = flow.run_local_server(port=0)

    token_path = _token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json())
    print(f"Token saved to {token_path}")


def get_credentials() -> Credentials:
    """Return valid OAuth credentials, refreshing the token if needed.

    Raises RuntimeError with a clear "run `verified-googledocs-mcp auth` first" message
    when no valid token exists. The server calls this before any tool runs;
    failure here is fast and diagnosed rather than a silent 401 later.
    """
    token_path = _token_path()
    if not token_path.exists():
        raise RuntimeError(
            "No token found. Run `verified-googledocs-mcp auth` to authorize the server, "
            f"then retry. (Expected token at {token_path})"
        )

    credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except RefreshError as exc:
                raise RuntimeError(
                    "Token refresh failed. Run `verified-googledocs-mcp auth` to re-authorize. "
                    f"(Detail: {exc})"
                ) from exc
            # Persist the refreshed token.
            token_path.write_text(credentials.to_json())
        else:
            raise RuntimeError(
                "Stored token is invalid. Run `verified-googledocs-mcp auth` to re-authorize."
            )

    return credentials
