"""Helpers for reading Tenki CLI configuration without exposing secrets."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from utils import fast_safe_load

TENKI_DEFAULT_API_ENDPOINT = "https://api.tenki.cloud"

_SECRET_KEYS = frozenset({
    "auth_token",
    "api_key",
    "access_token",
    "session_token",
    "token",
})

_SDK_AUTH_PREFIXES = ("cookie:", "ory_st_", "sk-")


def tenki_cli_config_path() -> Path:
    """Return the Tenki CLI config path.

    ``TENKI_CONFIG_PATH`` is honored for tests and uncommon CLI installs.
    """
    override = os.getenv("TENKI_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "tenki" / "config.yaml"


def load_tenki_cli_config() -> dict[str, Any]:
    """Load Tenki CLI config, returning ``{}`` on missing or invalid files."""
    path = tenki_cli_config_path()
    try:
        data = fast_safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first_string(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _string(data.get(key))
        if value:
            return value
    return ""


def _normalize_cli_auth_token(secret: str, key: str = "") -> str:
    """Return a Tenki SDK-compatible auth token from Tenki CLI config.

    Tenki CLI v0.6 stores its browser session cookie as a bare ``auth_token``.
    The Python SDK expects cookie credentials to be prefixed with ``cookie:``;
    otherwise it sends the value as a bearer token and the API returns
    ``sandbox: unauthorized``.
    """
    secret = _string(secret)
    if not secret or secret.startswith(_SDK_AUTH_PREFIXES):
        return secret
    if key.lower() == "auth_token":
        return f"cookie:{secret}"
    return secret


def _find_secret_value(data: Any) -> str:
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(key, str) and key.lower() in _SECRET_KEYS:
                secret = _string(value)
                if secret:
                    return _normalize_cli_auth_token(secret, key)
            found = _find_secret_value(value)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_secret_value(item)
            if found:
                return found
    return ""


def resolve_tenki_api_endpoint(explicit: str = "") -> str:
    """Resolve the Tenki API endpoint from config/env/CLI defaults."""
    explicit = _string(explicit)
    if explicit:
        return explicit
    for env_name in ("TENKI_API_ENDPOINT", "TENKI_API_URL"):
        value = _string(os.getenv(env_name))
        if value:
            return value
    cfg = load_tenki_cli_config()
    return _first_string(cfg, ("api_endpoint", "api_url", "endpoint")) or TENKI_DEFAULT_API_ENDPOINT


def resolve_tenki_workspace_id(explicit: str = "") -> str:
    explicit = _string(explicit)
    if explicit:
        return explicit
    for env_name in ("TENKI_WORKSPACE_ID", "TENKI_WORKSPACE"):
        value = _string(os.getenv(env_name))
        if value:
            return value
    return _first_string(load_tenki_cli_config(), ("current_workspace_id", "workspace_id", "workspace"))


def resolve_tenki_project_id(explicit: str = "") -> str:
    explicit = _string(explicit)
    if explicit:
        return explicit
    for env_name in ("TENKI_PROJECT_ID", "TENKI_PROJECT"):
        value = _string(os.getenv(env_name))
        if value:
            return value
    return _first_string(load_tenki_cli_config(), ("current_project_id", "project_id", "project"))


def resolve_tenki_auth_token(explicit: str = "") -> str:
    """Resolve a Tenki auth token/API key without logging or persisting it."""
    explicit = _string(explicit)
    if explicit:
        return explicit
    for env_name in ("TENKI_AUTH_TOKEN", "TENKI_API_KEY"):
        value = _string(os.getenv(env_name))
        if value:
            return value
    return _find_secret_value(load_tenki_cli_config())


def has_tenki_auth() -> bool:
    return bool(resolve_tenki_auth_token())
