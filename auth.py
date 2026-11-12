"""Qt bridge for OpenSSH's password, keyboard-interactive and host-key prompts."""
import json
from pathlib import Path
import shlex
import sys
import tempfile

from PyQt6.QtCore import QObject, QProcessEnvironment, pyqtSignal
from PyQt6.QtNetwork import QLocalServer


class AskpassBridge(QObject):
    requested = pyqtSignal(str, str)
    dismissed = pyqtSignal()
    answered = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.directory = None
        self.pending = None
        self.clients = set()
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self.connected)

    def start(self):
        self.stop()
        self.directory = tempfile.TemporaryDirectory(prefix="webcam-ssh-")
        root = Path(self.directory.name)
        self.socket_path = str(root / "askpass.sock")
        self.control_path = str(root / "control")
        helper = root / "askpass"
        helper.write_text("#!/bin/sh\nexec " + shlex.join([
            sys.executable, str(Path(__file__).with_name("askpass.py"))]) + ' "$@"\n')
        helper.chmod(0o700)
        if not self.server.listen(self.socket_path):
            message = self.server.errorString()
            self.stop()
            raise OSError(message)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("SSH_ASKPASS", str(helper))
        environment.insert("SSH_ASKPASS_REQUIRE", "force")
        environment.insert("WEBCAM_ASKPASS_SOCKET", self.socket_path)
        return environment

    def connected(self):
        while self.server.hasPendingConnections():
            client = self.server.nextPendingConnection()
            self.clients.add(client)
            client.readyRead.connect(lambda c=client: self.read_request(c))
            client.disconnected.connect(lambda c=client: self.disconnected(c))
            self.read_request(client)

    def read_request(self, client):
        if not client.canReadLine():
            if client.bytesAvailable() > 65536:
                client.abort()
            return
        if self.pending is not None:
            client.abort()
            return
        try:
            request = json.loads(bytes(client.readLine(65536)))
            prompt, kind = request["prompt"], request.get("kind", "")
            if not isinstance(prompt, str) or not isinstance(kind, str):
                raise ValueError("Invalid askpass request")
        except (ValueError, KeyError, TypeError):
            client.abort()
            return
        self.pending = client
        self.requested.emit(prompt, kind)

    def disconnected(self, client):
        if client is self.pending:
            self.pending = None
            self.dismissed.emit()
        self.clients.discard(client)
        client.deleteLater()

    def respond(self, answer):
        client = self.pending
        self.pending = None
        if client is None:
            return
        client.write(json.dumps(answer).encode() + b"\n")
        client.disconnectFromServer()
        self.dismissed.emit()
        self.answered.emit(answer is not None)

    def stop(self):
        self.pending = None
        self.dismissed.emit()
        for client in list(self.clients):
            client.abort()
        self.clients.clear()
        self.server.close()
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None
