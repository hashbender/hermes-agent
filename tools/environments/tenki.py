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

from hermes_constants import get_hermes_home
from tools.environments.base import (
    BaseEnvironment,
    _ThreadedProcessHandle,
    _load_json_store,
    _save_json_store,
)
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
_SNAPSHOT_STORE = get_hermes_home() / "tenki_snapshots.json"
_SNAPSHOT_NAMESPACE = "direct"


def _load_snapshots() -> dict:
    return _load_json_store(_SNAPSHOT_STORE)


def _save_snapshots(data: dict) -> None:
    _save_json_store(_SNAPSHOT_STORE, data)


def _snapshot_key(task_id: str) -> str:
    return f"{_SNAPSHOT_NAMESPACE}:{task_id}"


def _get_snapshot_restore_candidate(task_id: str) -> tuple[str | None, bool]:
    snapshots = _load_snapshots()
    namespaced_key = _snapshot_key(task_id)
    snapshot_id = snapshots.get(namespaced_key)
    if isinstance(snapshot_id, str) and snapshot_id:
        return snapshot_id, False
    legacy_snapshot_id = snapshots.get(task_id)
    if isinstance(legacy_snapshot_id, str) and legacy_snapshot_id:
        return legacy_snapshot_id, True
    return None, False


def _store_snapshot(task_id: str, snapshot_id: str) -> None:
    snapshots = _load_snapshots()
    snapshots[_snapshot_key(task_id)] = snapshot_id
    snapshots.pop(task_id, None)
    _save_snapshots(snapshots)


