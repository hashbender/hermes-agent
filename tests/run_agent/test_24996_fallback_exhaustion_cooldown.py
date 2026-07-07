"""Regression tests for #24996 — fallback-switch storm on host memory.

When every provider in the fallback chain fails non-retryably back-to-back
(e.g. HTTP 400/402/429 across distinct providers), the within-turn walk is
bounded (``_fallback_index`` advances monotonically and the loop aborts when
the chain exhausts).  The damaging mode is *cross-turn*: ``restore_primary_
runtime`` resets ``_fallback_index = 0`` every turn, so a client that
re-submits immediately replays the entire chain — re-marshaling the full
(potentially 80k-token) context once per provider every turn — with no
throttle on the non-rate-limit path.

The fix arms a short cooldown via CooldownManager when the chain exhausts on
a non-rate-limit failure, so the next turn's restore stays gated until the
cooldown clears.  Rate-limit / billing failures keep their own exponential
cooldown and are unaffected.
"""

from unittest.mock import MagicMock, patch
from run_agent import AIAgent
from agent.error_classifier import FailoverReason
from agent.chat_completion_helpers import _FALLBACK_EXHAUSTED_COOLDOWN_S
from agent.cooldown_manager import CooldownManager, set_cooldown_manager


def _make_agent(fallback_model=None):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _mock_client(base_url="https://openrouter.ai/api/v1", api_key="fb-key"):
    mock = MagicMock()
    mock.base_url = base_url
    mock.api_key = api_key
    return mock


def _fresh_mgr():
    """Return a no-persistence CooldownManager and install it as the singleton."""
    mgr = CooldownManager(storage_path=False)
    set_cooldown_manager(mgr)
    return mgr


class TestExhaustionArmsCooldown:
    def test_non_retryable_exhaustion_arms_cooldown(self):
        """Walking a non-empty chain to exhaustion on a non-rate-limit
        failure arms a short cooldown via CooldownManager."""
        mgr = _fresh_mgr()
        fbs = [
            {"provider": "openai", "model": "gpt-4o"},
            {"provider": "zai", "model": "glm-4.7"},
        ]
        agent = _make_agent(fallback_model=fbs)
        with (
            patch("agent.auxiliary_client.resolve_provider_client",
                  return_value=(_mock_client(), "resolved")),
        ):
            assert agent._try_activate_fallback() is True   # -> entry 0
            assert agent._try_activate_fallback() is True   # -> entry 1
            # Chain now exhausted; a non-rate-limit failure must arm cooldown.
            assert agent._try_activate_fallback() is False

        # CooldownManager should have a cooling entry for the primary provider
        status = mgr.get_cooldown_status()
        assert len(status["cooling"]) >= 1

    def test_no_chain_does_not_arm_cooldown(self):
        """An empty chain (no fallback configured) must not arm a cooldown."""
        mgr = _fresh_mgr()
        agent = _make_agent(fallback_model=None)
        assert agent._try_activate_fallback() is False
        status = mgr.get_cooldown_status()
        assert status["cooling"] == []

    def test_rate_limit_exhaustion_arms_cooldown(self):
        """A rate-limit failure arms an exponential cooldown via CooldownManager."""
        mgr = _fresh_mgr()
        fbs = [{"provider": "openai", "model": "gpt-4o"}]
        agent = _make_agent(fallback_model=fbs)
        with (
            patch("agent.auxiliary_client.resolve_provider_client",
                  return_value=(_mock_client(), "resolved")),
        ):
            assert agent._try_activate_fallback(reason=FailoverReason.rate_limit) is True
            assert agent._try_activate_fallback(reason=FailoverReason.rate_limit) is False

        status = mgr.get_cooldown_status()
        assert len(status["cooling"]) >= 1

    def test_cooldown_armed_only_once_for_same_provider(self):
        """Chain-switching while already on fallback must not re-arm cooldown
        for the primary provider."""
        mgr = _fresh_mgr()
        fbs = [
            {"provider": "openrouter", "model": "model-a"},
            {"provider": "anthropic", "model": "model-b"},
        ]
        agent = _make_agent(fallback_model=fbs)
        with (
            patch("agent.auxiliary_client.resolve_provider_client",
                  return_value=(_mock_client(), "resolved")),
        ):
            agent._try_activate_fallback(reason=FailoverReason.rate_limit)
            # Record cooling keys after first call
            first_cooling = set(mgr.get_cooldown_status()["cooling"])

            agent._try_activate_fallback(reason=FailoverReason.rate_limit)
            second_cooling = set(mgr.get_cooldown_status()["cooling"])

        # Same keys cooling — second call on a different provider should not
        # have added the primary provider again.
        assert first_cooling == second_cooling
