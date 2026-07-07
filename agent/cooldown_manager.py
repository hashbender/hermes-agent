"""Cooldown manager for LLM provider failover.

Python port of the Nachos llm-proxy CooldownManager (TypeScript source at
nachos/packages/core/llm-proxy/src/cooldowns.ts).

Tracks per-provider-key cooldown state so the fallback chain can skip
providers that recently returned 429 (rate_limit) or 402 (billing) errors,
rather than hammering them again immediately.

Two failure modes:
  - 'rate_limit': exponential backoff in seconds
      cooldown = min(base_seconds * multiplier^(count-1), max_seconds)
  - 'billing':    exponential backoff in hours
      cooldown = min(billing_base_hours * 2^(count-1), billing_max_hours) * 3600

Persistence:
  Cooldown state is persisted to ~/.hermes/cooldowns.json (wall-time UTC
  timestamps) so cooldowns survive process restarts.  Atomic writes (write to
  .tmp then os.replace) guard against corruption.  Pass storage_path=False to
  disable persistence (useful in tests).

Thread-safe: all state mutations are protected by a threading.Lock.

Usage
-----
from agent.cooldown_manager import get_cooldown_manager

mgr = get_cooldown_manager()
mgr.mark_failure("openrouter", "rate_limit")   # record a 429
if mgr.is_cooling("openrouter"):               # True while cooling
    ...
mgr.clear("openrouter")                        # clear after success
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Literal, Optional, Union

logger = logging.getLogger(__name__)


@dataclass
class _CooldownState:
    """Internal per-key state."""
    count: int = 0
    until: float = 0.0   # monotonic timestamp when cooldown expires
    reason: str = "rate_limit"


def build_cooldown_key(provider: str, api_key: Optional[str], reason: str) -> str:
    """Build a cooldown dict key from provider, optional API key, and reason.

    - billing failures → ``provider`` alone (account-wide; pool rotation can't help)
    - rate_limit failures → ``provider:fingerprint`` where fingerprint is the
      first 8 chars of *api_key*.  Falls back to ``provider`` alone when no
      key is available.

    Parameters
    ----------
    provider:
        Provider name, e.g. ``"openrouter"``.
    api_key:
        Active API key string, or ``None`` / empty string if not available.
    reason:
        ``"billing"`` or ``"rate_limit"``.
    """
    provider = (provider or "").strip().lower()
    if reason == "billing" or not api_key:
        return provider
    fingerprint = api_key[:8]
    return f"{provider}:{fingerprint}"


class CooldownManager:
    """Thread-safe cooldown tracker for LLM provider keys.

    Parameters
    ----------
    base_seconds:
        Baseline cooldown duration for the first rate_limit failure (seconds).
    multiplier:
        Exponential multiplier applied on each successive rate_limit failure.
    max_seconds:
        Hard ceiling on rate_limit cooldown duration (seconds).
    billing_base_hours:
        Baseline cooldown for the first billing failure (hours).
    billing_max_hours:
        Hard ceiling on billing cooldown duration (hours).
    storage_path:
        Path to JSON file for disk persistence.  Defaults to
        ``get_hermes_home() / "cooldowns.json"``.  Pass ``False`` to disable
        persistence entirely (useful in tests).
    """

    def __init__(
        self,
        base_seconds: float = 60.0,
        multiplier: float = 5.0,
        max_seconds: float = 3600.0,
        billing_base_hours: float = 5.0,
        billing_max_hours: float = 24.0,
        storage_path: Union[Path, None, "Literal[False]"] = None,
    ) -> None:
        self._base_seconds = base_seconds
        self._multiplier = multiplier
        self._max_seconds = max_seconds
        self._billing_base_hours = billing_base_hours
        self._billing_max_hours = billing_max_hours
        self._states: Dict[str, _CooldownState] = {}
        self._lock = threading.Lock()

        # Resolve storage path
        if storage_path is False:
            self._storage_path: Optional[Path] = None
        elif storage_path is None:
            try:
                from hermes_constants import get_hermes_home
                self._storage_path = get_hermes_home() / "cooldowns.json"
            except Exception:
                self._storage_path = None
        else:
            self._storage_path = Path(storage_path)

        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_cooling(self, key: str) -> bool:
        """Return True if *key* is currently on cooldown."""
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return False
            return time.monotonic() < state.until

    def mark_failure(
        self,
        key: str,
        reason: Literal["rate_limit", "billing"],
    ) -> float:
        """Record a failure for *key* and start/extend its cooldown.

        Returns the number of seconds until the cooldown expires (from now).
        """
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = _CooldownState()
                self._states[key] = state
            state.count += 1
            state.reason = reason

            if reason == "billing":
                hours = min(
                    self._billing_base_hours * (2 ** (state.count - 1)),
                    self._billing_max_hours,
                )
                cooldown_seconds = hours * 3600.0
            else:  # rate_limit
                cooldown_seconds = min(
                    self._base_seconds * (self._multiplier ** (state.count - 1)),
                    self._max_seconds,
                )

            state.until = time.monotonic() + cooldown_seconds

        logger.info(
            "Cooldown: key=%r reason=%s count=%d duration=%.0fs",
            key, reason, state.count, cooldown_seconds,
        )
        self._persist()
        return cooldown_seconds

    def clear(self, key: str) -> None:
        """Remove cooldown state for *key* (e.g. after a successful call)."""
        with self._lock:
            removed = self._states.pop(key, None)
        if removed is not None:
            logger.debug("Cooldown cleared for key=%r", key)
        self._persist()

    def get_all_states(self) -> Dict[str, dict]:
        """Return a snapshot of all cooldown states (for debug/status).

        Each value is a dict with keys:
          - count  (int): number of failures recorded
          - until  (float): monotonic timestamp when cooldown expires
          - cooling (bool): whether the key is currently on cooldown
          - remaining_seconds (float): seconds until expiry (0 if not cooling)
        """
        now = time.monotonic()
        with self._lock:
            snapshot = {}
            for key, state in self._states.items():
                remaining = max(0.0, state.until - now)
                snapshot[key] = {
                    "count": state.count,
                    "until": state.until,
                    "cooling": now < state.until,
                    "remaining_seconds": remaining,
                }
        return snapshot

    def get_cooldown_status(self) -> dict:
        """Return a human-readable summary dict for display/logging.

        Keys:
          - total_tracked (int): number of keys with recorded state
          - cooling (list[str]): keys currently on cooldown
          - expired (list[str]): keys that had cooldowns that already expired
          - details (dict): per-key info from get_all_states()
        """
        states = self.get_all_states()
        cooling = [k for k, v in states.items() if v["cooling"]]
        expired = [k for k, v in states.items() if not v["cooling"] and v["count"] > 0]
        return {
            "total_tracked": len(states),
            "cooling": cooling,
            "expired": expired,
            "details": states,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load persisted cooldown state from disk.

        Converts wall-time ``until_wall`` values to monotonic offsets.
        Entries that have already expired are silently skipped.
        """
        if self._storage_path is None or not self._storage_path.exists():
            return
        try:
            with open(self._storage_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            now_wall = time.time()
            now_mono = time.monotonic()
            with self._lock:
                for key, entry in data.items():
                    until_wall = float(entry.get("until_wall", 0))
                    remaining = until_wall - now_wall
                    if remaining <= 0:
                        continue  # already expired — prune it
                    state = _CooldownState(
                        count=int(entry.get("count", 1)),
                        until=now_mono + remaining,
                        reason=str(entry.get("reason", "rate_limit")),
                    )
                    self._states[key] = state
        except Exception as exc:
            logger.warning("Failed to load cooldown state from %s: %s", self._storage_path, exc)

    def _persist(self) -> None:
        """Write active (non-expired) cooldown state to disk atomically."""
        if self._storage_path is None:
            return
        now_mono = time.monotonic()
        now_wall = time.time()
        try:
            with self._lock:
                data = {}
                for key, state in self._states.items():
                    remaining = state.until - now_mono
                    if remaining <= 0:
                        continue  # expired, omit
                    data[key] = {
                        "reason": state.reason,
                        "count": state.count,
                        "until_wall": now_wall + remaining,
                    }
            # Atomic write: write to .tmp then os.replace
            tmp_path = self._storage_path.with_suffix(".json.tmp")
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp_path, self._storage_path)
        except Exception as exc:
            logger.warning("Failed to persist cooldown state to %s: %s", self._storage_path, exc)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_singleton: Optional[CooldownManager] = None
_singleton_lock = threading.Lock()


def get_cooldown_manager() -> CooldownManager:
    """Return the process-wide CooldownManager singleton.

    The singleton is initialized with defaults on first call.  Tests may
    replace it via :func:`set_cooldown_manager` or simply instantiate their
    own :class:`CooldownManager` directly.
    """
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = CooldownManager()
    return _singleton


def set_cooldown_manager(manager: CooldownManager) -> None:
    """Replace the module-level singleton (useful for tests or custom config)."""
    global _singleton
    with _singleton_lock:
        _singleton = manager
