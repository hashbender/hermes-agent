"""Tests for agent/system_prompt.py — context-file cwd wiring."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.system_prompt import build_system_prompt_parts


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _captured_context_cwd(agent):
    """The cwd build_system_prompt_parts hands to build_context_files_prompt."""
    captured = {}

    def fake_context_files(cwd=None, skip_soul=False, context_length=None):
        captured["cwd"] = cwd
        return ""

    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", side_effect=fake_context_files),
    ):
        build_system_prompt_parts(agent)
    return captured["cwd"]


class TestContextFileCwd:
    def test_none_when_terminal_cwd_unset(self, monkeypatch):
        # Unset → None, so discovery falls back to the launch dir inside
        # build_context_files_prompt (the local-CLI #19242 contract).
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        assert _captured_context_cwd(_make_agent()) is None

    def test_configured_dir_when_terminal_cwd_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        assert _captured_context_cwd(_make_agent()) == tmp_path


def _stable_prompt(agent):
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


def _init_code_repo(path):
    """A git repo that actually holds code — the coding posture requires a source
    file (or manifest), not a bare ``.git`` (a prose/notes repo stays general)."""
    import subprocess

    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (path / "main.py").write_text("print('hi')\n")


class TestCodingContextBlock:
    def test_injected_when_active(self, monkeypatch, tmp_path):
        _init_code_repo(tmp_path)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=["read_file"], platform="cli")
        stable = _stable_prompt(agent)
        assert "coding agent" in stable
        assert "Workspace" in stable

    def test_absent_when_off(self, monkeypatch, tmp_path):
        _init_code_repo(tmp_path)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=["read_file"], platform="cli")
        # Drive the real path: force the resolved mode to "off" via config.
        with patch("agent.coding_context._coding_mode", return_value="off"):
            stable = _stable_prompt(agent)
        assert "coding agent" not in stable

    def test_absent_without_tools(self, monkeypatch, tmp_path):
        _init_code_repo(tmp_path)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=[], platform="cli")
        assert "coding agent" not in _stable_prompt(agent)


class TestCronDeliveryInvariants:
    """CRON_DELIVERY_INVARIANTS ([SILENT] suppression + no send_message) must
    reach the system prompt for every cron run, and must survive a
    platform_hints.cron override — unlike PLATFORM_HINTS["cron"] (the
    descriptive hint), it is NOT subject to the replace/append override
    resolved by _resolve_platform_hint. See agent/prompt_builder.py's
    CRON_DELIVERY_INVARIANTS docstring for the full rationale."""

    def test_present_for_cron_platform(self):
        stable = _stable_prompt(_make_agent(platform="cron"))
        # Base descriptive hint (PLATFORM_HINTS["cron"], via _effective_hint)
        # must still land alongside the invariants — this guards against a
        # regression where _effective_hint silently stops resolving while
        # CRON_DELIVERY_INVARIANTS alone keeps this test green.
        assert "There is no user present" in stable
        assert "send_message" in stable
        assert "[SILENT]" in stable

    def test_absent_for_non_cron_platform(self):
        stable = _stable_prompt(_make_agent(platform="cli"))
        assert "[SILENT]" not in stable

    def test_survives_platform_hints_replace_override(self):
        """Regression guard: an admin using the documented
        platform_hints.cron.replace override to customize the descriptive
        cron hint's wording must never accidentally disable [SILENT]
        suppression or the send_message prohibition — those are scheduler
        mechanics (cron/scheduler.py::run_job), not just tone."""
        agent = _make_agent(
            platform="cron",
            _platform_hint_overrides={
                "cron": {"replace": "Custom cron hint with no mention of delivery mechanics."}
            },
        )
        stable = _stable_prompt(agent)
        assert "Custom cron hint with no mention of delivery mechanics." in stable
        assert "send_message" in stable
        assert "[SILENT]" in stable

    def test_survives_platform_hints_append_override(self):
        agent = _make_agent(
            platform="cron",
            _platform_hint_overrides={"cron": {"append": "Extra operator note."}},
        )
        stable = _stable_prompt(agent)
        assert "Extra operator note." in stable
        assert "send_message" in stable
        assert "[SILENT]" in stable

