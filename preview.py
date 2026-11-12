"""Private, in-memory JPEG preview transport and an aspect-preserving widget."""
import os
from pathlib import Path
import tempfile

from PyQt6.QtCore import QObject, QRect, QSocketNotifier, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QSizePolicy, QWidget


class JpegFrames:
    """Reassemble JPEGs across pipe reads, keeping only the latest complete frame."""
    MAX_BUFFER = 2 * 1024 * 1024

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        latest = None
        while True:
            start = self.buffer.find(b"\xff\xd8")
            if start < 0:
                self.buffer[:] = self.buffer[-1:] if self.buffer.endswith(b"\xff") else b""
                break
            if start:
                del self.buffer[:start]
            end = self.buffer.find(b"\xff\xd9", 2)
            if end < 0:
                if len(self.buffer) > self.MAX_BUFFER:
                    self.buffer.clear()
                break
            if end + 2 <= self.MAX_BUFFER:
                latest = bytes(self.buffer[:end + 2])
            del self.buffer[:end + 2]
        return latest


class PreviewPipe(QObject):
    frame_ready = pyqtSignal(QImage)
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.directory = None
        self.fd = None
        self.notifier = None
        self.frames = JpegFrames()

    def start(self):
        self.stop()
        try:
            self.directory = tempfile.TemporaryDirectory(prefix="remote-webcam-preview-")
            path = Path(self.directory.name) / "frames"
            os.mkfifo(path, 0o600)
            # O_RDWR avoids EOF/busy polling before GStreamer opens its writer.
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
            self.notifier = QSocketNotifier(self.fd, QSocketNotifier.Type.Read, self)
            self.notifier.activated.connect(self.read_ready)
            return str(path)
        except OSError:
            self.stop()
            raise

    def read_ready(self):
        if self.fd is None:
            return
        latest = None
        try:
            # Bound each callback so a busy camera cannot monopolize the UI.
            for _ in range(32):
                try:
                    data = os.read(self.fd, 65536)
                except BlockingIOError:
                    break
                if not data:
                    break
                frame = self.frames.feed(data)
                if frame is not None:
                    latest = frame
        except OSError as error:
            self.notifier.setEnabled(False)
            self.failed.emit(f"Local preview failed: {error}")
            return
        if latest:
            image = QImage.fromData(latest, "JPEG")
            if not image.isNull():
                self.frame_ready.emit(image)

    def stop(self):
        if self.notifier is not None:
            self.notifier.setEnabled(False)
            self.notifier.deleteLater()
            self.notifier = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None
        self.frames = JpegFrames()


class VideoPreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = QImage()
        self.message = "Start streaming to see your camera here."
        self.setMinimumSize(320, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("Local camera preview")

    def set_image(self, image):
        self.image = image
        self.update()

    def set_state(self, state):
        if state != "Streaming":
            self.image = QImage()
        self.message = ("Start streaming to see your camera here." if state in ("Stopped", "Stopping")
                        else "Waiting for video…")
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0d141e"))
        if self.image.isNull():
            painter.setPen(QColor("#9aabbd"))
            painter.drawText(self.rect().adjusted(20, 20, -20, -20),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.message)
        else:
            size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect((self.width() - size.width()) // 2, (self.height() - size.height()) // 2,
                           size.width(), size.height())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(target, self.image)
