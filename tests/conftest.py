import os
import shlex
import subprocess
import sys

import pytest

# test_urls.py is a standalone, network-and-binary manual integration script
# (run directly with `python tests/test_urls.py`), not a pytest module — its
# helper functions take positional args and would mis-collect. Exclude it from
# pytest collection so the default `pytest` run stays fully offline.
collect_ignore = ["test_urls.py"]

FAKES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The fake mpv serves a Unix socket; its Windows named-pipe server is written
# but has not been run on a real Windows machine, so tests that play skip there.
needs_fake_mpv = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fake_mpv's Windows named-pipe IPC is unverified; playback tests run on POSIX",
)
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only behaviour")


def _command(script: str) -> str:
    parts = (sys.executable, os.path.join(FAKES, script))
    if sys.platform == "win32":  # split with posix=False, which keeps only "…" quoting
        return " ".join(f'"{p}"' for p in parts)
    return " ".join(shlex.quote(p) for p in parts)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """An isolated TUNETAPE_DATA_DIR (never the real one), plus fake mpv/yt-dlp.

    The fakes are set even for tests that don't play, so nothing here can ever
    reach the real binaries or the network.
    """
    path = tmp_path / "data"
    monkeypatch.setenv("TUNETAPE_DATA_DIR", str(path))
    monkeypatch.setenv("TUNETAPE_MPV", _command("fake_mpv.py"))
    monkeypatch.setenv("TUNETAPE_YTDLP", _command("fake_ytdlp.py"))
    monkeypatch.delenv("TUNETAPE_DAEMON_TRANSPORT", raising=False)
    monkeypatch.delenv("FAKE_MPV_DURATION", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return path


@pytest.fixture
def audio(tmp_path):
    """``audio("name-fake-dur-1.mp3")`` -> path of an (empty) local audio file."""
    folder = tmp_path / "music"
    folder.mkdir(exist_ok=True)

    def make(name):
        p = folder / name
        p.write_bytes(b"")
        return str(p)

    return make


def pid_alive(pid) -> bool:
    """Whether a process exists, without signalling it.

    Not os.kill(pid, 0): on Windows signal 0 is CTRL_C_EVENT, which would
    interrupt a whole console process group.
    """
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def run_tunetape(*args, env=None, timeout=60):
    """Run ``python -m tunetape …`` with the current (isolated) environment."""
    full_env = dict(os.environ if env is None else env)
    full_env["PYTHONPATH"] = os.pathsep.join(
        p for p in (REPO, full_env.get("PYTHONPATH")) if p)
    return subprocess.run(
        [sys.executable, "-m", "tunetape", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=full_env, timeout=timeout,
    )
