"""Regression guard: don't send Anthropic ``thinking`` to Kimi's /coding endpoint.

Kimi's ``api.kimi.com/coding`` endpoint speaks the Anthropic Messages protocol
but has its own thinking semantics.  When ``thinking.enabled`` is present in
the request, Kimi validates the message history and requires every prior
assistant tool-call message to carry OpenAI-style ``reasoning_content``.

The Anthropic path never populates that field, and
``convert_messages_to_anthropic`` strips Anthropic thinking blocks on
third-party endpoints — so after one turn with tool calls the next request
fails with HTTP 400::

    thinking is enabled but reasoning_content is missing in assistant
    tool call message at index N

The guard is scoped narrowly to the ``/coding`` endpoint only.  Non-``/coding``
Kimi endpoints (e.g. ``api.kimi.com/v1``, custom proxied) do not enforce this
restriction and should receive the thinking parameter.  Kimi on the
chat_completions route handles ``thinking`` via ``extra_body`` in
``ChatCompletionsTransport`` (#13503).  See #56727.
"""

from __future__ import annotations

import pytest


class TestKimiCodingSkipsAnthropicThinking:
    """build_anthropic_kwargs must not inject ``thinking`` for Kimi /coding."""

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://api.kimi.com/coding",
            "https://api.kimi.com/coding/v1",
            "https://api.kimi.com/coding/anthropic",
            "https://api.kimi.com/coding/",
        ],
    )
    def test_kimi_coding_endpoint_omits_thinking(self, base_url: str) -> None:
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model="kimi-k2.5",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "medium"},
            base_url=base_url,
        )
        assert "thinking" not in kwargs, (
            "Anthropic thinking must not be sent to Kimi /coding — "
            "endpoint requires reasoning_content on history we don't preserve."
        )
        assert "output_config" not in kwargs

    def test_kimi_coding_with_explicit_disabled_also_omits(self) -> None:
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model="kimi-k2.5",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": False},
            base_url="https://api.kimi.com/coding",
        )
        assert "thinking" not in kwargs

    def test_non_kimi_third_party_still_gets_thinking(self) -> None:
        """MiniMax and other third-party Anthropic endpoints must retain thinking."""
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model="MiniMax-M2.7",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "medium"},
            base_url="https://api.minimax.io/anthropic",
        )
        assert "thinking" in kwargs
        assert kwargs["thinking"]["type"] == "enabled"

    def test_native_anthropic_still_gets_thinking(self) -> None:
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model="claude-sonnet-4-20250514",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "medium"},
            base_url=None,
        )
        assert "thinking" in kwargs

    def test_kimi_root_endpoint_via_anthropic_transport_gets_thinking(self) -> None:
        """Plain ``api.kimi.com`` non-/coding endpoint keeps thinking.

        The thinking guard only applies to the ``/coding`` endpoint, which
        has specific ``reasoning_content`` validation requirements for
        replayed tool-call messages.  Other Kimi endpoints (e.g. ``/v1``)
        do not enforce this restriction and should receive the thinking
        parameter normally.  See #56727.
        """
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model="kimi-k2.5",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "medium"},
            base_url="https://api.kimi.com/v1",
        )
        assert "thinking" in kwargs

    # ── #56727: custom / proxied Kimi-compatible endpoints now get thinking ──
    @pytest.mark.parametrize(
        "base_url,model",
        [
            # Custom host with Kimi-family model — the reporter's case
            ("http://my-kimi-proxy.internal", "kimi-2.6"),
            ("https://llm.example.com/anthropic", "kimi-k2.5"),
            ("https://llm.example.com/anthropic", "moonshot-v1-8k"),
            ("https://llm.example.com/anthropic", "kimi_thinking"),
            ("https://llm.example.com/anthropic", "moonshotai/kimi-k2.5"),
            # Official Moonshot host (previously uncovered)
            ("https://api.moonshot.ai/anthropic", "moonshot-v1-32k"),
            ("https://api.moonshot.cn/anthropic", "moonshot-v1-32k"),
        ],
    )
    def test_kimi_family_custom_endpoint_gets_thinking(
        self, base_url: str, model: str
    ) -> None:
        """Custom / proxied Kimi endpoints (non-/coding) keep Anthropic thinking.

        The thinking guard only applies to Kimi's ``/coding`` endpoint.
        Other Kimi endpoints do not enforce the ``reasoning_content``
        validation and should receive the thinking parameter.  See #56727.
        """
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "medium"},
            base_url=base_url,
        )
        assert "thinking" in kwargs, (
            f"Kimi-family endpoint ({base_url}, {model}) should receive "
            f"Anthropic thinking — only /coding endpoints block it.  See #56727."
        )

    def test_custom_endpoint_non_kimi_model_keeps_thinking(self) -> None:
        """Custom endpoint with a non-Kimi model must keep thinking intact.

        Guards against over-broad model-family matching — only model names
        starting with a Kimi/Moonshot prefix should trigger suppression.
        """
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model="MiniMax-M2.7",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "medium"},
            base_url="https://my-llm-proxy.example.com/anthropic",
        )
        assert "thinking" in kwargs
        assert kwargs["thinking"]["type"] == "enabled"

    def test_kimi_family_replay_preserves_unsigned_thinking(self) -> None:
        """On a custom Kimi endpoint, unsigned reasoning_content thinking
        blocks must survive the third-party signature-stripping pass so
        the upstream's message-history validation passes.
        """
        from agent.anthropic_adapter import convert_messages_to_anthropic

        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "reasoning_content": "planning the tool call",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "skill_view", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
        _, converted = convert_messages_to_anthropic(
            messages,
            base_url="http://my-kimi-proxy.internal",
            model="kimi-2.6",
        )
        # The assistant message still carries the unsigned thinking block
        # synthesised from reasoning_content (required by Kimi's history
        # validation).  A plain third-party endpoint would have stripped it.
        assistant_msg = next(m for m in converted if m["role"] == "assistant")
        assistant_blocks = assistant_msg["content"]
        thinking_blocks = [
            b for b in assistant_blocks
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        assert len(thinking_blocks) == 1
        assert thinking_blocks[0]["thinking"] == "planning the tool call"
