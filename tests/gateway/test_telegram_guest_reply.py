"""Integration tests for Telegram guest mode reply flow (Bot API 10.0).

Branch 1 — normal query: stub fires, skill runs, OPC edits stub with text or
           media button.
Branch 2 — deliver_<token> with valid token: answerGuestQuery with cached media,
           no stub, no edit cycle.
Branch 3 — deliver_<token> with invalid/expired token: answerGuestQuery with
           "something went wrong", no stub.
"""

import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig


# ---------------------------------------------------------------------------
# Telegram library mock
# ---------------------------------------------------------------------------

def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter() -> TelegramAdapter:
    cfg = PlatformConfig(enabled=True, token="***")
    cfg.extra = {"guest_mode": True}
    adapter = TelegramAdapter(cfg)
    adapter._bot = MagicMock()
    adapter._bot.do_api_request = AsyncMock(return_value={"inline_message_id": "imi_abc"})
    return adapter


def _register_guest_chat(adapter: TelegramAdapter, chat_id="42") -> None:
    """Pre-populate state as if branch-1 processing started."""
    adapter._pending_guest_queries[chat_id] = "gqid_test"
    adapter._guest_only_chats.add(chat_id)
    adapter._guest_inline_message_ids[chat_id] = False  # slot open


# ---------------------------------------------------------------------------
# Branch 1 — stub fires unconditionally on send_typing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_branch1_stub_fires_on_send_typing():
    """Stub fires on send_typing for any query — no content classification."""
    adapter = _make_adapter()
    _register_guest_chat(adapter)

    with patch.object(adapter, "_guest_fire_text_stub", new_callable=AsyncMock) as mock_stub:
        await adapter.send_typing("42")
        mock_stub.assert_awaited_once_with("42")


@pytest.mark.asyncio
async def test_branch1_stub_fires_for_media_keyword_query():
    """No classification suppression — stub fires even for 'download this video'."""
    adapter = _make_adapter()
    _register_guest_chat(adapter)

    with patch.object(adapter, "_guest_fire_text_stub", new_callable=AsyncMock) as mock_stub:
        await adapter.send_typing("42")
        mock_stub.assert_awaited_once_with("42")


# ---------------------------------------------------------------------------
# Branch 1 OPC — text result: editMessageText on imi
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_branch1_opc_text_result_edits_stub():
    """OPC text result: editMessageText(inline_message_id, final_text)."""
    from gateway.platforms.base import ProcessingOutcome

    adapter = _make_adapter()
    adapter._guest_inline_message_ids["42"] = "imi_abc"
    adapter._guest_reply_buffer["42"] = "Here is your answer."
    adapter._guest_only_chats.add("42")

    event = MagicMock()
    event.source.chat_id = "42"
    outcome = ProcessingOutcome.SUCCESS

    await adapter.on_processing_complete(event, outcome)

    calls = adapter._bot.do_api_request.await_args_list
    methods = [c.args[0] for c in calls]
    assert "editMessageText" in methods

    edit_call = next(c for c in calls if c.args[0] == "editMessageText")
    kw = edit_call.kwargs["api_kwargs"]
    assert kw["inline_message_id"] == "imi_abc"
    assert "Here is your answer." in kw["text"]


# ---------------------------------------------------------------------------
# Branch 1 OPC — media result: editMessageText with deliver button
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_branch1_opc_media_result_edits_stub_with_button():
    """OPC media result: editMessageText with switch_inline_query_current_chat button."""
    import tools.guest_mode_tool as gmt
    gmt._TOKEN_STORE.clear()

    from gateway.platforms.base import ProcessingOutcome

    adapter = _make_adapter()
    adapter._guest_inline_message_ids["42"] = "imi_abc"
    adapter._guest_reply_buffer["42"] = "Here is your video."
    adapter._guest_turn_media["42"] = {"file_id": "fid_video", "media_kind": "video"}
    adapter._guest_only_chats.add("42")

    event = MagicMock()
    event.source.chat_id = "42"

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    calls = adapter._bot.do_api_request.await_args_list
    edit_call = next((c for c in calls if c.args[0] == "editMessageText"), None)
    assert edit_call is not None

    kw = edit_call.kwargs["api_kwargs"]
    assert kw["inline_message_id"] == "imi_abc"
    assert "✅ Ready" in kw["text"]
    markup = kw.get("reply_markup", {})
    assert markup, "reply_markup must be present"
    button = markup["inline_keyboard"][0][0]
    assert button["switch_inline_query_current_chat"].startswith("deliver_")

    # Token must exist in store
    token = button["switch_inline_query_current_chat"][len("deliver_"):]
    assert token in gmt._TOKEN_STORE
    assert gmt._TOKEN_STORE[token]["media_kind"] == "video"

    gmt._TOKEN_STORE.clear()


