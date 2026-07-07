from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "productivity"
    / "telephony"
    / "scripts"
    / "telephony.py"
)


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._payload


def _load_telephony_module():
    spec = importlib.util.spec_from_file_location("telephony_script_under_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_json_request_wraps_malformed_success_json():
    telephony = _load_telephony_module()

    with patch.object(telephony.urllib.request, "urlopen", return_value=_FakeResponse(b"<html>nope</html>")):
        with pytest.raises(telephony.TelephonyError, match="Malformed JSON response"):
            telephony._json_request("GET", "https://example.invalid/api")
