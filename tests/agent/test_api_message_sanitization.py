from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _mock_response(content="ok"):
    message = SimpleNamespace(
        content=content,
        tool_calls=None,
        refusal=None,
        reasoning_content=None,
    )
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(
        choices=[choice],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model="test-model",
        id="test-id",
    )


@patch("run_agent.AIAgent._build_system_prompt", return_value="system prompt")
def test_internal_timestamp_metadata_not_sent_to_chat_completions(_mock_sys):
    from run_agent import AIAgent

    captured = {}

    def fake_api_call(self, api_kwargs):
        captured["messages"] = api_kwargs["messages"]
        return _mock_response()

    agent = AIAgent(
        model="test/model",
        api_key="test-key",
        base_url="http://localhost:1234/v1",
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )
    agent.client = MagicMock()

    history = [{"role": "user", "content": "previous", "timestamp": 123.456}]
    with patch.object(AIAgent, "_interruptible_api_call", fake_api_call):
        agent.run_conversation("next", conversation_history=history)

    assert captured["messages"]
    assert all("timestamp" not in msg for msg in captured["messages"])