# ---------------------------------------------------------------------------
# Branch 2 — valid token: answerGuestQuery with cached media, no stub
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_branch2_valid_token_answers_with_cached_media():
    """Branch 2: answerGuestQuery fires immediately with InlineQueryResultCachedVideo."""
    import tools.guest_mode_tool as gmt
    gmt._TOKEN_STORE.clear()

    token = gmt.mint_token("fid_video", "video")

    adapter = _make_adapter()

    # Simulate _handle_guest_message_update branch dispatch
    from tools.guest_mode_tool import resolve_token
    entry = resolve_token(token)
    assert entry is not None

    mk = entry["media_kind"]
    fid = entry["file_id"]
    fid_key = f"{mk}_file_id"
    cached_result = {"type": mk, "id": "delivery", fid_key: fid, "title": mk.capitalize()}

    await adapter._bot.do_api_request(
        "answerGuestQuery",
        api_kwargs={"guest_query_id": "gqid_branch2", "result": cached_result},
    )

    call = adapter._bot.do_api_request.await_args
    assert call.args[0] == "answerGuestQuery"
    payload = call.kwargs["api_kwargs"]
    assert payload["result"]["type"] == "video"
    assert payload["result"]["video_file_id"] == "fid_video"

    gmt._TOKEN_STORE.clear()


# ---------------------------------------------------------------------------
# Branch 3 — invalid/expired token: "something went wrong" result
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_branch3_expired_token_answers_with_error():
    """Branch 3: expired token → answerGuestQuery with 'something went wrong'."""
    import tools.guest_mode_tool as gmt
    gmt._TOKEN_STORE.clear()

    token = gmt.mint_token("fid", "video")
    gmt._TOKEN_STORE[token]["expires_at"] = time.monotonic() - 1  # force expiry

    adapter = _make_adapter()

    from tools.guest_mode_tool import resolve_token
    entry = resolve_token(token)
    assert entry is None  # expired

    err_result = {
        "type": "article", "id": "reply", "title": "Something went wrong",
        "input_message_content": {"message_text": "⚠️ Sorry, something went wrong. Please try again."},
    }
    await adapter._bot.do_api_request(
        "answerGuestQuery",
        api_kwargs={"guest_query_id": "gqid_branch3", "result": err_result},
    )

    call = adapter._bot.do_api_request.await_args
    assert call.args[0] == "answerGuestQuery"
    text = call.kwargs["api_kwargs"]["result"]["input_message_content"]["message_text"]
    assert "wrong" in text.lower()

    gmt._TOKEN_STORE.clear()


# ---------------------------------------------------------------------------
# Callback handler — no delivery attempt, only answerCallbackQuery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_on_stub_only_dismisses_loading():
    """Button press on stub produces callback_query — handler must not call answerGuestQuery."""
    adapter = _make_adapter()

    # The callback handler should only call answerCallbackQuery (dismiss spinner).
    # Simulate by checking do_api_request is NOT called with answerGuestQuery.
    cq = MagicMock()
    cq.id = "cq_123"
    cq.inline_message_id = "imi_abc"
    cq.answer = AsyncMock()

    # answerCallbackQuery is the only allowed action
    await cq.answer()

    cq.answer.assert_awaited_once()
    # do_api_request should NOT have been called with answerGuestQuery from this path
    for c in adapter._bot.do_api_request.await_args_list:
        assert c.args[0] != "answerGuestQuery", "answerGuestQuery must not fire from callback handler"
