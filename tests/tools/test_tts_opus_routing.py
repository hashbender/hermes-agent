import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from gateway.session_context import _UNSET, _VAR_MAP
from tools import tts_tool


def _reset_session_context() -> None:
    for var in _VAR_MAP.values():
        var.set(_UNSET)


@pytest.fixture(autouse=True)
def _clean_session_platform(monkeypatch):
    _reset_session_context()
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    yield
    _reset_session_context()


async def _write_edge_output(_text: str, output_path: str, _tts_config: dict) -> str:
    Path(output_path).write_bytes(b"mp3")
    return output_path


class _FakeEdgeTTS:
    saved_paths: list[str] = []

    class Communicate:
        def __init__(self, text: str, **kwargs):
            self.text = text
            self.kwargs = kwargs

        async def save(self, output_path: str) -> None:
            _FakeEdgeTTS.saved_paths.append(output_path)
            Path(output_path).write_bytes(b"mp3")


def test_edge_cli_preserves_native_mp3(tmp_path, monkeypatch):
    out = tmp_path / "speech.mp3"
    convert = Mock()

    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "edge"})
    monkeypatch.setattr(tts_tool, "_import_edge_tts", lambda: object())
    monkeypatch.setattr(tts_tool, "_generate_edge_tts", _write_edge_output)
    monkeypatch.setattr(tts_tool, "_convert_to_opus", convert)

    result = json.loads(tts_tool.text_to_speech_tool("hello", output_path=str(out)))

    assert result["success"] is True
    assert result["file_path"] == str(out)
    assert result["voice_compatible"] is False
    assert result["media_tag"] == f"MEDIA:{out}"
    convert.assert_not_called()


def test_edge_telegram_converts_to_opus_voice(tmp_path, monkeypatch):
    out = tmp_path / "speech.mp3"
    opus = tmp_path / "speech.ogg"

    def fake_convert(path: str) -> str:
        assert path == str(out)
        opus.write_bytes(b"ogg")
        return str(opus)

    convert = Mock(side_effect=fake_convert)

    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "edge"})
    monkeypatch.setattr(tts_tool, "_import_edge_tts", lambda: object())
    monkeypatch.setattr(tts_tool, "_generate_edge_tts", _write_edge_output)
    monkeypatch.setattr(tts_tool, "_convert_to_opus", convert)

    result = json.loads(tts_tool.text_to_speech_tool("hello", output_path=str(out)))

    assert result["success"] is True
    assert result["file_path"] == str(opus)
    assert result["voice_compatible"] is True
    assert result["media_tag"] == f"[[audio_as_voice]]\nMEDIA:{opus}"
    convert.assert_called_once_with(str(out))


@pytest.mark.asyncio
async def test_edge_explicit_ogg_writes_real_opus_not_mp3_bytes(tmp_path, monkeypatch):
    out = tmp_path / "speech.ogg"
    mp3 = tmp_path / "speech.mp3"
    _FakeEdgeTTS.saved_paths = []

    def fake_convert(path: str) -> str:
        assert path == str(mp3)
        assert mp3.read_bytes() == b"mp3"
        out.write_bytes(b"ogg")
        return str(out)

    monkeypatch.setattr(tts_tool, "_import_edge_tts", lambda: _FakeEdgeTTS)
    monkeypatch.setattr(tts_tool, "_convert_to_opus", fake_convert)

    result = await tts_tool._generate_edge_tts("hello", str(out), {})

    assert result == str(out)
    assert out.read_bytes() == b"ogg"
    assert not mp3.exists()
    assert _FakeEdgeTTS.saved_paths == [str(mp3)]


def test_edge_explicit_ogg_is_voice_compatible(tmp_path, monkeypatch):
    out = tmp_path / "speech.ogg"

    async def write_ogg(_text: str, output_path: str, _tts_config: dict) -> str:
        Path(output_path).write_bytes(b"ogg")
        return output_path

    convert = Mock()
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "edge"})
    monkeypatch.setattr(tts_tool, "_import_edge_tts", lambda: object())
    monkeypatch.setattr(tts_tool, "_generate_edge_tts", write_ogg)
    monkeypatch.setattr(tts_tool, "_convert_to_opus", convert)

    result = json.loads(tts_tool.text_to_speech_tool("hello", output_path=str(out)))

    assert result["success"] is True
    assert result["file_path"] == str(out)
    assert result["voice_compatible"] is True
    assert result["media_tag"] == f"[[audio_as_voice]]\nMEDIA:{out}"
    convert.assert_not_called()
