from __future__ import annotations

import sys
import types
from types import SimpleNamespace


class _FakeResult:
    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 0):
        self.stdout_text = stdout
        self.stderr_text = stderr
        self.exit_code = exit_code


class _FakeSandbox:
    def __init__(
        self,
        *,
        name: str = "sb-test",
        state: str = "RUNNING",
        metadata: dict | None = None,
    ):
        self.exec_calls: list[tuple[tuple, dict]] = []
        self.terminated = False
        self.paused = False
        self.resumed = False
        self.waited = False
        self.id = "sb-test"
        self.name = name
        self.state = state
        self.info = SimpleNamespace(name=name, metadata=metadata or {})
        self.fs = SimpleNamespace(
            mkdir=lambda *_args, **_kwargs: None,
            upload=lambda *_args, **_kwargs: None,
            download=lambda *_args, **_kwargs: None,
        )

    def exec(self, *args, **kwargs):
        self.exec_calls.append((args, kwargs))
        command = args[-1] if args else ""
        if "echo \"$HOME\"" in command:
            return _FakeResult(stdout="/home/tenki\n")
        return _FakeResult(stdout="ran\n", exit_code=0)

    def terminate(self):
        self.terminated = True
        self.state = "TERMINATED"

    def pause(self):
        self.paused = True
        self.state = "PAUSED"

    def resume(self):
        self.resumed = True
        self.state = "RUNNING"

    def wait_ready(self, *_args, **_kwargs):
        self.waited = True


class _FakeSandboxFactory:
    created_kwargs: list[dict] = []
    sandboxes: list[_FakeSandbox] = []

    @classmethod
    def create(cls, **kwargs):
        sandbox = _FakeSandbox(
            name=kwargs.get("name", "sb-test"),
            metadata=kwargs.get("metadata", {}),
        )
        cls.created_kwargs.append(kwargs)
        cls.sandboxes.append(sandbox)
        return sandbox


class _FakeClient:
    listed_sandboxes: list[_FakeSandbox] = []
    closed_count = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def create(self, **kwargs):
        return _FakeSandboxFactory.create(**kwargs)

    def list(self, **_kwargs):
        return list(self.listed_sandboxes)

    def list_project(self, *_args, **_kwargs):
        return list(self.listed_sandboxes)

    def list_workspace(self, *_args, **_kwargs):
        return list(self.listed_sandboxes)

    def close(self):
        type(self).closed_count += 1


def _install_fake_tenki(monkeypatch):
    module = types.ModuleType("tenki_sandbox")
    _FakeSandboxFactory.created_kwargs = []
    _FakeSandboxFactory.sandboxes = []
    _FakeClient.listed_sandboxes = []
    _FakeClient.closed_count = 0
    module.Client = _FakeClient
    module.Sandbox = _FakeSandboxFactory
    monkeypatch.setitem(sys.modules, "tenki_sandbox", module)


def _clear_tenki_auth_env(monkeypatch):
    monkeypatch.delenv("TENKI_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TENKI_API_KEY", raising=False)


def test_tenki_cli_auth_token_is_normalized_for_sdk_cookie_auth(monkeypatch, tmp_path):
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text("auth_token: cli-cookie\n", encoding="utf-8")

    from tools.tenki_config import resolve_tenki_auth_token

    assert resolve_tenki_auth_token() == "cookie:cli-cookie"


def test_tenki_cli_auth_token_preserves_sdk_prefixes(monkeypatch, tmp_path):
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "config.yaml"))

    from tools.tenki_config import resolve_tenki_auth_token

    for token in ("cookie:cli-cookie", "ory_st_session", "sk-api-key"):
        (tmp_path / "config.yaml").write_text(f"auth_token: {token}\n", encoding="utf-8")
        assert resolve_tenki_auth_token() == token


def test_tenki_cli_api_key_is_not_treated_as_cookie(monkeypatch, tmp_path):
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text("api_key: provider-key\n", encoding="utf-8")

    from tools.tenki_config import resolve_tenki_auth_token

    assert resolve_tenki_auth_token() == "provider-key"


