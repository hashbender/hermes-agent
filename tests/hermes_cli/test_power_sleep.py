"""Tests for opt-in host sleep prevention while Hermes is running."""

from hermes_cli import power_sleep


def test_prevent_sleep_disabled_by_default():
    assert power_sleep.should_prevent_sleep("gateway", config={}) is False
    assert power_sleep.should_prevent_sleep("desktop", config={"power": {}}) is False


def test_prevent_sleep_enabled_for_configured_surfaces():
    config = {
        "power": {
            "prevent_sleep": {
                "enabled": True,
                "surfaces": ["desktop", "gateway"],
                "mode": "system",
            }
        }
    }

    assert power_sleep.should_prevent_sleep("desktop", config=config) is True
    assert power_sleep.should_prevent_sleep("gateway", config=config) is True
    assert power_sleep.should_prevent_sleep("cron", config=config) is False
    assert power_sleep.prevent_sleep_mode(config=config) == "system"


def test_boolean_shorthand_uses_default_surfaces():
    config = {"power": {"prevent_sleep": True}}

    assert power_sleep.should_prevent_sleep("desktop", config=config) is True
    assert power_sleep.should_prevent_sleep("gateway", config=config) is True
    assert power_sleep.prevent_sleep_mode(config=config) == "system"


def test_invalid_mode_falls_back_to_system():
    config = {"power": {"prevent_sleep": {"enabled": True, "mode": "invalid"}}}

    assert power_sleep.prevent_sleep_mode(config=config) == "system"


def test_windows_execution_state_flags_are_started_and_cleared():
    calls = []

    def fake_set_thread_execution_state(flags):
        calls.append(flags)
        return 0x80000000

    handle = power_sleep.start_prevent_sleep(
        "gateway",
        config={"power": {"prevent_sleep": {"enabled": True, "mode": "system"}}},
        platform="win32",
        set_thread_execution_state=fake_set_thread_execution_state,
    )

    assert handle.started is True
    assert calls == [power_sleep.ES_CONTINUOUS | power_sleep.ES_SYSTEM_REQUIRED]

    handle.stop()
    assert calls[-1] == power_sleep.ES_CONTINUOUS


def test_display_mode_adds_display_required_flag():
    calls = []

    handle = power_sleep.start_prevent_sleep(
        "desktop",
        config={"power": {"prevent_sleep": {"enabled": True, "mode": "display"}}},
        platform="win32",
        set_thread_execution_state=lambda flags: calls.append(flags) or 1,
    )

    assert handle.started is True
    assert calls == [
        power_sleep.ES_CONTINUOUS
        | power_sleep.ES_SYSTEM_REQUIRED
        | power_sleep.ES_DISPLAY_REQUIRED
    ]


def test_non_windows_enabled_config_returns_inactive_handle_without_calling_api():
    calls = []

    handle = power_sleep.start_prevent_sleep(
        "gateway",
        config={"power": {"prevent_sleep": {"enabled": True}}},
        platform="linux",
        set_thread_execution_state=lambda flags: calls.append(flags) or 1,
    )

    assert handle.started is False
    assert calls == []
