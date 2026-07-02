"""Wire-protocol dataclasses for the dispatcher IPC client.

JSON line-delimited envelope over a Unix domain socket. The gateway
uses this module to construct and parse envelopes when forwarding
slash commands to an external dispatcher process. The wire spec is
intentionally simple: a JSON object per line, newline-terminated.

If the dispatcher server changes the wire shape, this module MUST
be updated in the same commit to keep client and server in sync.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass


# --- status codes ---

STATUS_OK = 0
STATUS_BAD_REQUEST = 1   # malformed / unknown op
STATUS_INTERNAL = 2      # handler raised
STATUS_BUSY = 3          # handler max_inflight reached


# --- ops ---

OP_DISPATCH = "dispatch"
OP_PING = "ping"
OP_SHUTDOWN = "shutdown"


# --- envelope ---

@dataclass(frozen=True)
class Envelope:
    """One wire message. Either request (status absent) or response
    (status present, required). The server is the authority on
    validation; clients must handle STATUS_BAD_REQUEST gracefully.
    """

    request_id: str
    op: str
    payload: dict
    status: int | None = None

    def to_jsonl(self) -> bytes:
        """Serialize as one JSON line (newline-terminated)."""
        d = {
            "request_id": self.request_id,
            "op": self.op,
            "payload": self.payload,
        }
        if self.status is not None:
            d["status"] = self.status
        return (json.dumps(d, ensure_ascii=False) + "\n").encode("utf-8")

    @classmethod
    def from_jsonl(cls, line: bytes) -> "Envelope":
        """Parse one JSON line into an Envelope. Raises ValueError on
        malformed JSON or missing required fields."""
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise ValueError(
                f"envelope must be a JSON object, got {type(obj).__name__}"
            )
        for required in ("request_id", "op", "payload"):
            if required not in obj:
                raise ValueError(f"missing required field {required!r}")
        if not isinstance(obj["request_id"], str):
            raise ValueError("request_id must be a string")
        if not isinstance(obj["op"], str):
            raise ValueError("op must be a string")
        if not isinstance(obj["payload"], dict):
            raise ValueError("payload must be a JSON object")
        status = obj.get("status")
        if status is not None and (
            isinstance(status, bool) or not isinstance(status, int)
        ):
            raise ValueError(
                f"status must be int or absent, got {type(status).__name__}"
            )
        return cls(
            request_id=obj["request_id"],
            op=obj["op"],
            payload=obj["payload"],
            status=status,
        )


# --- helpers ---

def new_request_id() -> str:
    """Generate a request_id for a new request. UUID4 hex (32 chars)."""
    return uuid.uuid4().hex


def make_request(op: str, payload: dict | None = None) -> Envelope:
    """Build a fresh request envelope with a generated request_id."""
    return Envelope(
        request_id=new_request_id(),
        op=op,
        payload=payload if payload is not None else {},
    )
