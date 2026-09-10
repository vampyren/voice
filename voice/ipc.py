"""Newline-JSON over a Unix socket: the CLI talks to the daemon with this."""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Callable

from voice import paths

log = logging.getLogger(__name__)

# How long the server waits for a connected client to send its request line.
# A client that connects and never writes (or writes too slowly) must not wedge
# the single-threaded accept loop forever - it gets dropped after this timeout.
CLIENT_READ_TIMEOUT_S = 5.0

# How long the "is a daemon listening?" probe waits for connect() to complete.
CONNECT_PROBE_TIMEOUT_S = 1.0


class IPCError(RuntimeError):
    pass


class Server:
    def __init__(self, handler: Callable[[dict], dict], path: Path | None = None):
        self._handler = handler
        self.path = path or paths.socket_path()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.path.exists():
            # Only a socket nothing is listening on may be unlinked: a busy daemon
            # still accepts connections, and removing its socket would strand it.
            if _listening(self.path):
                raise IPCError("daemon already running")
            self.path.unlink()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self._sock.listen(8)
        self._thread = threading.Thread(target=self._serve, name="ipc-server", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            conn.settimeout(CLIENT_READ_TIMEOUT_S)
            with conn:
                try:
                    data = conn.makefile("rb").readline()
                except (socket.timeout, OSError):
                    # A client connected but never sent a full line (or sent it too
                    # slowly) - drop this connection and keep serving the next one.
                    reply = {"ok": False, "error": "timeout"}
                else:
                    try:
                        request = json.loads(data.decode()) if data else {}
                        reply = self._handler(request)
                    except Exception as exc:
                        log.exception("ipc handler failed")
                        reply = {"ok": False, "error": str(exc)}
                try:
                    conn.sendall((json.dumps(reply) + "\n").encode())
                except OSError:
                    pass

    def stop(self) -> None:
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
        if self.path.exists():
            self.path.unlink()


def send(command: dict, path: Path | None = None, timeout: float = 5.0) -> dict:
    path = path or paths.socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall((json.dumps(command) + "\n").encode())
            line = s.makefile("rb").readline()
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise IPCError("daemon not running") from exc
    except OSError as exc:
        raise IPCError(f"ipc failed: {exc}") from exc
    return json.loads(line.decode()) if line else {"ok": False, "error": "empty reply"}


def _listening(path: Path) -> bool:
    """True when something is accepting connections on `path`.

    A bare connect() is the right probe: the kernel accepts it into the listen
    backlog even while the single-threaded server is stuck inside a handler, so
    a busy daemon cannot be mistaken for a dead one (a ping round-trip could be).
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(CONNECT_PROBE_TIMEOUT_S)
            s.connect(str(path))
        return True
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    except OSError as exc:
        log.debug("socket probe of %s failed: %s", path, exc)
        return False


def is_running(path: Path | None = None) -> bool:
    return _listening(path or paths.socket_path())
