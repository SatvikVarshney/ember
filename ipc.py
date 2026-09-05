"""Single-instance control socket for Ember.

A unix socket rather than D-Bus, for one reason: the only thing that has to
cross this boundary is the word "toggle", and a socket lets the hotkey helper
stay free of any gi import. That is the difference between a keypress costing
~20ms and ~250ms, which is very much felt on a launcher.

GTK-free, like runner.py and launcher.py -- `handler` is called on a worker
thread and the caller is responsible for marshalling back to the UI loop.
"""

import os
import pathlib
import socket
import threading

SOCKET_PATH = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "ember.sock"


def send(message, timeout=0.6):
    """True when a running Ember accepted the message."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(SOCKET_PATH))
            sock.sendall((message + "\n").encode())
            return sock.recv(16).strip() == b"ok"
    except (OSError, socket.timeout):
        return False


def _bind():
    """Bind the socket, clearing a stale one. None means a live instance holds it."""
    if SOCKET_PATH.exists():
        # An existing file proves nothing -- a crash leaves one behind. Only a
        # successful ping proves somebody is actually listening.
        if send("ping", timeout=0.3):
            return None
        try:
            SOCKET_PATH.unlink()
        except OSError:
            return None

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(SOCKET_PATH))
    except OSError:
        server.close()
        return None
    server.listen(4)
    os.chmod(SOCKET_PATH, 0o600)
    return server


def serve(handler):
    """Start listening. Returns the server socket, or None if already running."""
    server = _bind()
    if server is None:
        return None

    def loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                try:
                    message = conn.recv(256).decode("utf-8", "replace").strip()
                except OSError:
                    continue
                try:
                    conn.sendall(b"ok\n")
                except OSError:
                    pass
                # Reply first, then act: the helper should never sit waiting on
                # the UI thread finishing an animation.
                if message and message != "ping":
                    handler(message)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return server


def cleanup():
    try:
        SOCKET_PATH.unlink()
    except OSError:
        pass
