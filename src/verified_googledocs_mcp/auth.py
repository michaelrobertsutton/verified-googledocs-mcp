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

from .verify import ErrorCode, _make_error

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


def _write_token(token_path: Path, data: str) -> None:
    """Write the cached token with owner-only permissions.

    The token file holds a long-lived OAuth refresh token, so it must not be
    readable by other local users. The parent directory is created 0o700 and
    the file is written atomically (temp file chmod 0o600, then renamed) so it
    is never briefly world-readable and a pre-existing loose-permission file is
    replaced rather than appended to.
    """
    token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = token_path.with_name(token_path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, token_path)
    os.chmod(token_path, 0o600)


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
    _write_token(token_path, credentials.to_json())
    print(f"Token saved to {token_path}")


def get_credentials() -> Credentials:
    """Return valid OAuth credentials, refreshing the token if needed.

    Raises VerifyError(AUTH_EXPIRED) — a typed, retryable error envelope — with a
    clear "run `verified-googledocs-mcp auth`" message when no valid token exists,
    a refresh fails, or the stored token is unusable. The diagnostics carry a
    machine-readable ``reason`` (no_token / refresh_failed / invalid_token). The
    server converts this envelope to a ToolError so every tool fails fast with
    AUTH_EXPIRED rather than a silent 401 later or a bare RuntimeError.
    """
    token_path = _token_path()
    if not token_path.exists():
        raise _make_error(
            ErrorCode.AUTH_EXPIRED,
            "No token found. Run `verified-googledocs-mcp auth` to authorize the server, "
            f"then retry. (Expected token at {token_path})",
            {"reason": "no_token", "token_path": str(token_path)},
        )

    credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except RefreshError as exc:
                raise _make_error(
                    ErrorCode.AUTH_EXPIRED,
                    "Token refresh failed. Run `verified-googledocs-mcp auth` to re-authorize. "
                    f"(Detail: {exc})",
                    {"reason": "refresh_failed", "detail": str(exc)},
                ) from exc
            # Persist the refreshed token (owner-only permissions).
            _write_token(token_path, credentials.to_json())
        else:
            raise _make_error(
                ErrorCode.AUTH_EXPIRED,
                "Stored token is invalid. Run `verified-googledocs-mcp auth` to re-authorize.",
                {"reason": "invalid_token"},
            )

    return credentials
