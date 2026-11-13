"""Locally launched Linux/macOS receiver; connects outward over WSS."""
from fractions import Fraction
import json
from pathlib import Path
import resource
import shutil
import sys

from PyQt6.QtCore import QObject, QProcess, QSettings, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from pull import MAX_BACKLOG, PullClient, mode_for, validate_mode
from remote import configure_script, requested_format, validate_remote_format
from stream import REMOTE_SETUP, StreamConfig, decoder_arguments


class ReceiverController(QObject):
    state_changed = pyqtSignal(str)
    failed = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, parent=None, backend=None):
        super().__init__(parent)
        self.backend = backend or sys.platform
        self.state = 'Stopped'
        self.client = PullClient(self)
        self.client.configured.connect(self.configure)
        self.client.video.connect(self.feed)
        self.client.failed.connect(self.fail)
        self.processes = []
        self.decoder = None
        self.config = None
        self.outputs = {}
        self.lines = {}
        self.last_frame = 0
        self.watchdog = QTimer(self)
        self.watchdog.setSingleShot(True)
        self.watchdog.timeout.connect(lambda: self.fail('Connection or video timed out. Check the sender and reconnect with a new code.'))
        self.kill_timer = QTimer(self)
        self.kill_timer.setSingleShot(True)
        self.kill_timer.timeout.connect(self.force_stop)

    def set_state(self, state):
        self.state = state
        self.state_changed.emit(state)

    def start(self, host, code):
        if self.state != 'Stopped':
            return
        if self.backend not in ('linux', 'darwin'):
            self.failed.emit('The receiver supports Linux and macOS.')
            return
        if not shutil.which('ffmpeg'):
            self.failed.emit('Install FFmpeg before starting the receiver.')
            return
        for process in self.processes:
            process.deleteLater()
        self.processes = []
        self.outputs = {}
        self.lines = {}
        self.decoder = self.config = None
        self.last_frame = 0
        self.set_state('Connecting')
        try:
            self.client.start(host.strip(), code)
        except (ValueError, OSError) as error:
            self.fail(str(error))
            return
        self.watchdog.start(20000)

    def configure(self, mode):
        if self.state != 'Connecting':
            self.fail('Unexpected video configuration.')
            return
        self.config = StreamConfig('1', mode['width'], mode['height'],
                                   Fraction(mode['fps_num'], mode['fps_den']), receiver='localhost')
        self.set_state('Preparing camera')
        self.watchdog.start(30000)
        if self.backend == 'linux':
            self.run_script('Camera setup', REMOTE_SETUP +
                            f'v4l2-ctl -d /dev/video42 --try-fmt-video-out={requested_format(self.config)}\n')
        else:
            self.start_decoder()

    def process(self, name):
        process = QProcess(self)
        process.setObjectName(name)
        process.readyReadStandardOutput.connect(lambda p=process: self.read_output(p))
        process.readyReadStandardError.connect(lambda p=process: self.read_error(p))
        process.errorOccurred.connect(lambda _, p=process: self.process_error(p))
        process.finished.connect(lambda code, _, p=process: self.finished(p, code))
        self.processes.append(process)
        return process

    def run_script(self, name, script):
        process = self.process(name)
        process.started.connect(lambda: (process.write(script.encode()), process.closeWriteChannel()))
        process.start('bash', ['-c', 'exec bash -s'])

    def start_decoder(self):
        process = self.decoder = self.process('Video output')
        self.set_state('Waiting for video')
        self.watchdog.start(20000)
        if self.backend == 'linux':
            process.started.connect(self.client.ready)
            arguments = decoder_arguments(self.config)
            process.start(arguments[0], arguments[1:])
        else:
            process.start(sys.executable, ['-u', str(Path(__file__).with_name('obs_receiver.py')),
                          str(self.config.width), str(self.config.height),
                          str(self.config.fps.numerator), str(self.config.fps.denominator)])

    def feed(self, data):
        if self.state in ('Stopped', 'Stopping'):
            return
        if self.decoder is None or self.decoder.state() != QProcess.ProcessState.Running:
            self.fail('Video output is not ready.')
        elif self.decoder.bytesToWrite() + len(data) > MAX_BACKLOG:
            self.fail('Video output cannot keep up. Reduce resolution or frame rate and reconnect.')
        elif self.decoder.write(data) != len(data):
            self.fail('Could not deliver video to the virtual camera.')

    def read_output(self, process):
        if process not in self.processes:
            return
        text = bytes(process.readAllStandardOutput()).decode(errors='replace')
        if process is self.decoder and self.backend == 'darwin':
            self.read_lines(process, text, True)
        else:
            name = process.objectName()
            self.outputs[name] = (self.outputs.get(name, '') + text)[-65536:]

    def read_error(self, process):
        if process not in self.processes:
            return
        text = bytes(process.readAllStandardError()).decode(errors='replace')
        if process is self.decoder and self.backend == 'linux':
            self.read_lines(process, text, False)
        elif text.strip():
            self.log.emit(text[-8192:].strip())

    def read_lines(self, process, text, structured):
        pending = self.lines.get(process, '') + text
        if len(pending) > 65536:
            self.fail('Invalid video output status.')
            return
        while '\n' in pending:
            line, pending = pending.split('\n', 1)
            if structured:
                try:
                    message = json.loads(line)
                    if message['type'] == 'ready':
                        if validate_mode(message) != mode_for(self.config):
                            raise ValueError()
                        self.client.ready()
                    elif message['type'] == 'progress' and type(message.get('frames')) is int:
                        self.progress(message['frames'])
                    elif message['type'] == 'error':
                        self.fail(str(message.get('message', 'Virtual camera failed.'))[:2048])
                    else:
                        raise ValueError()
                except (ValueError, KeyError, TypeError):
                    self.fail('Invalid virtual camera status or video mode.')
            elif line.startswith('frame='):
                try:
                    self.progress(int(line.split('=', 1)[1]))
                except ValueError:
                    pass
            elif line and '=' not in line:
                self.log.emit(line[:2048])
        self.lines[process] = pending

    def progress(self, frames):
        if frames <= self.last_frame or self.state in ('Stopped', 'Stopping'):
            return
        self.last_frame = frames
        if self.state == 'Waiting for video' and self.backend == 'linux':
            self.set_state('Checking video')
            self.watchdog.start(10000)
            self.run_script('Video verification', 'LC_ALL=C v4l2-ctl -d /dev/video42 --get-fmt-video\n')
        elif self.state != 'Checking video':
            self.set_state('Streaming')
            self.watchdog.start(8000)
            self.client.progress(frames)

    def process_error(self, process):
        if process in self.processes and self.state not in ('Stopped', 'Stopping'):
            self.fail(f'{process.objectName()}: {process.errorString()}')
        QTimer.singleShot(0, self.finish_stop)

    def finished(self, process, code):
        if process not in self.processes:
            return
        self.read_output(process)
        self.read_error(process)
        if self.state == 'Stopping':
            self.finish_stop()
            return
        if self.state == 'Stopped':
            return
        name = process.objectName()
        if code != 0 or process is self.decoder:
            self.fail(f'{name} exited (code {code}). Check Connection details.')
            return
        try:
            validate_remote_format(self.config, self.outputs.get(name, ''))
        except ValueError as error:
            self.fail(str(error))
            return
        if name == 'Camera setup':
            self.run_script('Camera configuration', configure_script(self.config))
        elif name == 'Camera configuration':
            self.start_decoder()
        elif name == 'Video verification':
            self.set_state('Streaming')
            self.watchdog.start(8000)
            self.client.progress(self.last_frame)

    def fail(self, message):
        if self.state not in ('Stopped', 'Stopping'):
            self.failed.emit(message)
            self.client.report_error()
            self.stop()

    def stop(self):
        if self.state in ('Stopped', 'Stopping'):
            return
        self.set_state('Stopping')
        self.watchdog.stop()
        self.client.stop()
        for process in self.processes:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.closeWriteChannel()
                if process is not self.decoder:
                    process.terminate()
        self.kill_timer.start(2000)
        self.finish_stop()

    def force_stop(self):
        for process in self.processes:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
        self.finish_stop()

    def finish_stop(self):
        if self.state == 'Stopping' and all(p.state() == QProcess.ProcessState.NotRunning for p in self.processes):
            self.kill_timer.stop()
            self.decoder = self.config = None
            self.set_state('Stopped')


class ReceiverWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName('receiverWindow')
        self.setWindowTitle('CamLink Receiver')
        self.setWindowIcon(QIcon(str(Path(__file__).with_name('webcam.svg'))))
        self.resize(620, 430)
        self.closing = False
        self.settings = QSettings('LocalTools', 'CamLinkReceiver')
        self.controller = ReceiverController(self)
        layout = QVBoxLayout(self)
        title = QLabel('CamLink Receiver')
        title.setObjectName('title')
        layout.addWidget(title)
        description = QLabel('Connect to the camera sender on your LAN or VPN. Enter the one-time code shown there.')
        description.setWordWrap(True)
        layout.addWidget(description)
        form = QFormLayout()
        self.host = QLineEdit(self.settings.value('host', ''))
        self.host.setPlaceholderText('Sender hostname or IP address')
        self.code = QLineEdit()
        self.code.setMaxLength(256)
        self.code.setEchoMode(QLineEdit.EchoMode.Password)
        self.code.setPlaceholderText('Paste the complete pairing code')
        form.addRow('Sender', self.host)
        form.addRow('Pairing code', self.code)
        layout.addLayout(form)
        output = 'OBS Virtual Camera' if sys.platform == 'darwin' else 'Remote Webcam'
        layout.addWidget(QLabel(f'Select “{output}” in Zoom after video starts.'))
        self.status = QLabel('Stopped')
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.connect_button = QPushButton('Connect')
        self.stop_button = QPushButton('Stop')
        self.stop_button.setEnabled(False)
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.stop_button)
        layout.addLayout(buttons)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumBlockCount(200)
        layout.addWidget(QLabel('Connection details'))
        layout.addWidget(self.details)
        self.connect_button.clicked.connect(self.connect_receiver)
        self.stop_button.clicked.connect(self.controller.stop)
        self.controller.state_changed.connect(self.state_changed)
        self.controller.log.connect(self.details.appendPlainText)
        self.controller.failed.connect(self.details.appendPlainText)

    def connect_receiver(self):
        self.details.clear()
        self.settings.setValue('host', self.host.text().strip())
        code = self.code.text()
        self.code.clear()
        self.controller.start(self.host.text(), code)

    def state_changed(self, state):
        self.status.setText(state)
        self.details.appendPlainText(state)
        idle = state == 'Stopped'
        for widget in (self.host, self.code, self.connect_button):
            widget.setEnabled(idle)
        self.stop_button.setEnabled(state not in ('Stopped', 'Stopping'))
        if idle and self.closing:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        self.closing = True
        self.code.clear()
        if self.controller.state != 'Stopped':
            event.ignore()
            self.controller.stop()
        else:
            event.accept()


def main():
    from app import STYLE
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    application = QApplication(sys.argv)
    application.setApplicationName('CamLink Receiver')
    application.setDesktopFileName('camlink-receiver')
    application.setStyle('Fusion')
    application.setStyleSheet(STYLE)
    window = ReceiverWindow()
    window.show()
    return application.exec()


if __name__ == '__main__':
    raise SystemExit(main())
