from dataclasses import replace
from fractions import Fraction
import io
import json
import os
from pathlib import Path
import resource
import shutil
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QProcess
from PyQt6.QtNetwork import QHostAddress
from PyQt6.QtWidgets import QApplication

from obs_receiver import main as obs_main, read_frame
from pull import CHUNK_SIZE, MAX_BACKLOG, Pairing, PullClient, PullServer, validate_mode
from receiver import ReceiverController
from stream import StreamConfig, StreamController, decoder_arguments, sender_arguments

MODE = dict(width=640, height=480, fps_num=30, fps_den=1)


class QtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def wait(self, predicate, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail('Timed out waiting for transport state')


class PairingTests(QtCase):
    def setUp(self):
        self.server = PullServer()
        self.errors = []
        self.server.failed.connect(self.errors.append)
        self.code = self.server.start(MODE, 0, QHostAddress.SpecialAddress.LocalHost)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.stop()
        self.server.stop()
        self.app.processEvents()

    def client(self, code=None):
        client = PullClient()
        errors, configured, video = [], [], []
        client.failed.connect(errors.append)
        client.configured.connect(configured.append)
        client.video.connect(video.append)
        self.clients.append(client)
        client.start('127.0.0.1', code or self.code)
        return client, errors, configured, video

    def test_tls_pairing_video_and_progress(self):
        progress = []
        self.server.progress.connect(progress.append)
        client, errors, configured, video = self.client()
        self.wait(lambda: bool(configured))
        self.assertEqual(configured, [MODE])
        self.assertEqual(self.server.code, '')
        self.assertEqual(self.server.token, '')
        self.assertIsNone(client.offer)
        self.assertFalse(self.server.server.isListening())
        self.assertFalse(self.server.send_video(b'not ready'))
        client.ready()
        self.wait(lambda: self.server.receiving)
        payload = b'\0\xffvideo' * 300
        self.assertTrue(self.server.send_video(payload))
        self.wait(lambda: bool(video))
        self.assertEqual(video, [payload])
        client.progress(15)
        self.wait(lambda: progress == [15])
        self.assertEqual(errors, [])
        self.assertEqual(self.errors, [])

    def test_wrong_token_does_not_consume_pairing_code(self):
        offer = Pairing.parse(self.code)
        bad = replace(offer, token='A' * 43).encode()
        _, errors, configured, _ = self.client(bad)
        self.wait(lambda: bool(errors))
        self.assertEqual(configured, [])
        self.assertEqual(self.server.code, self.code)
        _, errors, configured, _ = self.client()
        self.wait(lambda: bool(configured))
        self.assertEqual(errors, [])

    def test_wrong_certificate_pin_sends_no_authentication(self):
        offer = Pairing.parse(self.code)
        bad = replace(offer, fingerprint='0' * 64).encode()
        client, errors, configured, _ = self.client(bad)
        self.wait(lambda: bool(errors))
        self.assertEqual(configured, [])
        self.assertIsNone(self.server.peer)
        self.assertIsNotNone(client.offer)
        self.assertEqual(self.server.code, self.code)

    def test_pairing_rotates_and_previous_code_cannot_connect(self):
        first = Pairing.parse(self.code)
        self.server.stop()
        second_code = self.server.start(MODE, first.port, QHostAddress.SpecialAddress.LocalHost)
        second = Pairing.parse(second_code)
        self.assertNotEqual(first.token, second.token)
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        _, errors, configured, _ = self.client()
        self.wait(lambda: bool(errors))
        self.assertFalse(configured)

    def test_expiry_closes_listener_and_erases_pairing(self):
        self.server.expiry.start(1)
        self.wait(lambda: bool(self.errors))
        self.assertEqual(self.server.code, '')
        self.assertEqual(self.server.token, '')
        self.assertIsNone(self.server.server)

    def test_disconnect_and_backpressure_are_reported(self):
        client, _, configured, _ = self.client()
        self.wait(lambda: bool(configured))
        client.ready()
        self.wait(lambda: self.server.receiving)
        with patch.object(self.server.peer.socket, 'bytesToWrite', return_value=MAX_BACKLOG):
            self.assertFalse(self.server.send_video(b'x'))
        self.assertIn('cannot keep up', self.errors[-1])
        client.stop()
        self.wait(lambda: any('disconnected' in error for error in self.errors))

    def test_oversized_video_is_rejected(self):
        client, _, configured, _ = self.client()
        self.wait(lambda: bool(configured))
        client.ready()
        self.wait(lambda: self.server.receiving)
        self.assertFalse(self.server.send_video(b'x' * (CHUNK_SIZE + 1)))
        self.assertTrue(self.errors)

    def test_unexpected_control_and_mode_change_stop_the_session(self):
        client, _, configured, _ = self.client()
        self.wait(lambda: bool(configured))
        client.wire.send(dict(type='ready', **dict(MODE, width=1280)))
        self.wait(lambda: bool(self.errors))
        self.assertFalse(self.server.receiving)
        self.assertIn('different video mode', self.errors[0])

    def test_heartbeat_timeout_closes_the_connection(self):
        client, errors, configured, _ = self.client()
        self.wait(lambda: bool(configured))
        client.wire.last_pong = time.monotonic() - 16
        client.wire.heartbeat()
        self.assertTrue(errors)
        self.wait(lambda: bool(self.errors))

    def test_non_ascii_authentication_cannot_crash_the_listener(self):
        client, errors, _, _ = self.client()
        # Replace the authentication handler before the TLS handshake completes.
        client.wire.socket.connected.disconnect(client.connected)
        client.wire.socket.connected.connect(lambda: client.wire.send(dict(type='authenticate', token='☃' * 43)))
        self.wait(lambda: bool(errors))
        self.assertIsNone(self.server.peer)
        self.assertEqual(self.server.code, self.code)

    def test_invalid_modes_and_codes_are_rejected(self):
        for changes in ({'width': True}, {'height': 481}, {'width': 8192, 'height': 8192},
                        {'fps_den': 0}, {'fps_num': 121}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_mode(dict(MODE, **changes))
        for code in ('', 'CL1.0.' + '0' * 64 + '.' + 'a' * 43, self.code + '.extra'):
            with self.assertRaises(ValueError):
                Pairing.parse(code)
        self.assertNotIn(Pairing.parse(self.code).token, repr(Pairing.parse(self.code)))


class FrameTests(unittest.TestCase):
    def test_obs_startup_failure_is_actionable_and_never_starts_decoder(self):
        def missing_camera(**_):
            raise RuntimeError('OBS not installed')
        output = io.StringIO()
        with (patch.dict(sys.modules, numpy=SimpleNamespace(),
                         pyvirtualcam=SimpleNamespace(Camera=missing_camera, PixelFormat=SimpleNamespace(I420='I420'))),
              patch.object(sys, 'argv', ['obs_receiver.py', '640', '480', '30', '1']),
              patch.object(sys, 'stdout', output),
              patch('obs_receiver.signal.signal'),
              patch('obs_receiver.subprocess.Popen') as process):
            self.assertEqual(obs_main(), 1)
            process.assert_not_called()
        message = json.loads(output.getvalue())
        self.assertEqual(message['type'], 'error')
        self.assertIn('OBS Virtual Camera', message['message'])
        self.assertIn('close OBS', message['message'])

    def test_fragmented_raw_frames_and_truncated_input(self):
        class Fragmented(io.BytesIO):
            def readinto(self, target):
                return super().readinto(target[:3])
        stream = Fragmented(bytes(range(24)))
        self.assertEqual(read_frame(stream, 12), bytes(range(12)))
        self.assertEqual(read_frame(stream, 12), bytes(range(12, 24)))
        self.assertIsNone(read_frame(stream, 12))
        with self.assertRaises(ValueError):
            read_frame(Fragmented(b'partial'), 12)


class LocalReceiverTests(QtCase):
    def test_linux_clamping_at_each_stage_stops_without_reporting_video(self):
        stages = ('Camera setup', 'Camera configuration', 'Video verification')
        good = "Width/Height : 640/480\nPixel Format : 'YU12'\nBytes per Line : 640\nSize Image : 460800\n"
        for rejected_stage in stages:
            with self.subTest(stage=rejected_stage):
                receiver = ReceiverController(backend='linux')
                errors, states, attempted = [], [], []
                receiver.failed.connect(errors.append)
                receiver.state_changed.connect(states.append)

                def run_script(name, _):
                    attempted.append(name)
                    output = good.replace('640/480', '320/240') if name == rejected_stage else good
                    process = receiver.process(name)
                    process.start(sys.executable, ['-c', f'print({output!r})'])

                # A controlled decoder reports its first frame, then waits for
                # stdin EOF. The real controller must reject a late format race
                # before reporting those frames to the sender.
                decoder = [sys.executable, '-u', '-c',
                           'import sys; print("frame=1", file=sys.stderr, flush=True); sys.stdin.buffer.read()']
                with (patch.object(receiver.client, 'start', side_effect=lambda *_: receiver.configure(MODE)),
                      patch.object(receiver.client, 'ready') as ready,
                      patch.object(receiver.client, 'progress') as progress,
                      patch.object(receiver, 'run_script', side_effect=run_script),
                      patch('receiver.shutil.which', return_value=sys.executable),
                      patch('receiver.decoder_arguments', return_value=decoder)):
                    try:
                        receiver.start('receiver.example', 'unused-test-code')
                        self.wait(lambda: receiver.state == 'Stopped')
                        self.assertTrue(errors)
                        self.assertIn('fixed at 320×240', errors[0])
                        self.assertEqual(attempted, list(stages[:stages.index(rejected_stage) + 1]))
                        self.assertNotIn('Streaming', states)
                        progress.assert_not_called()
                        self.assertEqual(ready.call_count, int(rejected_stage == 'Video verification'))
                        self.assertTrue(all(p.state() == QProcess.ProcessState.NotRunning for p in receiver.processes))
                    finally:
                        receiver.stop()
                        self.wait(lambda: receiver.state == 'Stopped')


@unittest.skipUnless(shutil.which('gst-launch-1.0') and shutil.which('ffmpeg'), 'GStreamer and FFmpeg required')
class PullMediaTests(QtCase):
    def test_real_tls_video_decodes_and_receiver_stop_releases_sender(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ctl = root / 'v4l2-ctl'
            ctl.write_text(f'#!{sys.executable}\n' +
                           'print("Width/Height : 640/480\\nPixel Format : \'YU12\'\\n'
                           'Bytes per Line : 640\\nSize Image : 460800")\n')
            ctl.chmod(0o755)
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            sender = StreamController()
            receiver = ReceiverController(backend='linux')
            errors, logs = [], []
            sender.failed.connect(errors.append)
            receiver.failed.connect(errors.append)
            sender.log.connect(logs.append)
            receiver.log.connect(logs.append)
            config = StreamConfig('1', 640, 480, Fraction(30), transport='pull', listen_port=port)

            def test_source(config, preview):
                args = sender_arguments(config, preview)
                return ['-q', '-e', 'videotestsrc', 'is-live=true', 'pattern=ball',
                        '!', 'video/x-raw,width=640,height=480,framerate=30/1'] + args[args.index('decodebin') - 1:]

            def null_output(config):
                return decoder_arguments(config)[:-3] + ['-f', 'null', '-']

            with (patch.dict(os.environ, PATH=directory + os.pathsep + os.environ['PATH']),
                  patch('stream.sender_arguments', side_effect=test_source),
                  patch('receiver.decoder_arguments', side_effect=null_output),
                  patch('receiver.REMOTE_SETUP', ''),
                  patch.object(sender.auth, 'start', side_effect=AssertionError('Must not use SSH'))):
                try:
                    sender.start(config)
                    self.assertEqual(sender.state, 'Waiting for receiver')
                    self.assertIsNone(sender.sender)
                    code = sender.pull.code
                    self.assertTrue(code)
                    receiver.start('127.0.0.1', code)
                    self.wait(lambda: (sender.state == 'Streaming' and receiver.state == 'Streaming') or errors, 15)
                    self.assertEqual(errors, [], '\n'.join(logs))
                    self.assertGreater(sender.last_frame, 0)
                    self.assertIsNone(sender.master)
                    self.assertNotIn(code, '\n'.join(logs))
                    receiver.stop()
                    self.wait(lambda: sender.state == receiver.state == 'Stopped')
                    self.assertIsNone(sender.preview.fd)
                    self.assertTrue(all(p.state() == QProcess.ProcessState.NotRunning
                                        for p in sender.processes + receiver.processes))
                finally:
                    receiver.stop()
                    sender.stop()
                    self.wait(lambda: sender.state == receiver.state == 'Stopped')


if __name__ == '__main__':
    unittest.main()
