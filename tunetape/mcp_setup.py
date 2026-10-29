"""What the MCP screen tells you to set up, and the one step it can do for you.

An agent starts ``tunetape mcp`` (mcp_server.py) from the command its config
names. This works out that command for this install, shows it for Claude Code
and for clients with an ``mcpServers`` JSON config, notices when Claude Code
already has it, and runs ``claude mcp add`` when the user asks.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Optional

NAME = "tunetape"
DOCS = "https://github.com/oauramos/tunetape/blob/main/docs/mcp.md"
EXAMPLES = ("play lofi hip hop", "queue this album after it", "what's playing?")


class Setup(NamedTuple):
    claude_command: str  # the line to run in a shell for Claude Code
    config_json: str  # the mcpServers block for every other client
    claude_found: bool  # Claude Code's CLI is on PATH, so tunetape can add itself
    registered: Optional[str]  # the Claude Code scope that already has tunetape, else None


def server_argv() -> list:
    """The command that starts the MCP server of this very install, absolute so
    GUI apps without the shell's PATH find it too: the ``tunetape`` script
    running now, else the one on PATH, else this interpreter (``python -m``)."""
    running = sys.argv[0] if sys.argv else ""
    if Path(running).stem.lower() == NAME and os.path.isfile(running):
        return [os.path.abspath(running), "mcp"]
    found = shutil.which(NAME)
    if found:
        return [os.path.abspath(found), "mcp"]
    return [sys.executable, "-m", NAME, "mcp"]


def _on_path(program: str) -> bool:
    found = shutil.which(NAME)
    try:
        return bool(found) and os.path.samefile(found, program)
    except OSError:
        return False


def _join(argv: list) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def _add_argv(server: list) -> list:
    return ["mcp", "add", "--scope", "user", NAME, "--", *server]


def claude_command() -> str:
    # Claude Code runs servers with the shell's PATH, so when that finds this
    # same tunetape the short form is the one to show.
    server = server_argv()
    if _on_path(server[0]):
        server = [NAME, "mcp"]
    return _join(["claude", *_add_argv(server)])


def config_json() -> str:
    argv = server_argv()
    entry = json.dumps({"command": argv[0], "args": argv[1:]})
    return '{\n  "mcpServers": {\n    "%s": %s\n  }\n}' % (NAME, entry)


def claude_cli() -> Optional[list]:
    """The argv that runs Claude Code's CLI, or None when it isn't installed."""
    found = shutil.which("claude")
    return [found] if found else None


def _claude_config() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) / ".claude.json" if base else Path.home() / ".claude.json"


def registered(cwd: Optional[str] = None) -> Optional[str]:
    """'user', 'local' or 'project' when Claude Code would already start tunetape
    from ``cwd`` (default: here), else None. Reads the config, never runs claude:
    ``claude mcp list`` starts every server to check on it, which is slow."""
    cwd = os.path.abspath(cwd or os.getcwd())
    try:
        data = json.loads(_claude_config().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if NAME in (data.get("mcpServers") or {}):
        return "user"
    project = (data.get("projects") or {}).get(cwd) or {}
    if isinstance(project, dict) and NAME in (project.get("mcpServers") or {}):
        return "local"
    try:
        shared = json.loads((Path(cwd) / ".mcp.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        shared = {}
    if isinstance(shared, dict) and NAME in (shared.get("mcpServers") or {}):
        return "project"
    return None


def setup() -> Setup:
    return Setup(claude_command(), config_json(), claude_cli() is not None, registered())


def add_to_claude_code() -> tuple:
    """Run ``claude mcp add`` for every project (user scope). Returns (ok, message)."""
    cli = claude_cli()
    if cli is None:
        return False, "Claude Code's `claude` command was not found on PATH."
    try:
        done = subprocess.run([*cli, *_add_argv(server_argv())], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"Could not run claude: {e}"
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        return False, detail[-1] if detail else f"claude exited with {done.returncode}."
    return True, "Added tunetape to Claude Code. Start a new claude session to use it."


def copy(text: str) -> bool:
    """Put ``text`` on the system clipboard with the OS's own tool. False when
    there is none; the screen also sends it over the terminal (OSC 52), which
    covers SSH but not every terminal (macOS Terminal ignores it)."""
    if sys.platform == "darwin":
        tools = [["pbcopy"]]
    elif sys.platform == "win32":
        tools = [["clip"]]
    else:
        tools = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]]
    for argv in tools:
        if not shutil.which(argv[0]):
            continue
        try:
            subprocess.run(argv, input=text, text=True, timeout=5, check=True,
                           capture_output=True)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False
