"""Tests for the dispatcher Unix-socket client.

The client wraps an asyncio Unix socket with retry + timeout. We
test against a REAL asyncio Unix socket server fixture on tmp_path,
not a mock. The fixture plays the role of the dispatcher: it
accepts one connection, reads one Envelope, and writes back a
configurable response (or hangs to test timeout).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import pytest
import pytest_asyncio

from gateway.dispatcher_client import (
    DispatcherClient,
    DispatcherConnectionError,
    _resolve_default_socket,
)
from gateway.dispatcher_protocol import (
    OP_DISPATCH,
    OP_PING,
    STATUS_BAD_REQUEST,
    STATUS_BUSY,
    STATUS_INTERNAL,
    STATUS_OK,
    Envelope,
    make_request,
)


# --- Fake dispatcher server fixture -------------------------------


class FakeDispatcher:
    """A real asyncio Unix socket server that pretends to be an
    external dispatcher."""

    def __init__(self, socket_path: Path) -> None:
        self._path = str(socket_path)
        self._server: Optional[asyncio.base_events.Server] = None
        self.connections = 0
        self.response_fn = self._default_response

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=self._path
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            Path(self._path).unlink()
        except FileNotFoundError:
            pass

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.connections += 1
        try:
            line = await asyncio.wait_for(
                reader.readuntil(b"\n"), timeout=5.0
            )
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            return
        try:
            req = Envelope.from_jsonl(line)
        except ValueError:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            return
        resp = self.response_fn(req)
        if resp is None:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            return
        writer.write(resp.to_jsonl())
        await writer.drain()
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    @staticmethod
    def _default_response(req: Envelope) -> Envelope:
        if req.op == OP_PING:
            return Envelope(
                request_id=req.request_id,
                op=OP_PING,
                payload={"ts": 0.0},
                status=STATUS_OK,
            )
        return Envelope(
            request_id=req.request_id,
            op=req.op,
            payload={"result": "echo", "echoed_payload": req.payload},
            status=STATUS_OK,
        )


@pytest_asyncio.fixture
async def fake_dispatcher(tmp_path: Path):
    sock = tmp_path / "dispatcher.sock"
    server = FakeDispatcher(sock)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


# --- DispatcherClient tests ---------------------------------------


@pytest.mark.asyncio
async def test_ping_returns_true_on_status_ok(
    fake_dispatcher: FakeDispatcher,
) -> None:
    client = DispatcherClient(socket_path=str(fake_dispatcher._path))
    assert await client.ping() is True
    assert fake_dispatcher.connections == 1


@pytest.mark.asyncio
async def test_dispatch_roundtrip_ping(
    fake_dispatcher: FakeDispatcher,
) -> None:
    client = DispatcherClient(socket_path=str(fake_dispatcher._path))
    req = make_request(OP_PING, {})
    resp = await client.dispatch(req)
    assert resp.status == STATUS_OK
    assert resp.op == OP_PING
    assert "ts" in resp.payload
    assert resp.request_id == req.request_id


@pytest.mark.asyncio
async def test_dispatch_echo_payload(
    fake_dispatcher: FakeDispatcher,
) -> None:
    client = DispatcherClient(socket_path=str(fake_dispatcher._path))
    req = make_request(
        OP_DISPATCH,
        {"source": "wechat", "content": "/echo hello"},
    )
    resp = await client.dispatch(req)
    assert resp.status == STATUS_OK
    assert resp.payload.get("echoed_payload") == {
        "source": "wechat",
        "content": "/echo hello",
    }
    assert resp.request_id == req.request_id


@pytest.mark.asyncio
async def test_dispatch_unreachable_raises(tmp_path: Path) -> None:
    sock = tmp_path / "nope.sock"
    client = DispatcherClient(
        socket_path=str(sock),
        timeout_s=0.2,
        max_retries=1,
    )
    with pytest.raises(DispatcherConnectionError):
        await client.dispatch(make_request(OP_PING, {}))


@pytest.mark.asyncio
async def test_dispatch_propagates_server_status_codes(
    fake_dispatcher: FakeDispatcher,
) -> None:
    def respond(req: Envelope) -> Envelope:
        return Envelope(
            request_id=req.request_id,
            op=req.op,
            payload={"error": "handler raised"},
            status=STATUS_INTERNAL,
        )

    fake_dispatcher.response_fn = respond
    client = DispatcherClient(socket_path=str(fake_dispatcher._path))
    resp = await client.dispatch(make_request(OP_DISPATCH, {"content": "/echo"}))
    assert resp.status == STATUS_INTERNAL
    assert "raised" in resp.payload["error"]


@pytest.mark.asyncio
async def test_dispatch_retries_on_connection_reset(
    fake_dispatcher: FakeDispatcher,
) -> None:
    call_count = {"n": 0}

    def respond(req: Envelope) -> Optional[Envelope]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return Envelope(
            request_id=req.request_id,
            op=req.op,
            payload={"result": "echo", "echoed_payload": req.payload},
            status=STATUS_OK,
        )

    fake_dispatcher.response_fn = respond
    client = DispatcherClient(
        socket_path=str(fake_dispatcher._path),
        max_retries=2,
    )
    resp = await client.dispatch(make_request(OP_PING, {}))
    assert resp.status == STATUS_OK
    assert call_count["n"] >= 2


@pytest.mark.asyncio
async def test_dispatch_timeout_raises(
    fake_dispatcher: FakeDispatcher,
) -> None:
    async def respond_hang(req: Envelope) -> Optional[Envelope]:
        await asyncio.sleep(60)
        return None

    fake_dispatcher.response_fn = respond_hang  # type: ignore[assignment]
    client = DispatcherClient(
        socket_path=str(fake_dispatcher._path),
        timeout_s=0.2,
        max_retries=0,
    )
    with pytest.raises(DispatcherConnectionError):
        await client.dispatch(make_request(OP_PING, {}))


@pytest.mark.asyncio
async def test_close_is_idempotent(
    fake_dispatcher: FakeDispatcher,
) -> None:
    client = DispatcherClient(socket_path=str(fake_dispatcher._path))
    await client.dispatch(make_request(OP_PING, {}))
    await client.close()
    await client.close()


@pytest.mark.asyncio
async def test_context_manager_closes_on_exit(
    fake_dispatcher: FakeDispatcher,
) -> None:
    async with DispatcherClient(
        socket_path=str(fake_dispatcher._path)
    ) as client:
        resp = await client.dispatch(make_request(OP_PING, {}))
        assert resp.status == STATUS_OK
    assert not client.is_connected


def test_default_socket_path_uses_xdg(monkeypatch) -> None:
    """No socket_path and no env var -> XDG runtime dir path."""
    monkeypatch.delenv("DISPATCHER_SOCKET_PATH", raising=False)
    client = DispatcherClient()
    assert client.socket_path == _resolve_default_socket()


def test_env_var_overrides_default(monkeypatch) -> None:
    """DISPATCHER_SOCKET_PATH overrides the default."""
    monkeypatch.setenv("DISPATCHER_SOCKET_PATH", "/tmp/custom.sock")
    client = DispatcherClient()
    assert client.socket_path == "/tmp/custom.sock"


# --- Format helper tests (sync) -----------------------------------


def test_format_status_ok_kind_echo() -> None:
    from gateway.run import GatewayRunner

    resp = Envelope(
        request_id="r1", op=OP_DISPATCH,
        payload={
            "result": "echo",
            "echoed_payload": {"content": "/echo hello"},
        },
        status=STATUS_OK,
    )
    text = GatewayRunner._format_dispatcher_response("echo", resp)
    assert text == "[dispatcher] echo: /echo hello"


def test_format_status_ok_kind_status() -> None:
    from gateway.run import GatewayRunner

    resp = Envelope(
        request_id="r1", op=OP_DISPATCH,
        payload={
            "result": "status",
            "uptime_s": 12.5,
            "handlers": ["echo", "status", "help"],
        },
        status=STATUS_OK,
    )
    text = GatewayRunner._format_dispatcher_response("status", resp)
    assert "alive" in text
    assert "12.5" in text
    assert "3 handlers" in text


def test_format_non_ok_status() -> None:
    """Non-OK status surfaces error string."""
    from gateway.run import GatewayRunner

    resp = Envelope(
        request_id="r1", op=OP_DISPATCH,
        payload={"error": "handler raised"},
        status=STATUS_INTERNAL,
    )
    text = GatewayRunner._format_dispatcher_response("echo", resp)
    assert "failed" in text
    assert "handler raised" in text


# --- Integration: GatewayRunner._forward_to_dispatcher -----------


@pytest.mark.asyncio
async def test_forward_to_dispatcher_via_runner(
    fake_dispatcher: FakeDispatcher,
) -> None:
    from gateway.run import GatewayRunner
    from types import SimpleNamespace

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._dispatcher_client = DispatcherClient(
        socket_path=str(fake_dispatcher._path)
    )

    event = SimpleNamespace(
        source=SimpleNamespace(platform=SimpleNamespace(value="wechat")),
        get_command_args=lambda: " hello world",
    )
    text = await runner._forward_to_dispatcher(event, "echo")
    assert text == "[dispatcher] echo: /echo hello world"


@pytest.mark.asyncio
async def test_forward_returns_none_on_dispatcher_down(
    tmp_path: Path,
) -> None:
    from gateway.run import GatewayRunner
    from types import SimpleNamespace

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._dispatcher_client = DispatcherClient(
        socket_path=str(tmp_path / "nope.sock"),
        timeout_s=0.1,
        max_retries=0,
    )

    event = SimpleNamespace(
        source=SimpleNamespace(platform=SimpleNamespace(value="wechat")),
        get_command_args=lambda: "",
    )
    result = await runner._forward_to_dispatcher(event, "echo")
    assert result is None
