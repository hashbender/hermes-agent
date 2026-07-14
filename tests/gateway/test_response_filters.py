from types import SimpleNamespace

from gateway.response_filters import (
    is_intentional_silence_agent_result,
    is_intentional_silence_response,
    should_drop_empty_inbound_event,
)


def test_exact_silence_tokens_are_intentional_silence():
    for token in ("[SILENT]", " SILENT ", "NO_REPLY", "no reply"):
        assert is_intentional_silence_response(token)


def test_blank_and_prose_mentions_are_not_silence():
    assert not is_intentional_silence_response("")
    assert not is_intentional_silence_response("Use NO_REPLY when no answer is needed.")
    assert not is_intentional_silence_response("The reply was [SILENT], intentionally.")


def test_pure_edit_verifier_receipts_are_suppressed_as_control_plane_noise():
    response = """Ad-hoc verifier passed again.

- Temp script: `/var/folders/wv/ny6041751mv_d_4ksb23nj900000gn/T/hermes-verify-9i3euhkc.py`
- Cleanup: removed

Verified:
- built and served APK SHA/size match

This is **ad-hoc verification** for the repeated guardrail notice."""
    assert is_intentional_silence_response(response)


def test_substantive_updates_with_verification_notes_are_not_suppressed():
    response = """Fixed and served.

Example mobile app v1.2.3 now includes the logo asset fix.

Ad-hoc verification passed using `/var/folders/.../hermes-verify-example.py`; cleanup removed."""
    assert not is_intentional_silence_response(response)


def test_failed_agent_result_never_counts_as_intentional_silence():
    assert is_intentional_silence_agent_result({"failed": False}, "NO_REPLY")
    assert not is_intentional_silence_agent_result({"failed": True}, "NO_REPLY")


def _event(**overrides):
    defaults = {
        "text": "",
        "internal": False,
        "media_urls": [],
        "media_types": [],
        "channel_context": None,
        "reply_to_text": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_empty_text_only_inbound_events_are_dropped_before_agent_dispatch():
    assert should_drop_empty_inbound_event(_event(text=""))
    assert should_drop_empty_inbound_event(_event(text="   \n\t"))
    assert should_drop_empty_inbound_event(
        _event(text="(The user sent a message with no text content)")
    )


def test_non_empty_and_contextual_inbound_events_are_not_dropped():
    assert not should_drop_empty_inbound_event(_event(text="hello"))
    assert not should_drop_empty_inbound_event(_event(text="", internal=True))
    assert not should_drop_empty_inbound_event(_event(text="", media_urls=["/tmp/photo.png"]))
    assert not should_drop_empty_inbound_event(_event(text="", media_types=["image/png"]))
    assert not should_drop_empty_inbound_event(_event(text="", reply_to_text="assistant reply"))
    assert not should_drop_empty_inbound_event(
        _event(
            text="(The user sent a message with no text content)",
            media_urls=["/tmp/photo.png"],
        )
    )


def test_empty_channel_context_only_event_is_dropped():
    assert should_drop_empty_inbound_event(_event(text="", channel_context="prior channel context"))
    assert should_drop_empty_inbound_event(
        _event(
            text="(The user sent a message with no text content)",
            channel_context="prior channel context",
        )
    )
