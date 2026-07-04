from __future__ import annotations

import os
from unittest.mock import patch

from tools import mcp_tool


def test_which_with_subprocess_env_uses_subprocess_pathext_on_windows(monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_tool.os, "name", "nt")
    monkeypatch.setenv("PATHEXT", ".EXE")

    def fake_which(command, path=None):
        seen["command"] = command
        seen["path"] = path
        seen["pathext"] = os.environ.get("PATHEXT")
        return "C:/tools/server.CMD"

    with patch("tools.mcp_tool.shutil.which", side_effect=fake_which):
        resolved = mcp_tool._which_with_subprocess_env(
            "server",
            path="C:/tools",
            env={"PATH": "C:/tools", "PATHEXT": ".CMD"},
        )

    assert resolved == "C:/tools/server.CMD"
    assert seen == {"command": "server", "path": "C:/tools", "pathext": ".CMD"}
    assert os.environ["PATHEXT"] == ".EXE"


def test_resolve_stdio_command_passes_subprocess_path_and_pathext(monkeypatch):
    monkeypatch.setattr(mcp_tool.os, "name", "nt")

    def fake_which(command, path=None):
        assert command == "server"
        assert path == "C:/mcp/bin"
        assert os.environ.get("PATHEXT") == ".BAT"
        return "C:/mcp/bin/server.BAT"

    with patch("tools.mcp_tool.shutil.which", side_effect=fake_which):
        command, env = mcp_tool._resolve_stdio_command(
            "server",
            {"PATH": "C:/mcp/bin", "PATHEXT": ".BAT"},
        )

    assert command == "C:/mcp/bin/server.BAT"
    assert env["PATH"].startswith("C:/mcp/bin")
    assert env["PATHEXT"] == ".BAT"