def test_tenki_environment_uses_cli_config_and_terminates_by_default(monkeypatch, tmp_path):
    _install_fake_tenki(monkeypatch)
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text(
        "\n".join(
            [
                "api_endpoint: https://api.tenki.test",
                "current_workspace_id: ws-123",
                "current_project_id: prj-456",
                "auth_token: tok-secret",
            ]
        ),
        encoding="utf-8",
    )

    from tools.environments.tenki import TenkiEnvironment

    monkeypatch.setattr(TenkiEnvironment, "init_session", lambda self: None)

    env = TenkiEnvironment(
        image="",
        task_id="session 1",
        persistent_filesystem=False,
        allow_inbound=False,
        allow_outbound=True,
    )

    kwargs = _FakeSandboxFactory.created_kwargs[0]
    assert kwargs["base_url"] == "https://api.tenki.test"
    assert kwargs["workspace_id"] == "ws-123"
    assert kwargs["project_id"] == "prj-456"
    assert kwargs["auth_token"] == "cookie:tok-secret"
    assert kwargs["allow_inbound"] is False
    assert kwargs["allow_outbound"] is True
    assert kwargs["cpu_cores"] == 1
    assert "idle_timeout" not in kwargs
    assert "idle_timeout_minutes" not in kwargs
    assert "pause_retention" not in kwargs
    assert kwargs["metadata"]["hermes_backend"] == "tenki"
    assert kwargs["name"].startswith("hermes-session-1")

    output, exit_code = env._exec_raw("echo ok", timeout=5)
    assert output == "ran\n"
    assert exit_code == 0

    sandbox = _FakeSandboxFactory.sandboxes[0]
    env.cleanup()
    assert sandbox.terminated is True
    assert sandbox.paused is False


def test_tenki_environment_pauses_when_persistent(monkeypatch, tmp_path):
    _install_fake_tenki(monkeypatch)
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text("auth_token: tok-secret\n", encoding="utf-8")

    from tools.environments.tenki import TenkiEnvironment

    monkeypatch.setattr(TenkiEnvironment, "init_session", lambda self: None)
    env = TenkiEnvironment(task_id="persist", persistent_filesystem=True)

    sandbox = _FakeSandboxFactory.sandboxes[0]
    env.cleanup()
    assert sandbox.paused is True
    assert sandbox.terminated is False


def test_tenki_environment_resumes_existing_persistent_sandbox(monkeypatch, tmp_path):
    _install_fake_tenki(monkeypatch)
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text("auth_token: tok-secret\n", encoding="utf-8")
    existing = _FakeSandbox(
        name="hermes-persist",
        state="PAUSED",
        metadata={"hermes_task_id": "persist"},
    )
    _FakeClient.listed_sandboxes = [existing]

    from tools.environments.tenki import TenkiEnvironment

    monkeypatch.setattr(TenkiEnvironment, "init_session", lambda self: None)
    env = TenkiEnvironment(task_id="persist", persistent_filesystem=True)

    assert env._sandbox is existing
    assert existing.resumed is True
    assert existing.waited is True
    assert _FakeSandboxFactory.created_kwargs == []
    env.cleanup()


def test_tenki_environment_ignores_mismatched_persistent_sandbox(monkeypatch, tmp_path):
    _install_fake_tenki(monkeypatch)
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text("auth_token: tok-secret\n", encoding="utf-8")
    _FakeClient.listed_sandboxes = [
        _FakeSandbox(
            name="hermes-other",
            state="PAUSED",
            metadata={"hermes_task_id": "other"},
        )
    ]

    from tools.environments.tenki import TenkiEnvironment

    monkeypatch.setattr(TenkiEnvironment, "init_session", lambda self: None)
    env = TenkiEnvironment(task_id="persist", persistent_filesystem=True)

    assert _FakeSandboxFactory.created_kwargs
    assert _FakeSandboxFactory.created_kwargs[0]["name"] == "hermes-persist"
    env.cleanup()


def test_tenki_environment_converts_idle_timeout_to_sdk_minutes(monkeypatch, tmp_path):
    _install_fake_tenki(monkeypatch)
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text("auth_token: tok-secret\n", encoding="utf-8")

    from tools.environments.tenki import TenkiEnvironment

    monkeypatch.setattr(TenkiEnvironment, "init_session", lambda self: None)
    env = TenkiEnvironment(task_id="idle", cpu=1.2, idle_timeout=61)

    kwargs = _FakeSandboxFactory.created_kwargs[0]
    assert kwargs["cpu_cores"] == 2
    assert kwargs["idle_timeout_minutes"] == 2
    env.cleanup()


def test_tenki_environment_omits_non_positive_pause_retention(monkeypatch, tmp_path):
    _install_fake_tenki(monkeypatch)
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text("auth_token: tok-secret\n", encoding="utf-8")

    from tools.environments.tenki import TenkiEnvironment

    monkeypatch.setattr(TenkiEnvironment, "init_session", lambda self: None)

    env = TenkiEnvironment(task_id="pause-default", pause_retention=0)
    kwargs = _FakeSandboxFactory.created_kwargs[0]
    assert "pause_retention" not in kwargs
    env.cleanup()

    env = TenkiEnvironment(task_id="pause-negative", pause_retention=-1)
    kwargs = _FakeSandboxFactory.created_kwargs[1]
    assert "pause_retention" not in kwargs
    env.cleanup()


