import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import _reply_anchor_for_event, _thread_metadata_for_source
from gateway.platforms.webhook import WebhookAdapter


class _FakeRequest:
    def __init__(self, route_name: str, body: dict, headers: dict | None = None):
        self.match_info = {"route_name": route_name}
        self._body = json.dumps(body).encode("utf-8")
        self.headers = headers or {}
        self.content_length = len(self._body)

    async def read(self):
        return self._body


class _CapturingAdapter:
    def __init__(self):
        self.events = []

    async def handle_message(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_synthetic_telegram_webhook_source_does_not_use_delivery_id_as_reply_anchor():
    config = PlatformConfig(
        enabled=True,
        extra={
            "host": "127.0.0.1",
            "routes": {
                "siri": {
                    "secret": "INSECURE_NO_AUTH",
                    "prompt": "{text}",
                    "source_platform": "telegram",
                    "source_chat_id": "1000000001",
                    "source_chat_type": "dm",
                    "source_user_id": "1000000001",
                    "source_user_name": "User",
                }
            },
        },
    )
    adapter = WebhookAdapter(config)
    telegram_adapter = _CapturingAdapter()
    setattr(adapter, "gateway_runner", SimpleNamespace(adapters={Platform.TELEGRAM: telegram_adapter}))

    response = await adapter._handle_webhook(
        _FakeRequest(
            "siri",
            {"text": "Give me the Burning Man itinerary"},
            headers={"X-Request-ID": "1781840547354"},
        )
    )
    assert response.status == 202

    # Let the task created by _handle_webhook run.
    await asyncio.sleep(0)

    assert len(telegram_adapter.events) == 1
    event = telegram_adapter.events[0]
    assert event.source.platform == Platform.TELEGRAM
    assert event.source.chat_id == "1000000001"
    assert event.source.message_id is None
    assert event.message_id is None
    assert _reply_anchor_for_event(event) is None

    metadata = _thread_metadata_for_source(event.source)
    assert metadata is None


@pytest.mark.asyncio
async def test_synthetic_telegram_webhook_audio_payload_routes_as_voice(monkeypatch, tmp_path):
    cached_audio = tmp_path / "shortcut_audio.m4a"

    def fake_cache_audio_from_bytes(data: bytes, ext: str = ".ogg") -> str:
        assert data == b"fake-m4a-bytes"
        assert ext == ".m4a"
        cached_audio.write_bytes(data)
        return str(cached_audio)

    monkeypatch.setattr(
        "gateway.platforms.webhook.cache_audio_from_bytes",
        fake_cache_audio_from_bytes,
    )

    config = PlatformConfig(
        enabled=True,
        extra={
            "host": "127.0.0.1",
            "routes": {
                "siri": {
                    "secret": "INSECURE_NO_AUTH",
                    "prompt": "[Siri relay / {route}]\n\n{message}",
                    "source_platform": "telegram",
                    "source_chat_id": "1000000001",
                    "source_chat_type": "dm",
                    "source_user_id": "1000000001",
                    "source_user_name": "User",
                }
            },
        },
    )
    adapter = WebhookAdapter(config)
    telegram_adapter = _CapturingAdapter()
    setattr(adapter, "gateway_runner", SimpleNamespace(adapters={Platform.TELEGRAM: telegram_adapter}))

    response = await adapter._handle_webhook(
        _FakeRequest(
            "siri",
            {
                "event_type": "siri",
                "message": "",
                "audio_base64": base64.b64encode(b"fake-m4a-bytes").decode("ascii"),
                "audio_mime_type": "audio/mp4",
                "audio_filename": "Shortcut Recording.m4a",
            },
            headers={"X-Request-ID": "shortcut-audio-1"},
        )
    )
    assert response.status == 202

    await asyncio.sleep(0)

    assert len(telegram_adapter.events) == 1
    event = telegram_adapter.events[0]
    assert event.source.platform == Platform.TELEGRAM
    assert event.source.chat_id == "1000000001"
    assert event.source.message_id is None
    assert event.message_id is None
    assert _reply_anchor_for_event(event) is None
    assert event.message_type.value == "voice"
    assert event.media_urls == [str(cached_audio)]
    assert event.media_types == ["audio/mp4"]


@pytest.mark.asyncio
async def test_synthetic_telegram_webhook_image_payload_routes_as_photo(monkeypatch, tmp_path):
    cached_image = tmp_path / "shortcut_screenshot.png"
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"

    def fake_cache_image_from_bytes(data: bytes, ext: str = ".jpg") -> str:
        assert data == png_bytes
        assert ext == ".png"
        cached_image.write_bytes(data)
        return str(cached_image)

    monkeypatch.setattr(
        "gateway.platforms.webhook.cache_image_from_bytes",
        fake_cache_image_from_bytes,
    )

    config = PlatformConfig(
        enabled=True,
        extra={
            "host": "127.0.0.1",
            "routes": {
                "siri-screen-phone": {
                    "secret": "INSECURE_NO_AUTH",
                    "prompt": "[Siri screen relay / phone]\n\n{message}",
                    "source_platform": "telegram",
                    "source_chat_id": "1000000001",
                    "source_chat_type": "dm",
                    "source_user_id": "1000000001",
                    "source_user_name": "User",
                }
            },
        },
    )
    adapter = WebhookAdapter(config)
    telegram_adapter = _CapturingAdapter()
    setattr(adapter, "gateway_runner", SimpleNamespace(adapters={Platform.TELEGRAM: telegram_adapter}))

    response = await adapter._handle_webhook(
        _FakeRequest(
            "siri-screen-phone",
            {
                "event_type": "siri_screen_phone",
                "message": "Act on this screenshot as Todd should.",
                "screenshot_base64": base64.b64encode(png_bytes).decode("ascii"),
                "screenshot_mime_type": "image/png",
                "screenshot_filename": "Screenshot Todd.png",
            },
            headers={"X-Request-ID": "shortcut-screen-1"},
        )
    )
    assert response.status == 202

    await asyncio.sleep(0)

    assert len(telegram_adapter.events) == 1
    event = telegram_adapter.events[0]
    assert event.source.platform == Platform.TELEGRAM
    assert event.source.chat_id == "1000000001"
    assert event.source.message_id is None
    assert event.message_id is None
    assert _reply_anchor_for_event(event) is None
    assert event.message_type.value == "photo"
    assert event.media_urls == [str(cached_image)]
    assert event.media_types == ["image/png"]
