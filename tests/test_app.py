import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from fractions import Fraction

from PyQt6.QtCore import QProcess, QSettings
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication, QDialog, QLineEdit

from app import WebcamWindow
from cameras import parse_cameras
from stream import StreamConfig, StreamController, sender_arguments
from remote import validate_remote_format

TEST_RECEIVER = "test@receiver.example"


def node(serial, name, device, formats, **props):
    return {"id": serial - 1000, "type": "PipeWire:Interface:Node", "info": {
        "props": {"object.serial": serial, "node.name": name, "media.class": "Video/Source",
                  "device.id": device, **props}, "params": {"EnumFormat": formats}}}


def fmt(w, h, rate=None):
    result = {"mediaSubtype": "raw", "size": {"width": w, "height": h}}
    if rate is not None:
        result["framerate"] = rate
    return result


BUILTIN = node(1042, "builtin", 1, [fmt(640, 480), fmt(1280, 720)],
               **{"device.api": "libcamera", "api.libcamera.location": "front", "node.description": "Built-in Camera"})
USB = node(1100, "usb", 2, [fmt(640, 480, [{"num": 15, "denom": 1}, {"num": 30, "denom": 1}]),
                           fmt(1920, 1080, {"num": 60, "denom": 1})])
INVENTORY = [BUILTIN, USB, {"id": 2, "type": "PipeWire:Interface:Device", "info": {"props": {
    "device.bus": "usb", "device.api": "v4l2", "device.description": "External USB Camera"}}}]


class CameraTests(unittest.TestCase):
    def test_remote_format_rejects_silent_clamping_and_bad_layout(self):
        config = StreamConfig("1042", 1920, 1080, Fraction(30), TEST_RECEIVER)
        good = "Width/Height : 1920/1080\nPixel Format : 'YU12'\nBytes per Line : 1920\nSize Image : 3110400\n"
        validate_remote_format(config, good)
        for output in ("", good.replace("1920/1080", "1280/800"), good.replace("YU12", "YUYV"),
                       good.replace("3110400", "1536000"), good.replace("Line : 1920", "Line : 2048")):
            with self.assertRaises(ValueError):
                validate_remote_format(config, output)

    def test_parent_properties_external_preference_and_fractional_rate(self):
        camera = parse_cameras(INVENTORY)[0]
        self.assertEqual((camera.name, camera.serial, camera.kind), ("usb", "1100", "External"))
        self.assertEqual(camera.modes[1].rates, (Fraction(60),))
        fractional = node(1200, "fractional", 3, [fmt(1280, 720, {"num": 30000, "denom": 1001})],
                          **{"device.api": "v4l2"})
        self.assertEqual(parse_cameras([fractional])[0].modes[0].rates, (Fraction(30000, 1001),))

    def test_unknown_rates_are_labelled_and_virtual_nodes_excluded(self):
        virtual = node(1200, "loopback", 3, [], **{"device.api": "v4l2", "api.v4l2.cap.driver": "v4l2 loopback"})
        cameras = parse_cameras([BUILTIN, virtual])
        self.assertEqual(len(cameras), 1)
        self.assertFalse(cameras[0].modes[0].rates_reported)
        self.assertEqual(cameras[0].modes[0].rates, tuple(map(Fraction, (15, 24, 25, 30, 60))))

    def test_ranges_and_empty_inventory(self):
        mode = fmt(640, 480, {"default": {"num": 30, "denom": 1}, "min": {"num": 15, "denom": 1}, "max": {"num": 60, "denom": 1}})
        mode["size"] = {"default": {"width": 640, "height": 480}, "min": {"width": 640, "height": 480}, "max": {"width": 1920, "height": 1080}}
        camera = parse_cameras([node(1042, "ranges", 1, [mode], **{"device.api": "v4l2"})])[0]
        self.assertTrue(any((m.width, m.height) == (1280, 720) and Fraction(60) in m.rates for m in camera.modes))
        self.assertEqual(parse_cameras([]), [])

    def test_mode_controls_capture_and_pacing(self):
        args = sender_arguments(StreamConfig("1042", 640, 480, Fraction(15), TEST_RECEIVER))
        self.assertIn("video/x-raw,format=I420,width=640,height=480,framerate=15/1", args)
        self.assertIn("datarate=6912000", args)
        self.assertIn("target-object=1042", args)
        for host in ("-oProxyCommand=bad", "host; touch /tmp/x", "user@host\ncommand"):
            with self.assertRaises(ValueError):
                StreamConfig("1042", 640, 480, Fraction(30), host).validate()


class ProcessAndWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.config_directory = tempfile.TemporaryDirectory()
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, cls.config_directory.name)

    @classmethod
    def tearDownClass(cls):
        cls.config_directory.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.original_path = os.environ["PATH"]
        os.environ["PATH"] = str(self.root) + os.pathsep + self.original_path
        QSettings("LocalTools", "LaptopWebcam").clear()
        QSettings("LocalTools", "LaptopWebcam").setValue("receiver", TEST_RECEIVER)
        self.stub("pw-dump", "print(" + repr(json.dumps(INVENTORY)) + ")")
        self.stub("ssh", '''import sys, time, re
from pathlib import Path
root = Path(__file__).parent
def print_format(width, height):
    print(f"Width/Height : {width}/{height}\\nPixel Format : 'YU12'\\nBytes per Line : {width}\\nSize Image : {width*height*3//2}")
if 'WEBCAM_SSH_READY' in sys.argv[-1]:
    if (root/'authenticate').exists():
        import os, subprocess
        for prompt, expected in [('Password:', 'test-password'), ('Verification code:', '123456')]:
            response = subprocess.run([os.environ['SSH_ASKPASS'], prompt], capture_output=True, text=True)
            if response.returncode or response.stdout.strip() != expected:
                sys.exit(255)
    print('WEBCAM_SSH_READY 192.0.2.1 12345 192.0.2.2 22', flush=True)
    sys.stdin.read()
elif sys.argv[-1] == "bash -s":
    script = sys.stdin.read()
    width, height = map(int, re.search(r"width=(\\d+),height=(\\d+)", script).groups())
    if (root/'locked-mode').exists():
        width, height = map(int, (root/'locked-mode').read_text().split())
    if '--set-fmt-video-out' in script:
        if (root/'clamp-on-configure').exists():
            width, height = 1280, 800
        (root/'mode').write_text(f'{width} {height}')
    print_format(width, height)
elif '--get-fmt-video' in sys.argv[-1]:
    width, height = map(int, (root/'mode').read_text().split())
    if (root/'clamp-on-verify').exists():
        width, height = 1280, 800
    print_format(width, height)
elif 'laptop-webcam-srtp' in sys.argv[-1]:
    import json, base64
    settings = json.loads(sys.stdin.readline())
    assert len(base64.b64decode(settings['key'])) == 30
    assert settings['port'] == 11111
    print('WEBCAM_SRTP_READY', file=sys.stderr, flush=True)
    for frame, message in enumerate(sys.stdin, 1):
        if not (root/'stall').exists():
            print(f'frame={frame}', file=sys.stderr, flush=True)
else:
    frame = 0
    while sys.stdin.buffer.read(1024):
        frame += 1
        if not (root/'stall').exists():
            print(f"frame={frame}", file=sys.stderr, flush=True)
''')
        self.stub("gst-launch-1.0", '''import sys, time
while True:
    sys.stdout.buffer.write(b"x" * 1024)
    sys.stdout.buffer.flush()
    time.sleep(.02)
''')
        self.stub("media-python", '''import sys, json, base64, time
settings = json.loads(sys.stdin.readline())
assert len(base64.b64decode(settings['key'])) == 30
assert 'srtpenc' in settings['pipeline']
while True:
    time.sleep(.05)
''')
        self.media_patch = patch('stream.MEDIA_PYTHON', str(self.root / 'media-python'))
        self.media_patch.start()
        self.controller = StreamController()
        self.errors = []
        self.controller.failed.connect(self.errors.append)

    def tearDown(self):
        self.controller.stop()
        self.wait(lambda: self.controller.state == "Stopped")
        self.media_patch.stop()
        os.environ["PATH"] = self.original_path
        self.directory.cleanup()

    def stub(self, name, body):
        path = self.root / name
        path.write_text(f"#!{sys.executable}\n" + body + "\n")
        path.chmod(0o755)

    def wait(self, predicate, timeout=5):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.01)
        self.app.processEvents()
        self.assertTrue(predicate(), "Timed out waiting for Qt process state")

    def test_start_stop_and_restart_clean_up_both_processes(self):
        for _ in range(2):
            self.controller.start(StreamConfig("1042", 640, 480, Fraction(15), TEST_RECEIVER))
            self.wait(lambda: self.controller.state == "Streaming")
            preview_directory = Path(self.controller.preview.directory.name)
            self.controller.stop()
            self.wait(lambda: self.controller.state == "Stopped")
            self.assertTrue(all(p.state() == QProcess.ProcessState.NotRunning for p in self.controller.processes))
            self.assertFalse(preview_directory.exists())
            self.assertIsNone(self.controller.preview.fd)
        self.assertEqual(self.errors, [])

    def test_setup_failure_never_starts_camera(self):
        self.stub("ssh", "import sys; print('Permission denied', file=sys.stderr); sys.exit(255)")
        self.controller.start(StreamConfig("1042", 640, 480, Fraction(30), TEST_RECEIVER))
        self.wait(lambda: bool(self.errors) and self.controller.state == "Stopped")
        self.assertEqual(len(self.controller.processes), 1)

    def test_first_launch_requires_a_receiver_before_connecting(self):
        QSettings("LocalTools", "LaptopWebcam").remove("receiver")
        window = WebcamWindow()
        self.wait(lambda: window.camera_box.count() == 2)
        self.assertEqual(window.receiver_edit.text(), "")
        window.start_button.click()
        self.assertEqual(window.controller.state, "Stopped")
        self.assertEqual(window.controller.processes, [])
        self.assertIn("Enter the receiver", window.error_label.text())
        window.close()

    def test_password_then_totp_share_one_connection_and_do_not_leak(self):
        (self.root / 'authenticate').touch()
        prompts, logs = [], []
        self.controller.log.connect(logs.append)
        def answer(prompt, kind):
            prompts.append(prompt)
            self.assertFalse(self.controller.watchdog.isActive())
            self.controller.auth.respond('test-password' if len(prompts) == 1 else '123456')
        self.controller.authentication_requested.connect(answer)
        self.controller.start(StreamConfig('1042', 640, 480, Fraction(30), TEST_RECEIVER))
        self.wait(lambda: self.controller.state == 'Streaming')
        auth_directory = Path(self.controller.auth.directory.name)
        self.assertEqual(auth_directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(prompts, ['Password:', 'Verification code:'])
        for secret in ('test-password', '123456'):
            self.assertNotIn(secret, '\n'.join(logs))
            self.assertTrue(all(secret not in ' '.join(p.arguments()) for p in self.controller.processes))
            self.assertNotIn(secret, (auth_directory / 'askpass').read_text())
        self.controller.stop()
        self.wait(lambda: self.controller.state == 'Stopped')
        self.assertFalse(auth_directory.exists())
        self.assertEqual(self.errors, [])

    def test_cancel_during_totp_stops_without_starting_capture(self):
        (self.root / 'authenticate').touch()
        prompts = []
        def answer(prompt, kind):
            prompts.append(prompt)
            self.controller.auth.respond('test-password' if len(prompts) == 1 else None)
        self.controller.authentication_requested.connect(answer)
        self.controller.start(StreamConfig('1042', 640, 480, Fraction(30), TEST_RECEIVER))
        self.wait(lambda: self.controller.state == 'Stopped')
        self.assertEqual(len(prompts), 2)
        self.assertEqual(len(self.controller.processes), 1)
        self.assertEqual(self.errors, [])
        self.assertIsNone(self.controller.auth.directory)

    def test_rtp_lifecycle_uses_direct_destination_and_control_connection(self):
        config = StreamConfig('1042', 1280, 720, Fraction(30), receiver=TEST_RECEIVER, transport='rtp', udp_port=11111)
        self.controller.start(config)
        self.wait(lambda: self.controller.state == 'Streaming')
        self.assertEqual(self.controller.config.rtp_host, '192.0.2.2')
        sender = next(p for p in self.controller.processes if p.objectName() == 'Camera')
        self.assertIn('rtph264pay', self.controller.sender_pipeline)
        self.assertIn('srtpenc', self.controller.sender_pipeline)
        self.assertIn('host=192.0.2.2', self.controller.sender_pipeline)
        self.assertIn('port=11111', self.controller.sender_pipeline)
        self.assertNotIn('mpegtsmux', self.controller.sender_pipeline)
        first_key = self.controller.srtp_session.key
        first_ssrc = self.controller.srtp_session.ssrc
        import base64
        for process in self.controller.processes:
            self.assertNotIn(base64.b64encode(first_key).decode(), ' '.join(process.arguments()))
            self.assertNotIn(first_key.hex(), ' '.join(process.arguments()))
        self.assertTrue(self.controller.heartbeat.isActive())
        self.controller.stop()
        self.wait(lambda: self.controller.state == 'Stopped')
        self.assertFalse(self.controller.heartbeat.isActive())
        self.assertIsNone(self.controller.srtp_session)
        self.controller.start(config)
        self.wait(lambda: self.controller.state == 'Streaming')
        self.assertNotEqual(first_key, self.controller.srtp_session.key)
        self.assertNotEqual(first_ssrc, self.controller.srtp_session.ssrc)
        self.assertEqual(self.errors, [])

    def test_sign_in_dialog_masks_password_and_closes_when_cancelled(self):
        (self.root / 'authenticate').touch()
        window = WebcamWindow()
        window.show()
        self.wait(lambda: window.camera_box.count() == 2)
        window.start_button.click()
        self.wait(lambda: window.auth_dialog is not None)
        dialog = window.auth_dialog
        field = dialog.findChild(QLineEdit)
        self.assertEqual(field.echoMode(), QLineEdit.EchoMode.Password)
        field.setText('test-password')
        dialog.accept()
        self.wait(lambda: window.auth_dialog is not None and window.auth_dialog is not dialog)
        window.auth_dialog.reject()
        self.wait(lambda: window.controller.state == 'Stopped')
        self.assertIsNone(window.auth_dialog)
        self.assertFalse(window.error_label.isVisible())
        window.close()

    def test_rtp_controls_and_settings_survive_reopening(self):
        window = WebcamWindow()
        window.show()
        self.wait(lambda: window.camera_box.count() == 2)
        self.assertFalse(window.udp_port_box.isVisible())
        window.transport_box.setCurrentIndex(1)
        self.assertTrue(window.udp_port_box.isVisible())
        window.udp_port_box.setValue(11111)
        window.udp_host_edit.setText('192.0.2.3')
        window.start_button.click()
        self.wait(lambda: window.controller.state == 'Streaming')
        self.assertFalse(window.transport_box.isEnabled())
        self.assertFalse(window.udp_port_box.isEnabled())
        window.close()
        self.wait(lambda: window.controller.state == 'Stopped')
        reopened = WebcamWindow()
        self.wait(lambda: reopened.camera_box.count() == 2)
        self.assertEqual(reopened.transport_box.currentData(), 'rtp')
        self.assertEqual(reopened.udp_port_box.value(), 11111)
        self.assertEqual(reopened.udp_host_edit.text(), '192.0.2.3')
        reopened.close()

    def test_capture_failure_stops_receiver(self):
        self.stub("gst-launch-1.0", "import sys; print('unsupported mode', file=sys.stderr); sys.exit(1)")
        self.controller.start(StreamConfig("1042", 640, 480, Fraction(30), TEST_RECEIVER))
        self.wait(lambda: bool(self.errors) and self.controller.state == "Stopped")
        self.assertTrue(all(p.state() == QProcess.ProcessState.NotRunning for p in self.controller.processes))

    def test_stalled_stream_watchdog(self):
        (self.root / 'stall').touch()
        self.controller.start(StreamConfig("1042", 640, 480, Fraction(30), TEST_RECEIVER))
        self.wait(lambda: self.controller.state == "Starting camera")
        self.controller.watchdog.start(80)
        self.wait(lambda: bool(self.errors) and self.controller.state == "Stopped")
        self.assertIn("No video", self.errors[0])

    def test_locked_remote_mode_rejected_before_camera_starts(self):
        (self.root / 'locked-mode').write_text('1280 800')
        self.controller.start(StreamConfig("1042", 1920, 1080, Fraction(30), TEST_RECEIVER))
        self.wait(lambda: bool(self.errors) and self.controller.state == "Stopped")
        self.assertIn('1920×1080', self.errors[0])
        self.assertIn('1280×800', self.errors[0])
        self.assertIn('Zoom', self.errors[0])
        self.assertEqual(len(self.controller.processes), 2)
        self.assertIsNone(self.controller.preview.fd)

    def test_mode_changed_between_probe_and_configuration(self):
        (self.root / 'clamp-on-configure').touch()
        self.controller.start(StreamConfig("1042", 1920, 1080, Fraction(30), TEST_RECEIVER))
        self.wait(lambda: bool(self.errors) and self.controller.state == "Stopped")
        self.assertEqual(len(self.controller.processes), 3)
        self.assertIn('1280×800', self.errors[0])

    def test_mode_changed_after_capture_never_reports_streaming(self):
        states = []
        self.controller.state_changed.connect(states.append)
        (self.root / 'clamp-on-verify').touch()
        self.controller.start(StreamConfig("1042", 1920, 1080, Fraction(30), TEST_RECEIVER))
        self.wait(lambda: bool(self.errors) and self.controller.state == "Stopped")
        self.assertIn('Checking video', states)
        self.assertNotIn('Streaming', states)
        self.assertTrue(all(p.state() == QProcess.ProcessState.NotRunning for p in self.controller.processes))
        self.assertIsNone(self.controller.preview.fd)

    def test_remote_lock_message_is_shown_in_window(self):
        (self.root / 'locked-mode').write_text('1280 800')
        window = WebcamWindow()
        window.show()
        self.wait(lambda: window.camera_box.count() == 2)
        window.start_button.click()
        self.wait(lambda: window.error_label.isVisible() and window.controller.state == 'Stopped')
        self.assertIn('Zoom', window.error_label.text())
        self.assertIn('1280×800', window.error_label.text())
        window.close()

    def test_dropdown_modes_and_close_while_streaming(self):
        window = WebcamWindow()
        window.show()
        self.wait(lambda: window.camera_box.count() == 2)
        self.assertEqual(window.camera_box.currentData(), "usb")
        self.assertEqual(window.fps_box.currentData(), Fraction(30))
        window.camera_box.setCurrentIndex(window.camera_box.findData("builtin"))
        fps60 = window.fps_box.findText("60 fps")
        self.assertGreaterEqual(fps60, 0)
        window.fps_box.setCurrentIndex(fps60)
        self.assertEqual(window.fps_box.currentData(), Fraction(60))
        window.camera_box.setCurrentIndex(window.camera_box.findData("usb"))
        self.assertEqual(window.fps_box.currentData(), Fraction(30))
        window.resolution_box.setCurrentIndex(1)
        self.assertEqual(window.fps_box.currentData(), Fraction(60))
        self.assertEqual(window.fps_box.count(), 1)
        window.start_button.click()
        self.wait(lambda: window.controller.state == "Streaming")
        self.assertFalse(window.camera_box.isEnabled())
        window.controller.preview_frame.emit(QImage(64, 48, QImage.Format.Format_RGB32))
        self.assertFalse(window.preview.image.isNull())
        window.close()
        self.wait(lambda: window.controller.state == "Stopped" and not window.isVisible())
        self.assertTrue(all(p.state() == QProcess.ProcessState.NotRunning for p in window.controller.processes))
        self.assertTrue(window.preview.image.isNull())


if __name__ == "__main__":
    unittest.main()
