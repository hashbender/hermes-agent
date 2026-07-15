"""``hermes terminal-setup`` subcommand parser.

Guides the user through configuring their terminal emulator for native
Shift+Enter newline support via the Kitty keyboard protocol (CSI-u).
"""

from __future__ import annotations

from typing import Callable


def build_terminal_setup_parser(subparsers, *, cmd_terminal_setup: Callable) -> None:
    """Attach the ``terminal-setup`` subcommand to ``subparsers``."""
    # =========================================================================
    # terminal-setup command
    # =========================================================================
    ts_parser = subparsers.add_parser(
        "terminal-setup",
        help="Configure your terminal for native Shift+Enter newline support",
        description=(
            "Interactive wizard that guides you through enabling the Kitty\n"
            "keyboard protocol (CSI-u) in your terminal emulator so that\n"
            "Shift+Enter inserts a newline in Hermes without submitting.\n\n"
            "Supported terminals:\n"
            "  iTerm2       — full wizard: GlobalKeyMap check + CSI-u instructions\n"
            "  Terminal.app — unsupported; explains Alt+Enter fallback\n"
            "  kitty        — already works; confirms and advises\n"
            "  WezTerm      — already works; confirms and advises\n"
            "  Ghostty      — already works; confirms and advises\n"
            "  VS Code      — directs to the /terminal-setup slash command\n\n"
            "Alt+Enter is always available as an unconditional fallback."
        ),
    )
    ts_parser.set_defaults(func=cmd_terminal_setup)
