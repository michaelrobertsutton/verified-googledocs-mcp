"""Unit tests for auth.py.

All tests mock the OAuth flow and filesystem. No real credentials required.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from googledocs_mcp import auth as auth_module


@pytest.fixture()
def tmp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect config and token paths to a temp directory."""
    config_dir = tmp_path / "googledocs-mcp"
    config_dir.mkdir()

    monkeypatch.setattr(auth_module, "_CONFIG_DIR", config_dir)
    monkeypatch.setattr(auth_module, "_DEFAULT_CREDENTIALS", config_dir / "credentials.json")
    monkeypatch.setattr(auth_module, "_DEFAULT_TOKEN", config_dir / "token.json")
    return config_dir


class TestGetCredentials:
    def test_raises_when_no_token(self, tmp_config: Path) -> None:
        with pytest.raises(RuntimeError, match="googledocs-mcp auth"):
            auth_module.get_credentials()

    def test_raises_message_names_token_path(self, tmp_config: Path) -> None:
        with pytest.raises(RuntimeError, match="token.json"):
            auth_module.get_credentials()

    def test_valid_token_returned(self, tmp_config: Path) -> None:
        """A valid, non-expired token is returned without any refresh."""
        mock_creds = MagicMock()
        mock_creds.valid = True

        token_path = tmp_config / "token.json"
        token_path.write_text("{}")

        # Patch Credentials at the module level where it is imported.
        with patch("googledocs_mcp.auth.Credentials") as mock_creds_cls:
            mock_creds_cls.from_authorized_user_file.return_value = mock_creds
            result = auth_module.get_credentials()

        assert result is mock_creds

    def test_expired_token_is_refreshed(self, tmp_config: Path) -> None:
        """An expired token with a refresh_token is automatically refreshed."""
        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "refresh-tok"
        mock_creds.to_json.return_value = json.dumps({"refreshed": True})

        token_path = tmp_config / "token.json"
        token_path.write_text("{}")

        with (
            patch("googledocs_mcp.auth.Credentials") as mock_creds_cls,
            patch("googledocs_mcp.auth.Request"),
        ):
            mock_creds_cls.from_authorized_user_file.return_value = mock_creds
            result = auth_module.get_credentials()

        mock_creds.refresh.assert_called_once()
        # Refreshed token is written back to disk.
        assert token_path.read_text() == json.dumps({"refreshed": True})
        assert result is mock_creds

    def test_refresh_failure_raises_with_auth_instruction(
        self, tmp_config: Path
    ) -> None:
        from google.auth.exceptions import RefreshError

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "bad-token"
        mock_creds.refresh.side_effect = RefreshError("invalid_grant")

        token_path = tmp_config / "token.json"
        token_path.write_text("{}")

        with (
            patch("googledocs_mcp.auth.Credentials") as mock_creds_cls,
            patch("googledocs_mcp.auth.Request"),
        ):
            mock_creds_cls.from_authorized_user_file.return_value = mock_creds
            with pytest.raises(RuntimeError, match="googledocs-mcp auth"):
                auth_module.get_credentials()

    def test_invalid_token_no_refresh_token_raises(self, tmp_config: Path) -> None:
        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = False
        mock_creds.refresh_token = None

        token_path = tmp_config / "token.json"
        token_path.write_text("{}")

        with patch("googledocs_mcp.auth.Credentials") as mock_creds_cls:
            mock_creds_cls.from_authorized_user_file.return_value = mock_creds
            with pytest.raises(RuntimeError, match="googledocs-mcp auth"):
                auth_module.get_credentials()


class TestRunAuthFlow:
    def test_missing_credentials_exits(self, tmp_config: Path) -> None:
        # No credentials.json present.
        with pytest.raises(SystemExit, match="credentials.json"):
            auth_module.run_auth_flow()

    def test_credentials_env_var_overrides_path(
        self, tmp_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        custom_path = str(tmp_config / "custom-creds.json")
        monkeypatch.setenv("GOOGLEDOCS_MCP_CREDENTIALS", custom_path)
        with pytest.raises(SystemExit, match="custom-creds.json"):
            auth_module.run_auth_flow()

    def test_flow_saves_token(
        self, tmp_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When credentials.json exists, flow runs and token is saved."""
        cred_path = tmp_config / "credentials.json"
        cred_path.write_text('{"installed": {}}')

        mock_creds = MagicMock()
        mock_creds.to_json.return_value = json.dumps({"access_token": "tok"})

        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_creds

        with patch("googledocs_mcp.auth.InstalledAppFlow") as mock_flow_cls:
            mock_flow_cls.from_client_secrets_file.return_value = mock_flow
            with patch("builtins.print"):
                auth_module.run_auth_flow()

        token_path = tmp_config / "token.json"
        assert token_path.exists()
        assert json.loads(token_path.read_text()) == {"access_token": "tok"}


class TestCredentialsPath:
    def test_default_path_under_config(self, tmp_config: Path) -> None:
        path = auth_module._credentials_path()
        assert path.name == "credentials.json"

    def test_env_var_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLEDOCS_MCP_CREDENTIALS", "/tmp/my-creds.json")
        path = auth_module._credentials_path()
        assert str(path) == "/tmp/my-creds.json"

    def test_env_var_cleared_restores_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GOOGLEDOCS_MCP_CREDENTIALS", raising=False)
        path = auth_module._credentials_path()
        assert path.name == "credentials.json"
