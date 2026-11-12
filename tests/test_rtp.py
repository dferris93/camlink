"""Real SRTP media plus negative authentication/replay tests over UDP."""
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from srtp import MEDIA_PYTHON, WORKER, SrtpSession
from stream import StreamConfig, decoder_arguments, sender_arguments


@unittest.skipUnless(shutil.which('gst-launch-1.0') and shutil.which('ffmpeg'), 'GStreamer and FFmpeg required')
class RtpIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='webcam-srtp-test-')
        self.root = Path(self.directory.name)
        self.processes = []
        self.logs = []
        self.config = StreamConfig('1', 640, 480, Fraction(30), receiver='test@receiver.example', transport='rtp', udp_host='127.0.0.1', udp_port=self.port())
        self.session = SrtpSession.create()

    def tearDown(self):
        for process in reversed(self.processes):
            self.stop(process)
        for log in self.logs:
            log.close()
        self.directory.cleanup()

    def stop(self, process):
        if process.stdin and not process.stdin.closed:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            self.fail('Secure worker did not stop when its control channel closed')

    def port(self):
        for _ in range(50):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.bind(('127.0.0.1', 0))
                port = udp.getsockname()[1]
                if port % 2 and port < 65535:
                    return port
        self.fail('No free odd UDP port')

    def spawn(self, role, message):
        log_path = self.root / f'{role}-{len(self.processes)}.log'
        log = log_path.open('wb')
        self.logs.append(log)
        process = subprocess.Popen([MEDIA_PYTHON, '-u', str(WORKER), role], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=log)
        self.processes.append(process)
        process.stdin.write(message)
        process.stdin.flush()
        return process, log_path

    def wait(self, predicate, log, timeout=6):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.025)
        self.fail('Timed out: ' + log.read_text())

    def receiver(self, session=None, frame_limit=None):
        decoder = decoder_arguments(self.config)
        decoder = decoder[:-3] + (['-frames:v', str(frame_limit)] if frame_limit else []) + ['-f', 'null', '-']
        process, log = self.spawn('receive', (session or self.session).message(
            port=self.config.udp_port, ipv6=False, decoder=decoder))
        self.wait(lambda: 'WEBCAM_SRTP_READY' in log.read_text(), log)
        return process, log

    def sender(self, session=None, port=None, buffers=120):
        config = replace(self.config, udp_port=port) if port else self.config
        args = sender_arguments(config)
        pipeline = ['videotestsrc', f'num-buffers={buffers}', 'is-live=true', 'pattern=ball',
                    '!', 'video/x-raw,width=640,height=480,framerate=30/1'] + args[args.index('decodebin') - 1:]
        return self.spawn('send', (session or self.session).message(pipeline=pipeline))

    def capture(self, session):
        port = self.port()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind(('127.0.0.1', port))
            udp.settimeout(5)
            sender, log = self.sender(session, port, buffers=30)
            try:
                packets = [udp.recv(65536) for _ in range(12)]
            except socket.timeout:
                self.fail('No encrypted packets: ' + log.read_text())
            finally:
                self.stop(sender)
        return packets

    def test_h264_srtp_decodes_on_selected_odd_port(self):
        receiver, log = self.receiver(frame_limit=30)
        sender, sender_log = self.sender()
        self.wait(lambda: receiver.poll() is not None, log, timeout=10)
        self.assertEqual(receiver.returncode, 0, log.read_text())
        self.assertIn('frame=30', log.read_text())
        self.assertIn('progress=end', log.read_text())
        import base64
        for path in (log, sender_log):
            self.assertNotIn(base64.b64encode(self.session.key).decode(), path.read_text())
            self.assertNotIn(self.session.key.hex(), path.read_text())
        for process in (receiver, sender):
            arguments = Path(f'/proc/{process.pid}/cmdline')
            if arguments.exists():
                self.assertNotIn(base64.b64encode(self.session.key), arguments.read_bytes())
                self.assertNotIn(self.session.key.hex().encode(), arguments.read_bytes())

    def test_closing_control_channel_cleans_up_without_video(self):
        receiver, log = self.receiver()
        self.assertIsNone(receiver.poll())
        self.stop(receiver)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind(('127.0.0.1', self.config.udp_port))

    def test_wrong_key_tampered_plaintext_and_replayed_packets_are_rejected(self):
        old = SrtpSession(SrtpSession.create().key, self.session.ssrc)
        old_packets = self.capture(old)
        valid_packets = self.capture(self.session)
        receiver, log = self.receiver()
        destination = (self.config.rtp_host, self.config.udp_port)
        def rejected():
            counts = re.findall(r'SRTP rejected packets: (\d+)', log.read_text())
            return int(counts[-1]) if counts else 0
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            # Short/malformed datagrams and an unknown SSRC cannot kill the
            # receiver or create extra per-source decryption contexts.
            for junk in (b'', b'x', b'\0' * 64, b'\xff' * 256):
                udp.sendto(junk, destination)
            foreign = bytearray(valid_packets[0])
            foreign[8] ^= 1
            udp.sendto(foreign, destination)
            time.sleep(.35)
            self.assertIsNone(receiver.poll(), log.read_text())
            before = rejected()
            # A previous session's key must not authenticate in this session.
            udp.sendto(old_packets[0], destination)
            self.wait(lambda: rejected() > before, log)
            before = rejected()
            # Corrupt an encrypted packet without changing its RTP header.
            tampered = valid_packets[1][:-1] + bytes([valid_packets[1][-1] ^ 1])
            udp.sendto(tampered, destination)
            self.wait(lambda: rejected() > before, log)
            before = rejected()
            # A normal RTP header and unauthenticated payload is not accepted.
            udp.sendto(valid_packets[2][:12] + b'\x65' + b'\0' * 100, destination)
            self.wait(lambda: rejected() > before, log)
            before = rejected()
            # Rejected packets must not poison the valid stream's state.
            udp.sendto(valid_packets[0], destination)
            time.sleep(.35)
            self.assertEqual(rejected(), before, log.read_text())
            udp.sendto(valid_packets[0], destination)
            self.wait(lambda: rejected() > before, log)
        self.assertIsNone(receiver.poll(), log.read_text())
        self.assertNotIn('frame=', log.read_text())


if __name__ == '__main__':
    unittest.main()