def test_tenki_environment_passes_positive_pause_retention(monkeypatch, tmp_path):
    _install_fake_tenki(monkeypatch)
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text("auth_token: tok-secret\n", encoding="utf-8")

    from tools.environments.tenki import TenkiEnvironment

    monkeypatch.setattr(TenkiEnvironment, "init_session", lambda self: None)
    env = TenkiEnvironment(task_id="pause-positive", pause_retention=3600)

    kwargs = _FakeSandboxFactory.created_kwargs[0]
    assert kwargs["pause_retention"] == 3600
    env.cleanup()


def test_tenki_sync_hermes_home_is_opt_in(monkeypatch, tmp_path):
    _install_fake_tenki(monkeypatch)
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text("auth_token: tok-secret\n", encoding="utf-8")

    from tools.environments import tenki as tenki_module
    from tools.environments.tenki import TenkiEnvironment

    calls = []

    class FakeSyncManager:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def sync(self, *, force=False):
            calls.append(("sync", force))

        def sync_back(self):
            calls.append(("sync_back", None))

    monkeypatch.setattr(tenki_module, "FileSyncManager", FakeSyncManager)
    monkeypatch.setattr(TenkiEnvironment, "init_session", lambda self: None)

    env = TenkiEnvironment(task_id="no-sync", sync_hermes_home=False)
    assert calls == []
    env.cleanup()

    env = TenkiEnvironment(task_id="sync", sync_hermes_home=True)
    assert calls[0][0] == "init"
    assert calls[1] == ("sync", True)
    env.cleanup()
    assert ("sync_back", None) in calls


def test_tenki_non_sudo_command_does_not_probe_sudo(monkeypatch, tmp_path):
    _install_fake_tenki(monkeypatch)
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text("auth_token: tok-secret\n", encoding="utf-8")

    from tools.environments.tenki import TenkiEnvironment

    monkeypatch.setattr(TenkiEnvironment, "init_session", lambda self: None)

    def fail_probe(self):
        raise AssertionError("sudo should not be probed for commands without sudo")

    monkeypatch.setattr(TenkiEnvironment, "_sudo_nopasswd_works", fail_probe)

    env = TenkiEnvironment(task_id="no-sudo")
    env.execute("echo ok", timeout=5)
    env.cleanup()


def test_tenki_passwordless_sudo_does_not_prompt_or_rewrite(monkeypatch, tmp_path):
    _install_fake_tenki(monkeypatch)
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text("auth_token: tok-secret\n", encoding="utf-8")

    from tools.environments.tenki import TenkiEnvironment

    monkeypatch.setattr(TenkiEnvironment, "init_session", lambda self: None)
    monkeypatch.setattr(TenkiEnvironment, "_sudo_nopasswd_works", lambda self: True)

    def fail_prompt(*_args, **_kwargs):
        raise AssertionError("Tenki sudo should not prompt for a host password")

    monkeypatch.setattr("tools.terminal_tool._prompt_for_sudo_password", fail_prompt)

    env = TenkiEnvironment(task_id="sudo-nopasswd")
    env.execute("sudo whoami", timeout=5)

    command = _FakeSandboxFactory.sandboxes[0].exec_calls[-1][0][-1]
    assert "sudo whoami" in command
    assert "sudo -S" not in command
    assert "sudo -n whoami" not in command
    env.cleanup()


def test_tenki_sudo_without_nopasswd_fails_fast_without_host_password(monkeypatch, tmp_path):
    _install_fake_tenki(monkeypatch)
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.setenv("SUDO_PASSWORD", "host-secret")
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text("auth_token: tok-secret\n", encoding="utf-8")

    from tools.environments.tenki import TenkiEnvironment

    monkeypatch.setattr(TenkiEnvironment, "init_session", lambda self: None)
    monkeypatch.setattr(TenkiEnvironment, "_sudo_nopasswd_works", lambda self: False)

    def fail_prompt(*_args, **_kwargs):
        raise AssertionError("Tenki sudo should not prompt for a host password")

    monkeypatch.setattr("tools.terminal_tool._prompt_for_sudo_password", fail_prompt)

    env = TenkiEnvironment(task_id="sudo-no-nopasswd")
    env.execute("sudo whoami", timeout=5)

    command = _FakeSandboxFactory.sandboxes[0].exec_calls[-1][0][-1]
    assert "sudo -n whoami" in command
    assert "sudo -S" not in command
    assert "host-secret" not in command
    env.cleanup()
