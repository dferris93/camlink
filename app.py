#!/usr/bin/env python3
"""Desktop controls for sharing a laptop/USB webcam over SSH or RTP/UDP."""
import json
import resource
from fractions import Fraction
from pathlib import Path
import sys

from PyQt6.QtCore import QProcess, QSettings, QTimer, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPlainTextEdit, QPushButton, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)

from cameras import parse_cameras, rate_text
from stream import StreamConfig, StreamController
from preview import VideoPreview


class WebcamWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CamLink")
        self.setWindowIcon(QIcon(str(Path(__file__).with_name("webcam.svg"))))
        self.resize(1080, 650)
        self.setMinimumWidth(900)
        # Keep the legacy storage ID so upgrades retain saved preferences.
        self.settings = QSettings("LocalTools", "LaptopWebcam")
        self.cameras = []
        self.closing = False
        self.auth_dialog = None
        self.controller = StreamController(self)
        self.controller.state_changed.connect(self.state_changed)
        self.controller.log.connect(self.append_log)
        self.controller.failed.connect(self.show_error)
        self.controller.authentication_requested.connect(self.ask_authentication)
        self.controller.authentication_dismissed.connect(self.dismiss_authentication)

        body = QWidget()
        self.setCentralWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        heading = QHBoxLayout()
        title = QLabel("CamLink")
        title.setObjectName("title")
        heading.addWidget(title)
        heading.addStretch()
        self.status = QLabel("Stopped")
        self.status.setObjectName("status")
        heading.addWidget(self.status)
        layout.addLayout(heading)
        subtitle = QLabel("Share a camera with your remote computer.")
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)

        content = QHBoxLayout()
        content.setSpacing(18)
        controls = QVBoxLayout()
        controls.setSpacing(16)
        content.addLayout(controls, 1)
        layout.addLayout(content, 1)

        card = QFrame()
        card.setObjectName("card")
        form = QFormLayout(card)
        form.setContentsMargins(20, 20, 20, 20)
        form.setSpacing(14)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.camera_box = QComboBox()
        self.camera_box.setMinimumContentsLength(16)
        self.camera_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        camera_row = QHBoxLayout()
        camera_row.addWidget(self.camera_box, 1)
        camera_row.addWidget(self.refresh_button)
        form.addRow("Camera", camera_row)
        self.resolution_box = QComboBox()
        self.fps_box = QComboBox()
        form.addRow("Resolution", self.resolution_box)
        form.addRow("Frame rate", self.fps_box)
        self.mode_hint = QLabel()
        self.mode_hint.setWordWrap(True)
        self.mode_hint.setObjectName("hint")
        form.addRow("", self.mode_hint)
        controls.addWidget(card)

        receiver_card = QFrame()
        receiver_card.setObjectName("card")
        receiver_form = QFormLayout(receiver_card)
        receiver_form.setContentsMargins(20, 18, 20, 18)
        self.receiver_edit = QLineEdit(self.settings.value("receiver", ""))
        self.receiver_edit.setPlaceholderText("user@hostname")
        self.receiver_edit.setToolTip("SSH destination containing the Remote Webcam virtual camera.")
        receiver_form.addRow("Remote computer", self.receiver_edit)
        self.transport_box = QComboBox()
        self.transport_box.addItem("SSH", "ssh")
        self.transport_box.addItem("Secure RTP (UDP)", "rtp")
        self.transport_box.setCurrentIndex(max(0, self.transport_box.findData(self.settings.value("transport", "ssh"))))
        receiver_form.addRow("Video transport", self.transport_box)
        self.udp_host_edit = QLineEdit(self.settings.value("udp_host", ""))
        self.udp_host_edit.setPlaceholderText("Remote computer’s address (automatic)")
        self.udp_host_edit.setToolTip("Optional direct UDP address, for a different network interface or an SSH jump host.")
        self.udp_port_box = QSpinBox()
        self.udp_port_box.setRange(1024, 65535)
        self.udp_port_box.setSingleStep(1)
        self.udp_port_box.setValue(self.settings.value("udp_port", 5004, type=int))
        receiver_form.addRow("RTP destination", self.udp_host_edit)
        receiver_form.addRow("RTP port", self.udp_port_box)
        self.transport_hint = QLabel()
        self.transport_hint.setWordWrap(True)
        self.transport_hint.setObjectName("hint")
        receiver_form.addRow("", self.transport_hint)
        self.receiver_form = receiver_form
        self.transport_box.currentIndexChanged.connect(self.transport_changed)
        self.transport_changed()
        receiver_hint = QLabel('Appears as “Remote Webcam”. Release it in Zoom before changing resolution.')
        receiver_hint.setWordWrap(True)
        receiver_hint.setObjectName("hint")
        receiver_form.addRow("", receiver_hint)
        controls.addWidget(receiver_card)
        controls.addStretch()

        preview_card = QFrame()
        preview_card.setObjectName("card")
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(16, 16, 16, 16)
        preview_layout.setSpacing(12)
        preview_layout.addWidget(QLabel("Local preview"))
        self.preview = VideoPreview()
        preview_layout.addWidget(self.preview, 1)
        preview_hint = QLabel("Same view as the remote camera.")
        preview_hint.setObjectName("hint")
        preview_layout.addWidget(preview_hint)
        content.addWidget(preview_card, 1)
        self.controller.preview_frame.connect(self.preview.set_image)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setObjectName("error")
        self.error_label.hide()
        layout.addWidget(self.error_label)
        actions = QHBoxLayout()
        self.summary = QLabel("Choose your camera and video settings.")
        self.summary.setWordWrap(True)
        actions.addWidget(self.summary, 1)
        self.start_button = QPushButton("Start streaming")
        self.start_button.setObjectName("primary")
        self.start_button.setMinimumWidth(148)
        self.start_button.clicked.connect(self.start_stream)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.controller.stop)
        self.stop_button.setEnabled(False)
        actions.addWidget(self.start_button)
        actions.addWidget(self.stop_button)
        layout.addLayout(actions)
        self.details_button = QToolButton()
        self.details_button.setText("Connection details")
        self.details_button.setCheckable(True)
        self.details_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_button.setArrowType(Qt.ArrowType.RightArrow)
        self.details_button.toggled.connect(self.toggle_details)
        layout.addWidget(self.details_button)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(300)
        self.log_box.setMinimumHeight(150)
        self.log_box.hide()
        layout.addWidget(self.log_box)

        self.camera_box.currentIndexChanged.connect(self.camera_changed)
        self.resolution_box.currentIndexChanged.connect(self.resolution_changed)
        self.fps_box.currentIndexChanged.connect(self.update_summary)
        self.discovery = QProcess(self)
        self.discovery.finished.connect(self.discovered)
        self.discovery.errorOccurred.connect(self.discovery_error)
        self.discovery_timer = QTimer(self)
        self.discovery_timer.setSingleShot(True)
        self.discovery_timer.timeout.connect(self.discovery_timeout)
        self.refresh()

    def append_log(self, text):
        self.log_box.appendPlainText(text)

    def transport_changed(self):
        rtp = self.transport_box.currentData() == "rtp"
        self.receiver_form.setRowVisible(self.udp_host_edit, rtp)
        self.receiver_form.setRowVisible(self.udp_port_box, rtp)
        self.transport_hint.setText(
            "Encrypted, authenticated UDP video. A fresh session key is shared over SSH each time you start."
            if rtp else "Encrypted video over SSH. Password and verification-code prompts appear when needed.")

    def ask_authentication(self, prompt, kind):
        self.dismiss_authentication()
        dialog = QDialog(self)
        self.auth_dialog = dialog
        dialog.setWindowTitle("SSH sign in" if kind != "confirm" else "SSH host confirmation")
        dialog.setMinimumWidth(460)
        layout = QVBoxLayout(dialog)
        remote = QLabel(f"Connecting to {self.controller.config.receiver}")
        remote.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(remote)
        label = QLabel(prompt)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label)
        field = QLineEdit()
        field.setEchoMode(QLineEdit.EchoMode.Password)
        field.setPlaceholderText("Password, key passphrase, or verification code")
        if kind not in ("confirm", "none"):
            layout.addWidget(field)
            field.returnPressed.connect(dialog.accept)
        standard = QDialogButtonBox.StandardButton
        buttons = QDialogButtonBox(standard.Yes | standard.No if kind == "confirm" else standard.Ok | standard.Cancel)
        if kind == "confirm":
            buttons.button(standard.Yes).clicked.connect(dialog.accept)
            buttons.button(standard.No).clicked.connect(dialog.reject)
            buttons.button(standard.No).setDefault(True)
        else:
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def finished(result):
            answer = ("yes" if kind == "confirm" else field.text()) if result == QDialog.DialogCode.Accepted else None
            field.clear()
            self.controller.auth.respond(answer)

        dialog.finished.connect(finished)
        dialog.open()
        if kind not in ("confirm", "none"):
            field.setFocus()

    def dismiss_authentication(self):
        if self.auth_dialog is not None:
            dialog, self.auth_dialog = self.auth_dialog, None
            dialog.blockSignals(True)
            for field in dialog.findChildren(QLineEdit):
                field.clear()
            dialog.reject()
            dialog.deleteLater()

    def show_error(self, text):
        self.error_label.setText(text)
        self.error_label.show()
        self.append_log(text)
        self.details_button.setChecked(True)

    def toggle_details(self, visible):
        self.log_box.setVisible(visible)
        self.details_button.setArrowType(Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow)

    def refresh(self):
        if self.controller.state != "Stopped" or self.discovery.state() != QProcess.ProcessState.NotRunning:
            return
        self.error_label.hide()
        self.refresh_button.setEnabled(False)
        self.start_button.setEnabled(False)
        self.summary.setText("Looking for cameras…")
        self.discovery.start("pw-dump", [])
        self.discovery_timer.start(8000)

    def discovery_timeout(self):
        self.discovery.kill()
        self.show_error("Camera discovery timed out. Check that PipeWire is running.")

    def discovery_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.discovery_timer.stop()
            self.refresh_button.setEnabled(True)
            self.show_error("Cannot run pw-dump. Install PipeWire tools and reopen the app.")

    def discovered(self, code, status):
        self.discovery_timer.stop()
        if self.closing:
            return
        self.refresh_button.setEnabled(True)
        old_name = self.camera_box.currentData() or self.settings.value("camera", "")
        self.camera_box.blockSignals(True)
        self.camera_box.clear()
        self.cameras = []
        try:
            if code != 0:
                detail = bytes(self.discovery.readAllStandardError()).decode(errors="replace").strip()
                raise ValueError(f"Camera discovery failed. {detail}")
            self.cameras = parse_cameras(json.loads(bytes(self.discovery.readAllStandardOutput())))
            for camera in self.cameras:
                self.camera_box.addItem(f"{camera.description} · {camera.kind}", camera.name)
            index = self.camera_box.findData(old_name)
            self.camera_box.setCurrentIndex(index if index >= 0 else 0)
        except (ValueError, TypeError, KeyError) as error:
            self.show_error(str(error))
        finally:
            self.camera_box.blockSignals(False)
        self.camera_changed()
        self.start_button.setEnabled(bool(self.cameras))
        if not self.cameras:
            self.summary.setText("No cameras found. Connect a webcam and click Refresh.")

    def camera_changed(self):
        self.resolution_box.blockSignals(True)
        self.resolution_box.clear()
        index = self.camera_box.currentIndex()
        if 0 <= index < len(self.cameras):
            for mode in self.cameras[index].modes:
                self.resolution_box.addItem(f"{mode.width} × {mode.height}", mode)
            wanted = self.settings.value("resolution", "1280x720")
            selected = next((i for i, mode in enumerate(self.cameras[index].modes)
                             if f"{mode.width}x{mode.height}" == wanted), -1)
            if selected < 0:
                selected = next((i for i, mode in enumerate(self.cameras[index].modes)
                                 if (mode.width, mode.height) == (1280, 720)), 0)
            self.resolution_box.setCurrentIndex(selected)
        self.resolution_box.blockSignals(False)
        self.resolution_changed()

    def resolution_changed(self):
        previous = self.fps_box.currentData()
        self.fps_box.blockSignals(True)
        self.fps_box.clear()
        mode = self.resolution_box.currentData()
        if mode:
            for fps in mode.rates:
                self.fps_box.addItem(f"{rate_text(fps)} fps", fps)
            try:
                wanted = previous or Fraction(self.settings.value("fps", "30"))
            except (ValueError, ZeroDivisionError):
                wanted = Fraction(30)
            # Fraction is a Python object, so compare values rather than Qt's
            # QVariant identity matching used by findData().
            index = next((i for i, fps in enumerate(mode.rates) if fps == wanted), -1)
            if index < 0:
                index = next((i for i, fps in enumerate(mode.rates) if fps == 30), -1)
            self.fps_box.setCurrentIndex(index if index >= 0 else 0)
            self.mode_hint.setText("Modes reported by this camera." if mode.rates_reported else
                                   "This camera doesn’t report frame rates. These common rates will be tried when you start.")
        else:
            self.mode_hint.clear()
        self.fps_box.blockSignals(False)
        self.update_summary()

    def update_summary(self):
        mode, fps = self.resolution_box.currentData(), self.fps_box.currentData()
        if mode and fps:
            self.summary.setText(f"{mode.width} × {mode.height} at {rate_text(fps)} fps")

    def start_stream(self):
        index = self.camera_box.currentIndex()
        mode, fps = self.resolution_box.currentData(), self.fps_box.currentData()
        if not 0 <= index < len(self.cameras) or not mode or not fps:
            return
        camera = self.cameras[index]
        receiver = self.receiver_edit.text().strip()
        self.error_label.hide()
        self.log_box.clear()
        self.settings.setValue("camera", camera.name)
        self.settings.setValue("resolution", f"{mode.width}x{mode.height}")
        self.settings.setValue("fps", str(fps))
        self.settings.setValue("receiver", receiver)
        transport = self.transport_box.currentData()
        udp_host = self.udp_host_edit.text().strip()
        udp_port = self.udp_port_box.value()
        self.settings.setValue("transport", transport)
        self.settings.setValue("udp_host", udp_host)
        self.settings.setValue("udp_port", udp_port)
        self.append_log(f"{camera.description}: {mode.width}x{mode.height}, {rate_text(fps)} fps → {receiver}")
        self.controller.start(StreamConfig(camera.serial, mode.width, mode.height, fps, receiver, transport, udp_host, udp_port))

    def state_changed(self, state):
        self.preview.set_state(state)
        self.status.setText(state)
        self.status.setProperty("live", state == "Streaming")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        idle = state == "Stopped"
        for widget in (self.camera_box, self.resolution_box, self.fps_box, self.receiver_edit, self.refresh_button,
                       self.transport_box, self.udp_host_edit, self.udp_port_box):
            widget.setEnabled(idle)
        self.start_button.setEnabled(idle and bool(self.cameras))
        self.stop_button.setEnabled(state not in ("Stopped", "Stopping"))
        self.append_log(state)
        if idle and self.closing:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        self.closing = True
        if self.discovery.state() != QProcess.ProcessState.NotRunning:
            self.discovery.kill()
            self.discovery.waitForFinished(1000)
        if self.controller.state != "Stopped":
            event.ignore()
            if self.controller.state != "Stopping":
                self.controller.stop()
        else:
            event.accept()


