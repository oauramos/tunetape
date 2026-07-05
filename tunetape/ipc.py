"""Transport to an mpv ``--input-ipc-server`` instance.

mpv exposes its JSON IPC over a Unix domain socket on macOS/Linux and over a
named pipe on Windows. This module hides that difference behind a small
:class:`MpvIpc` interface so :mod:`tunetape.player` speaks one API regardless
of platform:

    ipc = create_ipc()
    args = [..., f"--input-ipc-server={ipc.server_address}"]
    if ipc.connect(timeout=5.0):
        ipc.send(b'{"command": [...]}\\n')
        chunk = ipc.recv(4096)
    ipc.close()
    ipc.cleanup()

``recv`` raises on timeout / disconnect (mirroring a socket with a read
timeout); callers already wrap sends in a broad ``except``.
"""

import os
import socket
import sys
import tempfile
import time

_WINDOWS = sys.platform == "win32"


class MpvIpc:
    """Common interface for the two transports."""

    #: String to hand mpv via ``--input-ipc-server``.
    server_address = ""
    #: True once :meth:`connect` has succeeded.
    connected = False

    def reset(self):
        """Clear any stale endpoint before a (re)spawn. Default: no-op."""

    def connect(self, timeout: float) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def send(self, data: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def recv(self, n: int) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def cleanup(self) -> None:
        """Remove any filesystem artifacts. Default: no-op."""


class _UnixSocketIpc(MpvIpc):
    """AF_UNIX socket transport (macOS / Linux)."""

    def __init__(self):
        # Private temp dir instead of a predictable /tmp path.
        self._sock_dir = tempfile.mkdtemp(prefix="tunetape_")
        self._sock_path = os.path.join(self._sock_dir, "ipc.sock")
        self._sock = None
        self.server_address = self._sock_path

    def reset(self):
        # A prior failed attempt may have left the socket file behind.
        try:
            if os.path.exists(self._sock_path):
                os.unlink(self._sock_path)
        except OSError:
            pass

    def connect(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(self._sock_path):
                sock = None
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(self._sock_path)
                    sock.settimeout(2.0)
                    self._sock = sock
                    self.connected = True
                    return True
                except (ConnectionRefusedError, FileNotFoundError):
                    if sock is not None:
                        sock.close()
            time.sleep(0.1)
        return False

    def send(self, data: bytes) -> None:
        self._sock.sendall(data)

    def recv(self, n: int) -> bytes:
        return self._sock.recv(n)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
        self.connected = False

    def cleanup(self) -> None:
        try:
            if os.path.exists(self._sock_path):
                os.unlink(self._sock_path)
        except OSError:
            pass
        try:
            if os.path.exists(self._sock_dir):
                os.rmdir(self._sock_dir)
        except OSError:
            pass


class _NamedPipeIpc(MpvIpc):
    """Windows named-pipe transport (via pywin32)."""

    def __init__(self):
        import uuid

        import win32file  # noqa: F401  (import here so POSIX never needs pywin32)

        # A unique pipe name per controller avoids collisions across instances.
        self._pipe_name = r"\\.\pipe\tunetape-" + uuid.uuid4().hex
        self._handle = None
        self._read_timeout = 2.0
        self.server_address = self._pipe_name

    def connect(self, timeout: float) -> bool:
        import pywintypes
        import win32file
        import win32pipe
        import winerror

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                handle = win32file.CreateFile(
                    self._pipe_name,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0, None, win32file.OPEN_EXISTING, 0, None,
                )
            except pywintypes.error as e:
                if e.winerror == winerror.ERROR_FILE_NOT_FOUND:
                    # mpv hasn't created the pipe server yet — wait and retry.
                    time.sleep(0.1)
                    continue
                if e.winerror == winerror.ERROR_PIPE_BUSY:
                    win32pipe.WaitNamedPipe(self._pipe_name, 1000)
                    continue
                time.sleep(0.1)
                continue
            win32pipe.SetNamedPipeHandleState(
                handle, win32pipe.PIPE_READMODE_BYTE, None, None
            )
            self._handle = handle
            self.connected = True
            return True
        return False

    def send(self, data: bytes) -> None:
        import win32file

        win32file.WriteFile(self._handle, data)

    def recv(self, n: int) -> bytes:
        import pywintypes
        import win32file
        import win32pipe

        deadline = time.monotonic() + self._read_timeout
        while True:
            try:
                _, avail, _ = win32pipe.PeekNamedPipe(self._handle, 0)
            except pywintypes.error:
                raise ConnectionError("ipc pipe closed")
            if avail:
                _, data = win32file.ReadFile(self._handle, min(n, avail))
                return data
            if time.monotonic() >= deadline:
                raise TimeoutError("ipc read timeout")
            time.sleep(0.005)

    def close(self) -> None:
        if self._handle is not None:
            try:
                import win32file

                win32file.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
        self.connected = False


def create_ipc() -> MpvIpc:
    """Return the mpv IPC transport appropriate for this platform."""
    if _WINDOWS:
        return _NamedPipeIpc()
    return _UnixSocketIpc()
