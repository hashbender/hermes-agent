"""Tenki cloud sandbox execution environment."""

from __future__ import annotations

import inspect
import logging
import math
import os
import re
import shlex
import tarfile
import tempfile
import threading
from pathlib import Path
from typing import Any

from tools.environments.base import BaseEnvironment, _ThreadedProcessHandle
from tools.environments.file_sync import (
    FileSyncManager,
    iter_sync_files,
    quoted_mkdir_command,
    quoted_rm_command,
    unique_parent_dirs,
)
from tools.tenki_config import (
    resolve_tenki_api_endpoint,
    resolve_tenki_auth_token,
    resolve_tenki_project_id,
    resolve_tenki_workspace_id,
)

logger = logging.getLogger(__name__)


def _safe_name(value: str, *, fallback: str = "default", max_len: int = 48) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "").strip("-._")
    return (safe or fallback)[:max_len]


def _supports_any_kwargs(sig: inspect.Signature | None) -> bool:
    if sig is None:
        return True
    return any(param.kind == inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values())


def _add_supported(
    kwargs: dict[str, Any],
    sig: inspect.Signature | None,
    names: tuple[str, ...],
    value: Any,
) -> None:
    if value in (None, "", [], {}):
        return
    if sig is not None:
        for name in names:
            if name in sig.parameters:
                kwargs[name] = value
                return
    if _supports_any_kwargs(sig):
        kwargs[names[0]] = value