def _delete_snapshot(task_id: str, snapshot_id: str | None = None) -> None:
    snapshots = _load_snapshots()
    updated = False
    for key in (_snapshot_key(task_id), task_id):
        value = snapshots.get(key)
        if value is None:
            continue
        if snapshot_id is None or value == snapshot_id:
            snapshots.pop(key, None)
            updated = True
    if updated:
        _save_snapshots(snapshots)


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

    Tenki's SDK exposes process handles inside a remote sandbox, so this adapts
    them to the normal Hermes ``ProcessHandle`` contract with
    ``_ThreadedProcessHandle``.
    """

    _stdin_mode = "pipe"
    _snapshot_timeout = 60
    _terminal_states = frozenset({"TERMINATING", "TERMINATED", "DELETED", "FAILED", "ERROR"})

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
        self._cleanup_in_progress = False
        self._cleanup_sandbox = None
        self._task_id = task_id
        self._snapshot_restore_id: str | None = None
        self._snapshot_restore_from_legacy_key = False
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
        if self._persistent:
            self._snapshot_restore_id, self._snapshot_restore_from_legacy_key = (
                _get_snapshot_restore_candidate(self._task_id)
            )

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
        if self._snapshot_restore_id:
            _add_supported(kwargs, sig, ("snapshot_id",), self._snapshot_restore_id)
        else:
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
        # Do NOT emit a create-time ``timeout`` here: the SDK's Sandbox.create
        # pops ``timeout`` into the *Client* (HTTP) timeout, while Client.create
        # treats ``timeout`` as the *wait-for-ready* budget — so the same value
        # would mean two different things across the two create paths. The HTTP
        # timeout is set explicitly in _create_client(); readiness uses the
        # SDK's default wait budget.
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
            if state in self._terminal_states:
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
        if not self._ensure_sandbox_ready(sandbox):
            logger.info(
                "Tenki: existing sandbox for task %s is no longer reusable; creating a fresh sandbox",
                self._task_id,
            )
            return None
        sandbox_id = getattr(sandbox, "id", None) or getattr(sandbox, "sandbox_id", None)
        logger.info("Tenki: resumed sandbox %s for task %s", sandbox_id or "<unknown>", self._task_id)
        return sandbox

    def _ensure_sandbox_ready(self, sandbox: Any) -> bool:
        refresh = getattr(sandbox, "refresh", None)
        if callable(refresh):
            try:
                refresh()
            except Exception as exc:
                logger.info("Tenki: sandbox refresh failed for task %s: %s", self._task_id, exc)
                return False

        state = self._sandbox_state(sandbox)
        if state in self._terminal_states:
            return False

        try:
            if state and state != "RUNNING":
                resume = getattr(sandbox, "resume", None)
                if callable(resume):
                    resume()
                wait_ready = getattr(sandbox, "wait_ready", None)
                if callable(wait_ready):
                    wait_ready(max(60, self.timeout))
        except Exception as exc:
            logger.info("Tenki: could not make sandbox ready for task %s: %s", self._task_id, exc)
            return False

        return self._sandbox_state(sandbox) not in self._terminal_states

    def _ensure_sandbox(self) -> None:
        with self._lock:
            if self._cleanup_in_progress:
                raise RuntimeError("Tenki cleanup is in progress")
            if self._sandbox is not None:
                if self._ensure_sandbox_ready(self._sandbox):
                    return
                self._sandbox = None
            self._sandbox = self._resume_persistent_sandbox()
            if self._sandbox is not None:
                return
            self._sandbox = self._create_sandbox_with_snapshot_fallback()
            sandbox_id = getattr(self._sandbox, "id", None) or getattr(self._sandbox, "sandbox_id", None)
            logger.info("Tenki: created sandbox %s for task %s", sandbox_id or "<unknown>", self._task_id)

    def _create_sandbox_from_kwargs(self, kwargs: dict[str, Any]):
        if self._persistent:
            client = self._create_client()
            create_kwargs = dict(kwargs)
            for key in ("auth_token", "api_key", "base_url", "api_endpoint"):
                create_kwargs.pop(key, None)
            return client.create(**create_kwargs)
        return self._Sandbox.create(**kwargs)

    def _create_sandbox_with_snapshot_fallback(self):
        kwargs = self._create_kwargs()
        try:
            sandbox = self._create_sandbox_from_kwargs(kwargs)
        except Exception as exc:
            if not self._snapshot_restore_id:
                raise
            logger.warning(
                "Tenki: failed to restore snapshot %s for task %s; retrying with base image: %s",
                self._snapshot_restore_id,
                self._task_id,
                exc,
            )
            _delete_snapshot(self._task_id, self._snapshot_restore_id)
            self._snapshot_restore_id = None
            self._snapshot_restore_from_legacy_key = False
            sandbox = self._create_sandbox_from_kwargs(self._create_kwargs())
        else:
            if self._snapshot_restore_id and self._snapshot_restore_from_legacy_key:
                _store_snapshot(self._task_id, self._snapshot_restore_id)
        return sandbox

    def _remote_transfer_path(self, prefix: str) -> str:
        base = (self._remote_home or "/home/tenki").rstrip("/") or "/home/tenki"
        if base != "/home/tenki" and not base.startswith("/home/tenki/"):
            base = "/home/tenki"
        return f"{base}/{prefix}.{os.getpid()}.{self._session_id}.tar"

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

        remote_tar = self._remote_transfer_path(".hermes_tenki_sync")
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
        sandbox = self._transfer_sandbox()
        remote_tar = self._remote_transfer_path(".hermes_tenki_sync_back")
        rel_base = f"{self._remote_home}/.hermes".lstrip("/")
        try:
            output, exit_code = self._exec_raw_on_sandbox(
                sandbox,
                f"tar cf {shlex.quote(remote_tar)} -C / {shlex.quote(rel_base)}",
                timeout=120,
            )
            if exit_code != 0:
                raise RuntimeError(f"Tenki bulk download failed (exit {exit_code}): {output}")
            sandbox.fs.download(remote_tar, str(dest))
        finally:
            try:
                self._exec_raw_on_sandbox(sandbox, f"rm -f {shlex.quote(remote_tar)}", timeout=10)
            except Exception:
                pass

    def _transfer_sandbox(self):
        if self._cleanup_in_progress and self._cleanup_sandbox is not None:
            return self._cleanup_sandbox
        self._ensure_sandbox()
        return self._sandbox

    def _tenki_delete(self, remote_paths: list[str]) -> None:
        if not remote_paths:
            return
        self._exec_raw(quoted_rm_command(remote_paths), timeout=30)

    def _exec_raw(self, command: str, *, login: bool = False, timeout: int = 120) -> tuple[str, int]:
        self._ensure_sandbox()
        return self._exec_raw_on_sandbox(self._sandbox, command, login=login, timeout=timeout)

    def _exec_raw_on_sandbox(
        self,
        sandbox: Any,
        command: str,
        *,
        login: bool = False,
        timeout: int = 120,
    ) -> tuple[str, int]:
        flag = "-lc" if login else "-c"
        result = sandbox.exec("bash", flag, command, timeout=timeout)
        return self._result_to_output(result)

    @staticmethod
    def _result_to_output(result: Any) -> tuple[str, int]:
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

    def _start_process(
        self,
        cmd_string: str,
        *,
        login: bool,
        timeout: int,
        stdin_data: str | None,
        process_ref: dict[str, Any] | None = None,
    ) -> tuple[str, int]:
        self._ensure_sandbox()
        flag = "-lc" if login else "-c"
        start = getattr(self._sandbox, "start", None)
        if not callable(start):
            kwargs: dict[str, Any] = {"timeout": timeout}
            if stdin_data is not None:
                kwargs["input"] = stdin_data
            result = self._sandbox.exec("bash", flag, cmd_string, **kwargs)
            return self._result_to_output(result)

        process = start("bash", flag, cmd_string, timeout=timeout, stdin=stdin_data)
        if process_ref is not None:
            process_ref["process"] = process
        if stdin_data is None:
            close_stdin = getattr(process, "close_stdin", None)
            if callable(close_stdin):
                close_stdin()
        result = process.wait(timeout=timeout + 5 if timeout is not None else None)
        return self._result_to_output(result)

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
        process_ref: dict[str, Any] = {}

        def cancel() -> None:
            process = process_ref.get("process")
            kill = getattr(process, "kill", None)
            if callable(kill):
                try:
                    kill()
                    return
                except Exception:
                    pass
            with self._lock:
                sandbox = self._sandbox
                # Drop our reference so the next command resumes (persistent) or
                # recreates (ephemeral) a sandbox instead of reusing a torn-down
                # one.
                self._sandbox = None
            if sandbox is None:
                return
            # For a persistent sandbox, pause (preserve the filesystem) instead of
            # terminating: an interrupted or timed-out command must not destroy
            # state the user asked to keep. The paused sandbox is re-discovered and
            # resumed on the next command via _resume_persistent_sandbox().
            if self._persistent:
                pause = getattr(sandbox, "pause", None)
                if callable(pause):
                    try:
                        pause()
                        return
                    except Exception:
                        pass  # fall through to terminate if pause is unavailable
            for method_name in ("terminate", "close"):
                method = getattr(sandbox, method_name, None)
                if callable(method):
                    try:
                        method()
                    except Exception:
                        pass
                    return

        def exec_fn() -> tuple[str, int]:
            return self._start_process(
                cmd_string,
                login=login,
                timeout=timeout,
                stdin_data=stdin_data,
                process_ref=process_ref,
            )

        return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel)

    def cleanup(self):
        with self._lock:
            sandbox = self._sandbox
            sync_manager = self._sync_manager
            self._sync_manager = None
            client = self._client
            self._cleanup_in_progress = True
            self._cleanup_sandbox = sandbox
        if sandbox is None:
            self._close_client(client)
            with self._lock:
                if self._client is client:
                    self._client = None
                self._cleanup_in_progress = False
                self._cleanup_sandbox = None
            return

        try:
            if sync_manager:
                logger.info("Tenki: syncing files from sandbox...")
                try:
                    sync_manager.sync_back()
                except Exception as exc:
                    logger.warning("Tenki: sync_back failed: %s", exc)

            snapshot_saved = False
            if self._persistent:
                snapshot_saved = self._save_persistent_snapshot(sandbox)

            if self._persistent and not snapshot_saved:
                pause = getattr(sandbox, "pause", None)
                if callable(pause):
                    try:
                        pause()
                        logger.info("Tenki: paused sandbox for task %s", self._task_id)
                        return
                    except Exception as exc:
                        logger.warning("Tenki: pause failed; terminating instead: %s", exc)

            for method_name in ("terminate", "close"):
                method = getattr(sandbox, method_name, None)
                if not callable(method):
                    continue
                try:
                    method()
                    logger.info("Tenki: terminated sandbox for task %s", self._task_id)
                except Exception as exc:
                    logger.warning("Tenki: cleanup failed: %s", exc)
                return
        finally:
            self._close_client(client)
            with self._lock:
                if self._sandbox is sandbox:
                    self._sandbox = None
                if self._client is client:
                    self._client = None
                self._cleanup_in_progress = False
                self._cleanup_sandbox = None

    def _save_persistent_snapshot(self, sandbox: Any) -> bool:
        snapshot_id: str | None = None
        try:
            snapshot = sandbox.snapshot(name=self._sandbox_name(), wait=True)
            snapshot_id = getattr(snapshot, "id", None) or getattr(snapshot, "snapshot_id", None)
            if snapshot_id and self._client is not None:
                snapshots = getattr(self._client, "snapshots", None)
                wait_durable = getattr(snapshots, "wait_durable", None)
                if callable(wait_durable):
                    try:
                        wait_durable(snapshot_id, timeout=300)
                    except Exception as exc:
                        logger.info("Tenki: snapshot durability wait did not complete: %s", exc)
        except Exception as exc:
            logger.warning("Tenki: filesystem snapshot failed: %s", exc)
            return False
        if not snapshot_id:
            logger.warning("Tenki: snapshot completed without an id; preserving paused sandbox instead")
            return False
        _store_snapshot(self._task_id, snapshot_id)
        logger.info("Tenki: saved filesystem snapshot %s for task %s", snapshot_id, self._task_id)
        return True

    @staticmethod
    def _close_client(client: Any) -> None:
        if client is None:
            return
        close = getattr(client, "close", None)
        if callable(close):
            close()
