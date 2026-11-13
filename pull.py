"""Receiver-initiated WSS transport. Pairing credentials exist only in memory."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import resource
import secrets
import time

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import QHostAddress, QSsl, QSslCertificate, QSslConfiguration, QSslError, QSslKey, QSslSocket
from PyQt6.QtWebSockets import QWebSocket, QWebSocketServer

CHUNK_SIZE = 64 * 1024
MAX_BACKLOG = 512 * 1024
CONTROL_LIMIT = 4096


def validate_mode(message):
    fields = ('width', 'height', 'fps_num', 'fps_den')
    if any(type(message.get(k)) is not int for k in fields):
        raise ValueError('Invalid video mode.')
    width, height, numerator, denominator = (message[k] for k in fields)
    if (not 0 < width <= 8192 or not 0 < height <= 8192 or width % 2 or height % 2
            or width * height > 3840 * 2160 or not 0 < denominator <= 1000000
            or not 0 < numerator <= 120 * denominator):
        raise ValueError('Use an even-sized video mode up to 4K pixels and 120 fps.')
    return dict(zip(fields, (width, height, numerator, denominator)))


def mode_for(config):
    return validate_mode(dict(width=config.width, height=config.height,
                              fps_num=config.fps.numerator, fps_den=config.fps.denominator))


@dataclass(frozen=True, repr=False)
class Pairing:
    port: int
    fingerprint: str
    token: str

    def encode(self):
        return f'CL1.{self.port}.{self.fingerprint}.{self.token}'

    @classmethod
    def parse(cls, value):
        match = re.fullmatch(r'CL1\.(\d{1,5})\.([0-9a-f]{64})\.([A-Za-z0-9_-]{43})', value.strip())
        if not match or not 1 <= int(match[1]) <= 65535:
            raise ValueError('Paste the complete pairing code shown on the sender.')
        return cls(int(match[1]), match[2], match[3])


def tls_identity():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Import lazily: ordinary SSH/SRTP use and the receiver need no key generator.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'CamLink temporary session')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    certificate = QSslCertificate(cert.public_bytes(serialization.Encoding.PEM))
    private_key = QSslKey(key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), QSsl.KeyAlgorithm.Rsa)
    configuration = QSslConfiguration.defaultConfiguration()
    configuration.setProtocol(QSsl.SslProtocol.TlsV1_2OrLater)
    configuration.setLocalCertificate(certificate)
    configuration.setPrivateKey(private_key)
    configuration.setPeerVerifyMode(QSslSocket.PeerVerifyMode.VerifyNone)
    return configuration, hashlib.sha256(bytes(certificate.toDer())).hexdigest()


class Wire(QObject):
    message = pyqtSignal(dict)
    video = pyqtSignal(bytes)
    failed = pyqtSignal(str)
    disconnected = pyqtSignal()

    def __init__(self, socket, parent=None):
        super().__init__(parent)
        self.socket = socket
        socket.setParent(self)
        self.closed = False
        self.last_pong = time.monotonic()
        socket.setMaxAllowedIncomingFrameSize(CHUNK_SIZE)
        socket.setMaxAllowedIncomingMessageSize(CHUNK_SIZE)
        socket.setReadBufferSize(MAX_BACKLOG)
        socket.textMessageReceived.connect(self.read_text)
        socket.binaryMessageReceived.connect(lambda data: self.video.emit(bytes(data)))
        socket.disconnected.connect(self.disconnected)
        socket.errorOccurred.connect(lambda *_: self.fail('Secure connection failed. Check the address, port and network.'))
        socket.pong.connect(self.pong)
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.heartbeat)

    def begin_heartbeat(self):
        self.last_pong = time.monotonic()
        self.timer.start()

    def pong(self, *_):
        self.last_pong = time.monotonic()

    def heartbeat(self):
        if time.monotonic() - self.last_pong > 15:
            self.fail('Connection lost. Start a new session to reconnect.')
        else:
            self.socket.ping(b'camlink')

    def read_text(self, text):
        try:
            if len(text) > CONTROL_LIMIT:
                raise ValueError()
            message = json.loads(text)
            if not isinstance(message, dict) or not isinstance(message.get('type'), str):
                raise ValueError()
        except (ValueError, RecursionError):
            self.fail('Invalid control message.')
            return
        self.message.emit(message)

    def send(self, message):
        if not self.closed:
            self.socket.sendTextMessage(json.dumps(message, separators=(',', ':')))

    def fail(self, message):
        if not self.closed:
            self.failed.emit(message)
            self.close()

    def close(self):
        self.closed = True
        self.timer.stop()
        self.socket.abort()


class PullServer(QObject):
    code_changed = pyqtSignal(str)
    authenticated = pyqtSignal()
    ready = pyqtSignal()
    progress = pyqtSignal(int)
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = None
        self.peer = None
        self.pending = set()
        self.token = ''
        self.code = ''
        self.mode = None
        self.receiving = False
        self.expiry = QTimer(self)
        self.expiry.setSingleShot(True)
        self.expiry.timeout.connect(self.expired)

    def expired(self):
        self.stop()
        self.failed.emit('Pairing code expired. Start a new session.')

    def start(self, mode, port, address=QHostAddress.SpecialAddress.AnyIPv4):
        self.stop()
        self.mode = validate_mode(mode)
        if not QSslSocket.supportsSsl():
            raise ValueError('Qt TLS support is unavailable.')
        configuration, fingerprint = tls_identity()
        self.server = QWebSocketServer('CamLink', QWebSocketServer.SslMode.SecureMode, self)
        self.server.setSslConfiguration(configuration)
        self.server.setHandshakeTimeout(5000)
        self.server.setMaxPendingConnections(4)
        self.server.newConnection.connect(self.accept)
        if not self.server.listen(QHostAddress(address), port):
            self.stop()
            raise ValueError('Cannot listen on that TCP port. Choose an unused port.')
        self.token = secrets.token_urlsafe(32)
        self.code = Pairing(self.server.serverPort(), fingerprint, self.token).encode()
        self.code_changed.emit(self.code)
        self.expiry.start(300000)
        return self.code

    def accept(self):
        while self.server and self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if self.peer or len(self.pending) >= 4 or socket.requestUrl().path() != '/camlink':
                socket.abort()
                socket.deleteLater()
                continue
            wire = Wire(socket, self)
            self.pending.add(wire)
            wire.message.connect(lambda message, w=wire: self.read(w, message))
            wire.video.connect(lambda _, w=wire: w.fail('Unexpected video data.'))
            wire.failed.connect(lambda message, w=wire: self.peer_failed(w, message))
            wire.disconnected.connect(lambda w=wire: self.lost(w))
            deadline = QTimer(wire)
            deadline.setSingleShot(True)
            deadline.timeout.connect(lambda w=wire: w.close() if w in self.pending else None)
            deadline.start(5000)

    def peer_failed(self, wire, message):
        if wire is self.peer:
            self.failed.emit(message)

    def lost(self, wire):
        self.pending.discard(wire)
        if wire is self.peer:
            self.failed.emit('Receiver disconnected. Start a new session to reconnect.')
        wire.close()
        wire.deleteLater()

    def read(self, wire, message):
        kind = message['type']
        if wire in self.pending:
            token = message.get('token')
            if (kind != 'authenticate' or not isinstance(token, str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token) or not self.token
                    or not hmac.compare_digest(token, self.token)):
                wire.close()
                return
            self.pending.remove(wire)
            self.peer = wire
            self.token = self.code = ''
            self.code_changed.emit('')
            self.expiry.stop()
            self.server.close()
            for other in list(self.pending):
                other.close()
            wire.begin_heartbeat()
            wire.send(dict(type='configure', **self.mode))
            self.authenticated.emit()
        elif wire is self.peer:
            if kind == 'ready' and not self.receiving:
                try:
                    if validate_mode(message) != self.mode:
                        raise ValueError()
                except ValueError:
                    wire.fail('Receiver accepted a different video mode.')
                    return
                self.receiving = True
                self.ready.emit()
            elif kind == 'progress' and self.receiving and type(message.get('frames')) is int and 0 < message['frames'] < 2**31:
                self.progress.emit(message['frames'])
            elif kind == 'error':
                # Do not relay arbitrary peer text into a sender log.
                wire.fail('Receiver could not start or continue video. Check its Connection details.')
            else:
                wire.fail('Unexpected receiver control message.')

    def send_video(self, data):
        if not self.peer or not self.receiving:
            return False
        if len(data) > CHUNK_SIZE or self.peer.socket.bytesToWrite() + len(data) > MAX_BACKLOG:
            self.failed.emit('Network cannot keep up. Reduce the resolution or frame rate and reconnect.')
            return False
        return self.peer.socket.sendBinaryMessage(data) == len(data)

    def stop(self):
        self.expiry.stop()
        peer, self.peer = self.peer, None
        self.receiving = False
        self.token = self.code = ''
        self.code_changed.emit('')
        if self.server:
            self.server.close()
            self.server.deleteLater()
            self.server = None
        for wire in [*self.pending, *([peer] if peer else [])]:
            wire.close()
            wire.deleteLater()
        self.pending.clear()


class PullClient(QObject):
    configured = pyqtSignal(dict)
    video = pyqtSignal(bytes)
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.wire = None
        self.offer = None
        self.mode = None
        self.receiving = False

    def start(self, host, code):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        self.stop()
        self.offer = Pairing.parse(code)
        if not re.fullmatch(r'[A-Za-z0-9_.:\[\]-]+', host) or not host:
            raise ValueError('Enter the sender hostname or IP address, without a port.')
        socket = QWebSocket()
        configuration = QSslConfiguration.defaultConfiguration()
        configuration.setProtocol(QSsl.SslProtocol.TlsV1_2OrLater)
        socket.setSslConfiguration(configuration)
        wire = self.wire = Wire(socket, self)
        wire.failed.connect(self.failed)
        wire.disconnected.connect(lambda w=wire: self.failed.emit('Sender disconnected.') if self.wire is w else None)
        wire.message.connect(self.read)
        wire.video.connect(self.read_video)
        socket.sslErrors.connect(self.ssl_errors)
        socket.connected.connect(self.connected)
        url = QUrl()
        url.setScheme('wss')
        url.setHost(host.strip('[]'))
        url.setPort(self.offer.port)
        url.setPath('/camlink')
        socket.open(url)

    def pin_matches(self):
        if not self.wire or not self.offer:
            return False
        cert = self.wire.socket.sslConfiguration().peerCertificate()
        return not cert.isNull() and hmac.compare_digest(
            hashlib.sha256(bytes(cert.toDer())).hexdigest(), self.offer.fingerprint)

    def ssl_errors(self, errors):
        allowed = {QSslError.SslError.SelfSignedCertificate, QSslError.SslError.HostNameMismatch}
        if self.pin_matches() and all(error.error() in allowed for error in errors):
            self.wire.socket.ignoreSslErrors(errors)
        elif self.wire:
            self.wire.fail('Sender identity does not match the pairing code, or its certificate is invalid.')

    def connected(self):
        if not self.pin_matches():
            self.wire.fail('Sender identity does not match the pairing code.')
            return
        self.wire.begin_heartbeat()
        self.wire.send(dict(type='authenticate', token=self.offer.token))
        self.offer = None

    def read(self, message):
        if message['type'] != 'configure' or self.mode is not None:
            self.wire.fail('Unexpected sender control message.')
            return
        try:
            self.mode = validate_mode(message)
        except ValueError as error:
            self.wire.fail(str(error))
            return
        self.configured.emit(self.mode)

    def ready(self):
        if self.wire and self.mode and not self.receiving:
            self.receiving = True
            self.wire.send(dict(type='ready', **self.mode))

    def read_video(self, data):
        if self.receiving:
            self.video.emit(data)
        elif self.wire:
            self.wire.fail('Video arrived before the receiver was ready.')

    def progress(self, frames):
        if self.wire and self.receiving:
            self.wire.send(dict(type='progress', frames=frames))

    def report_error(self):
        if self.wire:
            self.wire.send(dict(type='error'))

    def stop(self):
        wire, self.wire = self.wire, None
        self.offer = self.mode = None
        self.receiving = False
        if wire:
            wire.close()
            wire.deleteLater()