def _result_attr(result: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        if not hasattr(result, name):
            continue
        value = getattr(result, name)
        if callable(value):
            try:
                value = value()
            except TypeError:
                pass
        if value is not None:
            return value
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _rewrite_sudo_noninteractive(command: str) -> tuple[str, int]:
    """Add ``-n`` to real sudo invocations so Tenki never prompts."""
    from tools.terminal_tool import _looks_like_env_assignment, _read_shell_token

    out: list[str] = []
    i = 0
    n = len(command)
    command_start = True
    sudo_count = 0

    while i < n:
        ch = command[i]

        if ch.isspace():
            out.append(ch)
            if ch == "\n":
                command_start = True
            i += 1
            continue

        if ch == "#" and command_start:
            comment_end = command.find("\n", i)
            if comment_end == -1:
                out.append(command[i:])
                break
            out.append(command[i:comment_end])
            i = comment_end
            continue

        if command.startswith("&&", i) or command.startswith("||", i) or command.startswith(";;", i):
            out.append(command[i:i + 2])
            i += 2
            command_start = True
            continue

        if ch in ";|&(":
            out.append(ch)
            i += 1
            command_start = True
            continue

        if ch == ")":
            out.append(ch)
            i += 1
            command_start = False
            continue

        token, next_i = _read_shell_token(command, i)
        if command_start and token == "sudo":
            out.append("sudo -n")
            sudo_count += 1
        else:
            out.append(token)

        if command_start and _looks_like_env_assignment(token):
            command_start = True
        else:
            command_start = False
        i = next_i

    return "".join(out), sudo_count


class TenkiEnvironment(BaseEnvironment):
    """Tenki sandbox backend.

    Tenki's SDK exposes blocking command execution, so this adapts it to the
    normal Hermes ``ProcessHandle`` contract with ``_ThreadedProcessHandle``.
    """

    _stdin_mode = "heredoc"
    _snapshot_timeout = 60

    def __init__(
        self,
        image: str = "",
        cwd: str = "/home/tenki",
        timeout: int = 60,
        cpu: float = 1,
        memory: int = 5120,
        disk: int = 51200,
        persistent_filesystem: bool = False,
        task_id: str = "default",
        api_endpoint: str = "",
        workspace_id: str = "",
        project_id: str = "",
        name_prefix: str = "hermes",
        allow_inbound: bool = False,
        allow_outbound: bool = True,
        max_duration: int = 3600,
        idle_timeout: int = 0,
        pause_retention: int = 0,
        sync_hermes_home: bool = False,
    ):
        super().__init__(cwd=cwd, timeout=timeout)

        try:
            from tools.lazy_deps import ensure as _lazy_ensure

            _lazy_ensure("terminal.tenki", prompt=False)
        except ImportError:
            pass
        except Exception as exc:
            raise ImportError(str(exc))

        from tenki_sandbox import Client, Sandbox

        self._Client = Client
        self._Sandbox = Sandbox
        self._client = None
        self._sandbox = None
        self._lock = threading.Lock()
        self._persistent = persistent_filesystem
        self._sync_hermes_home = sync_hermes_home
        self._sync_manager: FileSyncManager | None = None
        self._task_id = task_id
        self._image = image
        self._cpu = cpu
        self._memory = memory
        self._disk = disk
        self._api_endpoint = resolve_tenki_api_endpoint(api_endpoint)
        self._workspace_id = resolve_tenki_workspace_id(workspace_id)
        self._project_id = resolve_tenki_project_id(project_id)
        self._auth_token = resolve_tenki_auth_token()
        self._name_prefix = _safe_name(name_prefix, fallback="hermes", max_len=28)
        self._allow_inbound = allow_inbound
        self._allow_outbound = allow_outbound
        self._max_duration = max_duration
        self._idle_timeout = idle_timeout
        self._pause_retention = pause_retention
        self._remote_home = "/home/tenki"

        self._ensure_sandbox()
        self._resolve_remote_home()
        if self._sync_hermes_home:
            self._sync_manager = FileSyncManager(
                get_files_fn=lambda: iter_sync_files(f"{self._remote_home}/.hermes"),
                upload_fn=self._tenki_upload,
                delete_fn=self._tenki_delete,
                bulk_upload_fn=self._tenki_bulk_upload,
                bulk_download_fn=self._tenki_bulk_download,
            )
            self._sync_manager.sync(force=True)
        self.init_session()

    def _sandbox_create_signature(self) -> inspect.Signature | None:
        try:
            return inspect.signature(self._Sandbox.create)
        except (TypeError, ValueError):
            return None

    def _create_kwargs(self) -> dict[str, Any]:
        sig = self._sandbox_create_signature()
        kwargs: dict[str, Any] = {}
        sandbox_name = f"{self._name_prefix}-{_safe_name(self._task_id)}"

        _add_supported(kwargs, sig, ("name",), sandbox_name)
        _add_supported(kwargs, sig, ("image", "template"), self._image)
        cpu_cores = max(1, math.ceil(float(self._cpu))) if self._cpu else None
        _add_supported(kwargs, sig, ("cpu_cores", "cpu"), cpu_cores)
        _add_supported(kwargs, sig, ("memory_mb", "memory"), self._memory)

        if self._disk:
            disk_gb = max(1, math.ceil(float(self._disk) / 1024))
            _add_supported(kwargs, sig, ("disk_size_gb", "disk_gb", "disk"), disk_gb)

        _add_supported(kwargs, sig, ("allow_inbound",), self._allow_inbound)
        _add_supported(kwargs, sig, ("allow_outbound",), self._allow_outbound)
        _add_supported(kwargs, sig, ("max_duration",), self._max_duration)
        idle_timeout = _positive_float(self._idle_timeout)
        if idle_timeout is not None:
            idle_timeout_minutes = max(1, math.ceil(idle_timeout / 60))
            _add_supported(kwargs, sig, ("idle_timeout_minutes",), idle_timeout_minutes)
        pause_retention = _positive_float(self._pause_retention)
        if pause_retention is not None:
            _add_supported(kwargs, sig, ("pause_retention",), pause_retention)
        _add_supported(kwargs, sig, ("workspace_id",), self._workspace_id)
        _add_supported(kwargs, sig, ("project_id",), self._project_id)
        _add_supported(kwargs, sig, ("base_url", "api_endpoint"), self._api_endpoint)
        _add_supported(kwargs, sig, ("auth_token", "api_key"), self._auth_token)
        _add_supported(
            kwargs,
            sig,
            ("metadata",),
            {
                "hermes_task_id": self._task_id,
                "hermes_backend": "tenki",
            },
        )
        _add_supported(kwargs, sig, ("tags",), ["hermes-agent"])
        _add_supported(kwargs, sig, ("wait",), True)
        _add_supported(kwargs, sig, ("timeout",), max(60, self.timeout))
        return kwargs

    def _create_client(self):
        if self._client is None:
            self._client = self._Client(
                auth_token=self._auth_token,
                base_url=self._api_endpoint,
                timeout=max(60, self.timeout),
            )
        return self._client

    def _sandbox_name(self) -> str:
        return f"{self._name_prefix}-{_safe_name(self._task_id)}"

    @staticmethod
    def _sandbox_state(sandbox: Any) -> str:
        state = getattr(sandbox, "state", "")
        if callable(state):
            try:
                state = state()
            except TypeError:
                state = ""
        return str(state or "").upper()

    def _sandbox_matches_task(self, sandbox: Any) -> bool:
        name = getattr(sandbox, "name", "")
        info = getattr(sandbox, "info", None)
        if not name and info is not None:
            name = getattr(info, "name", "")
        if name != self._sandbox_name():
            return False
        metadata = getattr(info, "metadata", {}) if info is not None else {}
        if isinstance(metadata, dict) and metadata.get("hermes_task_id"):
            return metadata.get("hermes_task_id") == self._task_id
        return True

    def _find_persistent_sandbox(self):
        if not self._persistent:
            return None
        client = self._create_client()
        try:
            if self._project_id and hasattr(client, "list_project"):
                candidates = client.list_project(self._project_id, tags=["hermes-agent"])
            elif self._workspace_id and hasattr(client, "list_workspace"):
                candidates = client.list_workspace(self._workspace_id, tags=["hermes-agent"])
            else:
                candidates = client.list(tags=["hermes-agent"])
        except Exception as exc:
            logger.debug("Tenki: could not list persistent sandboxes: %s", exc)
            return None

        usable = []
        for sandbox in candidates:
            if not self._sandbox_matches_task(sandbox):
                continue
            state = self._sandbox_state(sandbox)
            if state in {"TERMINATING", "TERMINATED"}:
                continue
            usable.append((state, sandbox))
        if not usable:
            return None
        usable.sort(key=lambda item: 0 if item[0] == "RUNNING" else 1)
        return usable[0][1]

    def _resume_persistent_sandbox(self):
        sandbox = self._find_persistent_sandbox()
        if sandbox is None:
            return None
        state = self._sandbox_state(sandbox)
        if state != "RUNNING":
            resume = getattr(sandbox, "resume", None)
            if callable(resume):
                resume()
        wait_ready = getattr(sandbox, "wait_ready", None)
        if callable(wait_ready):
            wait_ready(max(60, self.timeout))
        sandbox_id = getattr(sandbox, "id", None) or getattr(sandbox, "sandbox_id", None)
        logger.info("Tenki: resumed sandbox %s for task %s", sandbox_id or "<unknown>", self._task_id)
        return sandbox

    def _ensure_sandbox(self) -> None:
        with self._lock:
            if self._sandbox is not None:
                return
            self._sandbox = self._resume_persistent_sandbox()
            if self._sandbox is not None:
                return
            kwargs = self._create_kwargs()
            if self._persistent:
                client = self._create_client()
                create_kwargs = dict(kwargs)
                for key in ("auth_token", "api_key", "base_url", "api_endpoint"):
                    create_kwargs.pop(key, None)
                self._sandbox = client.create(**create_kwargs)
            else:
                self._sandbox = self._Sandbox.create(**kwargs)
            sandbox_id = getattr(self._sandbox, "id", None) or getattr(self._sandbox, "sandbox_id", None)
            logger.info("Tenki: created sandbox %s for task %s", sandbox_id or "<unknown>", self._task_id)

    def _resolve_remote_home(self) -> None:
        try:
            result = self._exec_raw("echo \"$HOME\"", timeout=15)
            home = result[0].strip() if result[1] == 0 else ""
            if home:
                self._remote_home = home
                if self.cwd in {"~", "/home/tenki"}:
                    self.cwd = home
        except Exception:
            pass

    def _tenki_upload(self, host_path: str, remote_path: str) -> None:
        self._ensure_sandbox()
        parent = str(Path(remote_path).parent)
        self._sandbox.fs.mkdir(parent, recursive=True)
        self._sandbox.fs.upload(host_path, remote_path)

    def _tenki_bulk_upload(self, files: list[tuple[str, str]]) -> None:
        if not files:
            return

        self._ensure_sandbox()
        parents = unique_parent_dirs(files)
        if parents:
            self._exec_raw(quoted_mkdir_command(parents), timeout=30)

        remote_tar = f"/tmp/.hermes_tenki_sync.{os.getpid()}.{self._session_id}.tar"
        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
            with tarfile.open(fileobj=tmp, mode="w") as tar:
                for host_path, remote_path in files:
                    tar.add(host_path, arcname=remote_path.lstrip("/"))
            tmp.flush()
            self._sandbox.fs.upload(tmp.name, remote_tar)

        try:
            output, exit_code = self._exec_raw(
                f"tar xf {shlex.quote(remote_tar)} -C /",
                timeout=120,
            )
            if exit_code != 0:
                raise RuntimeError(f"Tenki bulk upload failed (exit {exit_code}): {output}")
        finally:
            try:
                self._exec_raw(f"rm -f {shlex.quote(remote_tar)}", timeout=10)
            except Exception:
                pass

    def _tenki_bulk_download(self, dest: Path) -> None:
        self._ensure_sandbox()
        remote_tar = f"/tmp/.hermes_tenki_sync_back.{os.getpid()}.{self._session_id}.tar"
        rel_base = f"{self._remote_home}/.hermes".lstrip("/")
        try:
            output, exit_code = self._exec_raw(
                f"tar cf {shlex.quote(remote_tar)} -C / {shlex.quote(rel_base)}",
                timeout=120,
            )
            if exit_code != 0:
                raise RuntimeError(f"Tenki bulk download failed (exit {exit_code}): {output}")
            self._sandbox.fs.download(remote_tar, dest)
        finally:
            try:
                self._exec_raw(f"rm -f {shlex.quote(remote_tar)}", timeout=10)
            except Exception:
                pass

    def _tenki_delete(self, remote_paths: list[str]) -> None:
        if not remote_paths:
            return
        self._exec_raw(quoted_rm_command(remote_paths), timeout=30)

    def _exec_raw(self, command: str, *, login: bool = False, timeout: int = 120) -> tuple[str, int]:
        self._ensure_sandbox()
        flag = "-lc" if login else "-c"
        result = self._sandbox.exec("bash", flag, command, timeout=timeout)
        stdout = _text(_result_attr(result, ("stdout_text", "stdout", "output", "result", "text")))
        stderr = _text(_result_attr(result, ("stderr_text", "stderr")))
        exit_code = _result_attr(result, ("exit_code", "returncode", "status_code"))
        if exit_code is None:
            ok = _result_attr(result, ("ok", "success"))
            exit_code = 0 if ok is True else 1
        if stdout and stderr and not stdout.endswith("\n"):
            output = stdout + "\n" + stderr
        else:
            output = stdout + stderr
        return output, int(exit_code)

    def _sudo_nopasswd_works(self) -> bool:
        try:
            _output, exit_code = self._exec_raw("sudo -n true", timeout=10)
        except Exception:
            return False
        return exit_code == 0

    def _prepare_command(self, command: str | None) -> tuple[str | None, str | None]:
        if command is None:
            return None, None

        # Tenki sandboxes should rely on their own sudoers policy. Do not ask
        # the user for a host sudo password, and do not send SUDO_PASSWORD to a
        # remote cloud sandbox. The default Tenki image supports NOPASSWD sudo.
        transformed, sudo_count = _rewrite_sudo_noninteractive(command)
        if sudo_count == 0:
            return command, None
        if self._sudo_nopasswd_works():
            return command, None
        return transformed, None

    def _before_execute(self) -> None:
        self._ensure_sandbox()
        if self._sync_manager:
            self._sync_manager.sync()

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ):
        def cancel() -> None:
            with self._lock:
                sandbox = self._sandbox
                self._sandbox = None
            if sandbox is None:
                return
            for method_name in ("terminate", "stop", "close"):
                method = getattr(sandbox, method_name, None)
                if callable(method):
                    try:
                        method()
                    except Exception:
                        pass
                    return

        def exec_fn() -> tuple[str, int]:
            return self._exec_raw(cmd_string, login=login, timeout=timeout)

        return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel)

    def cleanup(self):
        with self._lock:
            sandbox = self._sandbox
            self._sandbox = None
            sync_manager = self._sync_manager
            self._sync_manager = None
            client = self._client
            self._client = None
        if sandbox is None:
            if client is not None:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            return

        if sync_manager:
            logger.info("Tenki: syncing files from sandbox...")
            try:
                sync_manager.sync_back()
            except Exception as exc:
                logger.warning("Tenki: sync_back failed: %s", exc)

        if self._persistent:
            pause = getattr(sandbox, "pause", None)
            if callable(pause):
                try:
                    pause()
                    logger.info("Tenki: paused sandbox for task %s", self._task_id)
                    if client is not None:
                        close = getattr(client, "close", None)
                        if callable(close):
                            close()
                    return
                except Exception as exc:
                    logger.warning("Tenki: pause failed; terminating instead: %s", exc)

        for method_name in ("terminate", "delete", "close"):
            method = getattr(sandbox, method_name, None)
            if not callable(method):
                continue
            try:
                method()
                logger.info("Tenki: terminated sandbox for task %s", self._task_id)
            except Exception as exc:
                logger.warning("Tenki: cleanup failed: %s", exc)
            finally:
                if client is not None:
                    close = getattr(client, "close", None)
                    if callable(close):
                        close()
            return
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()
