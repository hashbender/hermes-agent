"""Unit coverage for local terminal memory-guard wrapping."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import Mock

from tools.environments import local as local_env
from tools.process_registry import ProcessRegistry, ProcessSession


def test_memory_guard_disabled_without_limit(monkeypatch):
    monkeypatch.delenv("TERMINAL_LOCAL_MEMORY_MAX_MB", raising=False)
    monkeypatch.setattr(local_env, "_systemd_run_available", lambda: True)

    assert local_env._maybe_wrap_with_systemd_memory_guard(
        cmd_string="echo hi",
        login=False,
        run_env={},
        cwd="/tmp",
    ) == (None, None, None, None)


def test_memory_guard_disabled_when_systemd_run_unavailable(monkeypatch):
    monkeypatch.setenv("TERMINAL_LOCAL_MEMORY_MAX_MB", "256")
    monkeypatch.setattr(local_env, "_systemd_run_available", lambda: False)

    assert local_env._maybe_wrap_with_systemd_memory_guard(
        cmd_string="echo hi",
        login=False,
        run_env={},
        cwd="/tmp",
    ) == (None, None, None, None)


def test_memory_guard_builds_transient_unit_without_secret_argv_leak(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_LOCAL_MEMORY_MAX_MB", "256")
    monkeypatch.setenv("TERMINAL_LOCAL_MEMORY_SWAP_MAX_MB", "64")
    monkeypatch.setattr(local_env, "_systemd_run_available", lambda: True)
    monkeypatch.setattr(local_env, "_find_bash", lambda: "/bin/bash")
    monkeypatch.setattr(local_env.tempfile, "gettempdir", lambda: str(tmp_path))

    argv, env, cwd, unit = local_env._maybe_wrap_with_systemd_memory_guard(
        cmd_string="echo $SECRET_VALUE",
        login=True,
        run_env={"SECRET_VALUE": "super secret", "OK_NAME": "ok", "BAD-NAME": "drop"},
        cwd="/work dir",
    )

    assert argv is not None
    assert env == os.environ.copy()
    assert cwd == "/"
    assert unit.startswith("hermes-terminal-")
    assert argv[:6] == ["systemd-run", "--user", "--collect", "--pipe", "--quiet", "--unit"]
    assert "MemoryMax=256M" in argv
    assert "MemorySwapMax=64M" in argv
    assert "OOMPolicy=stop" in argv
    assert "KillMode=control-group" in argv
    script_path = next(
        str(p)
        for p in argv
        if str(p).startswith(str(tmp_path / "hermes-memguard-")) and str(p).endswith(".sh")
    )
    assert argv[-2:] == ["-l", script_path]

    rendered_argv = "\n".join(map(str, argv))
    assert "super secret" not in rendered_argv
    assert "SECRET_VALUE" not in rendered_argv

    scripts = [p for p in tmp_path.glob("hermes-memguard-*.sh") if "-env-" not in p.name]
    env_files = list(tmp_path.glob("hermes-memguard-env-*.sh"))
    assert len(scripts) == 1
    assert len(env_files) == 1
    env_text = env_files[0].read_text()
    assert "export SECRET_VALUE='super secret'" in env_text
    assert "export OK_NAME=ok" in env_text
    assert "BAD-NAME" not in env_text
    script_text = scripts[0].read_text()
    assert "source " in script_text
    assert "builtin cd -- '/work dir'" in script_text
    assert "echo $SECRET_VALUE" in script_text


def test_memory_guard_invalid_limit_disables(monkeypatch, caplog):
    monkeypatch.setenv("TERMINAL_LOCAL_MEMORY_MAX_MB", "not-an-int")
    monkeypatch.setattr(local_env, "_systemd_run_available", lambda: True)

    assert local_env._maybe_wrap_with_systemd_memory_guard(
        cmd_string="echo hi",
        login=False,
        run_env={},
        cwd="/tmp",
    ) == (None, None, None, None)
    assert "Invalid TERMINAL_LOCAL_MEMORY_MAX_MB" in caplog.text


def test_kill_process_stops_systemd_guard_before_host_tree(monkeypatch):
    registry = ProcessRegistry()
    stopped = []
    terminated = []
    moved = []

    def fake_run(args, **kwargs):
        stopped.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("tools.process_registry.subprocess.run", fake_run)
    monkeypatch.setattr(registry, "_terminate_host_pid", lambda pid, expected: terminated.append((pid, expected)))
    monkeypatch.setattr(registry, "_move_to_finished", lambda session: moved.append(session.id))
    monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)

    proc = Mock()
    proc.pid = 4321
    session = ProcessSession(
        id="proc_memguard",
        command="sleep 60",
        pid=4321,
        process=proc,
        host_start_time=999,
        systemd_unit="hermes-terminal-test.service",
    )
    registry._running[session.id] = session

    result = registry.kill_process(session.id)

    assert result["status"] == "killed"
    assert stopped == [["systemctl", "--user", "stop", "hermes-terminal-test.service"]]
    assert terminated == [(4321, 999)]
    assert moved == ["proc_memguard"]
    assert session.completion_reason == "killed"
    assert session.termination_source == "process.kill"
