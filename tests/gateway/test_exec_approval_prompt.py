from gateway.run import _format_exec_approval_fallback_prompt


def test_exec_approval_fallback_includes_command_by_default():
    msg = _format_exec_approval_fallback_prompt(
        command="rm -rf /tmp/example",
        description="destructive command",
        typed_command_prefix="!",
    )

    assert "```" in msg
    assert "rm -rf /tmp/example" in msg
    assert "Reason: destructive command" in msg
    assert "`!approve`" in msg
    assert "`!deny`" in msg


def test_exec_approval_fallback_omits_command_when_tool_progress_is_off():
    msg = _format_exec_approval_fallback_prompt(
        command="curl -H 'Authorization: Bearer REDACTED' https://example.test",
        description="network command",
        include_command=False,
    )

    assert "Dangerous command requires approval" in msg
    assert "Reason: network command" in msg
    assert "```" not in msg
    assert "curl" not in msg
    assert "Authorization" not in msg
    assert "`/approve`" in msg
    assert "`/deny`" in msg


def test_exec_approval_fallback_truncates_long_command_preview():
    msg = _format_exec_approval_fallback_prompt(
        command="x" * 240,
        description="long command",
    )

    assert ("x" * 200) + "..." in msg
    assert "x" * 240 not in msg