STYLE = """
QWidget { font-size: 14px; color: #e7edf5; }
QMainWindow { background: #111923; }
QLabel#title { font-size: 27px; font-weight: 600; }
QLabel#subtitle, QLabel#hint { color: #9aabbd; }
QLabel#hint { font-size: 12px; }
QFrame#card { background: #1b2633; border: 1px solid #304154; border-radius: 10px; }
QComboBox, QLineEdit, QSpinBox { background: #111c29; border: 1px solid #41536a; border-radius: 5px; padding: 8px; }
QDialog { background: #1b2633; }
QComboBox QAbstractItemView { background: #1b2633; selection-background-color: #2d5681; }
QPushButton { background: #293c50; border: 1px solid #40566e; border-radius: 6px; padding: 9px 15px; }
QPushButton:hover { background: #344d68; }
QPushButton#primary { background: #397cce; border-color: #5192e1; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #458bdf; }
QPushButton:disabled, QComboBox:disabled, QLineEdit:disabled { color: #758496; background: #1b2633; }
QLabel#status { background: #2a394a; color: #bdd0e5; border-radius: 12px; padding: 6px 12px; font-size: 12px; }
QLabel#status[live=true] { background: #174b3d; color: #a5f0cf; }
QLabel#error { background: #47262c; color: #ffbdc4; padding: 12px; border-radius: 6px; }
QPlainTextEdit { background: #0d141e; border: 1px solid #304154; border-radius: 6px; font-family: monospace; font-size: 12px; }
QToolButton { color: #a9bfd7; border: none; padding: 4px; }
"""


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    app = QApplication(sys.argv)
    app.setApplicationName("CamLink")
    app.setDesktopFileName("laptop-webcam")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = WebcamWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
