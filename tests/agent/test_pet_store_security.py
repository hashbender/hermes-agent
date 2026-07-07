from __future__ import annotations

import base64

import pytest

from agent.pet import store


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class _Response:
    def __init__(self, *, url: str, content: bytes = b"", payload=None):
        self.url = url
        self.content = content
        self._payload = payload if payload is not None else {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload

    def iter_bytes(self):
        yield self.content


class _Stream:
    def __init__(self, response: _Response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, exc_type, exc, tb):
        return False


def test_download_rejects_petdex_redirect_to_non_petdex_host(monkeypatch, tmp_path):
    import httpx

    dest = tmp_path / "sprite.webp"

    def fake_stream(*args, **kwargs):
        return _Stream(_Response(url="http://169.254.169.254/latest/meta-data", content=b"secret"))

    monkeypatch.setattr(httpx, "stream", fake_stream)

    with pytest.raises(store.PetStoreError, match="non-petdex"):
        store._download("https://assets.petdex.dev/sprite.webp", dest, timeout=1)

    assert not dest.exists()


def test_download_json_rejects_petdex_redirect_to_non_petdex_host(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx,
        "get",
        lambda *args, **kwargs: _Response(
            url="http://169.254.169.254/latest/meta-data",
            payload={"leaked": "metadata"},
        ),
    )

    with pytest.raises(store.PetStoreError, match="non-petdex"):
        store._download_json("https://assets.petdex.dev/pet.json", timeout=1)


def test_thumbnail_rejects_petdex_redirect_to_non_petdex_host(monkeypatch, tmp_path):
    import httpx

    monkeypatch.setattr(store, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *args, **kwargs: _Response(
            url="http://169.254.169.254/latest/meta-data",
            content=_PNG_1X1,
        ),
    )

    assert store.thumbnail_png("demo", source_url="https://assets.petdex.dev/sprite.png", timeout=1) is None
    assert not (tmp_path / "pets" / ".thumbs" / "demo.png").exists()
