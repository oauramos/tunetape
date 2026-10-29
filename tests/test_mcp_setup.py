import json
import os
import sys

import pytest

from tunetape import mcp_setup


@pytest.fixture
def claude_dir(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(path))
    return path


def test_the_config_json_names_this_install(monkeypatch, tmp_path):
    script = tmp_path / "bin" / "tunetape"
    script.parent.mkdir()
    script.write_text("")
    monkeypatch.setattr(sys, "argv", [str(script)])
    config = json.loads(mcp_setup.config_json())
    assert config == {"mcpServers": {"tunetape": {"command": str(script), "args": ["mcp"]}}}


def test_a_checkout_falls_back_to_this_interpreter(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["-m"])
    monkeypatch.setattr(mcp_setup.shutil, "which", lambda name: None)
    assert mcp_setup.server_argv() == [sys.executable, "-m", "tunetape", "mcp"]
    assert mcp_setup.claude_command().startswith("claude mcp add --scope user tunetape -- ")


def test_the_claude_command_is_short_when_path_finds_this_install(monkeypatch, tmp_path):
    script = tmp_path / "tunetape"
    script.write_text("")
    monkeypatch.setattr(sys, "argv", [str(script)])
    monkeypatch.setattr(mcp_setup.shutil, "which", lambda name: str(script))
    assert mcp_setup.claude_command() == "claude mcp add --scope user tunetape -- tunetape mcp"
    # Another tunetape first on PATH: name this one in full.
    other = tmp_path / "other" / "tunetape"
    other.parent.mkdir()
    other.write_text("")
    monkeypatch.setattr(mcp_setup.shutil, "which", lambda name: str(other))
    assert str(script) in mcp_setup.claude_command()


@pytest.mark.parametrize("config, scope", [
    ({}, None),
    ({"mcpServers": {"other": {}}}, None),
    ({"mcpServers": {"tunetape": {"command": "tunetape"}}}, "user"),
    ({"projects": {"HERE": {"mcpServers": {"tunetape": {}}}}}, "local"),
    ({"projects": {"/elsewhere": {"mcpServers": {"tunetape": {}}}}}, None),
    ([], None),
])
def test_registered_reads_claude_codes_config(claude_dir, tmp_path, config, scope):
    here = str(tmp_path)
    text = json.dumps(config).replace("HERE", here.replace("\\", "\\\\"))
    (claude_dir / ".claude.json").write_text(text)
    assert mcp_setup.registered(here) == scope


def test_registered_reads_a_projects_mcp_json(claude_dir, tmp_path):
    assert mcp_setup.registered(str(tmp_path)) is None  # no config at all
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"tunetape": {}}}))
    assert mcp_setup.registered(str(tmp_path)) == "project"


def test_a_broken_config_is_not_registered(claude_dir, tmp_path):
    (claude_dir / ".claude.json").write_text("{not json")
    assert mcp_setup.registered(str(tmp_path)) is None


def fake_claude(tmp_path, code, out=""):
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(tmp_path / 'argv.json')!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        f"sys.stderr.write({out!r})\n"
        f"sys.exit({code})\n")
    return [sys.executable, str(script)]


def test_add_runs_claude_mcp_add(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_setup, "claude_cli", lambda: fake_claude(tmp_path, 0))
    ok, message = mcp_setup.add_to_claude_code()
    assert ok and "Added" in message
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[:6] == ["mcp", "add", "--scope", "user", "tunetape", "--"]
    assert argv[6:] == mcp_setup.server_argv()


def test_add_reports_claudes_error(monkeypatch, tmp_path):
    error = "MCP server tunetape already exists in user config\n"
    monkeypatch.setattr(mcp_setup, "claude_cli", lambda: fake_claude(tmp_path, 1, error))
    assert mcp_setup.add_to_claude_code() == (False, error.strip())


def test_add_without_claude(monkeypatch):
    monkeypatch.setattr(mcp_setup, "claude_cli", lambda: None)
    ok, message = mcp_setup.add_to_claude_code()
    assert not ok and "not found" in message


def test_copy_without_a_clipboard_tool(monkeypatch):
    monkeypatch.setattr(mcp_setup.shutil, "which", lambda name: None)
    assert mcp_setup.copy("x") is False
