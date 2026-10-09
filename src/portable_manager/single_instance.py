"""Ensure only one copy of the app runs; later launches ask the running copy to show itself."""

from __future__ import annotations

import getpass
import logging
import os
import re

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

_ACTIVATE_LINE = b"activate"


class SingleInstance(QObject):
    """Named-local-socket guard around a single running instance.

    The first process to call :meth:`try_acquire` listens on ``key``. Any later
    process connects, sends an activation request and gets ``False`` back.
    """

    activation_requested = Signal()

    def __init__(self, key: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._key = key
        self._server: QLocalServer | None = None
        # Partial input per incoming connection, keyed by socket.
        self._buffers: dict[QLocalSocket, bytes] = {}

    def try_acquire(self) -> bool:
        """Return True if this process is the primary instance."""
        probe = QLocalSocket()
        probe.connectToServer(self._key)
        if probe.waitForConnected(300):
            probe.write(_ACTIVATE_LINE + b"\n")
            probe.flush()
            probe.waitForBytesWritten(500)
            probe.disconnectFromServer()
            return False

        # Nothing is listening. Clear any socket left behind by a crash, then listen.
        QLocalServer.removeServer(self._key)
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self._key):
            # Never block startup because of this feature.
            log.warning(
                "Could not listen on single-instance socket %s: %s",
                self._key,
                server.errorString(),
            )
            server.deleteLater()
            return True

        server.newConnection.connect(self._on_new_connection)
        self._server = server
        return True

    def release(self) -> None:
        """Stop listening and drop any open connections."""
        for conn in list(self._buffers):
            conn.abort()
        self._buffers.clear()
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None
            QLocalServer.removeServer(self._key)

    def _on_new_connection(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            if conn is None:
                break
            conn.setParent(self)
            self._buffers[conn] = b""
            conn.readyRead.connect(lambda c=conn: self._consume(c))
            conn.disconnected.connect(lambda c=conn: self._on_disconnected(c))
            # Data may already be buffered by the time the connection is accepted.
            self._consume(conn)

    def _consume(self, conn: QLocalSocket, final: bool = False) -> None:
        if conn not in self._buffers:
            return
        data = self._buffers[conn] + bytes(conn.readAll())
        *lines, rest = data.split(b"\n")
        if final:
            lines.append(rest)
            rest = b""
        self._buffers[conn] = rest
        for line in lines:
            if line.strip() == _ACTIVATE_LINE:
                self.activation_requested.emit()

    def _on_disconnected(self, conn: QLocalSocket) -> None:
        # Process anything the peer sent without a trailing newline.
        self._consume(conn, final=True)
        self._buffers.pop(conn, None)
        conn.deleteLater()


def default_instance_key() -> str:
    """Per-user socket name, so separate Windows accounts do not block each other."""
    username = os.environ.get("USERNAME", "")
    if not username:
        try:
            username = getpass.getuser()
        except Exception:
            username = ""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", username)
    return "PortableProgramManager-" + safe
