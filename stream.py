"""Owned camera, SSH control connection and SSH/SRTP video transports."""
from dataclasses import dataclass, replace
from fractions import Fraction
import ipaddress
import math
import re
import shlex
import shutil

from PyQt6.QtCore import QObject, QProcess, QTimer, pyqtSignal
from PyQt6.QtGui import QImage

from preview import PreviewPipe
from auth import AskpassBridge
from remote import configure_script, requested_format, validate_remote_format
from srtp import MEDIA_PYTHON, WORKER, SrtpSession

SSH_OPTIONS = ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=5",
               "-o", "ServerAliveCountMax=2", "-o", "Compression=no"]
AUTH_COMMAND = 'printf "WEBCAM_SSH_READY %s\\n" "$SSH_CONNECTION"; exec cat >/dev/null'
REMOTE_SETUP = r'''set -euo pipefail
export LC_ALL=C
if [[ ! -c /dev/video42 ]]; then
  sudo -n modprobe v4l2loopback devices=1 video_nr=42 card_label="Remote Webcam" exclusive_caps=1
  udevadm settle
fi
if [[ ! -r /sys/class/video4linux/video42/name ]] ||
   [[ "$(cat /sys/class/video4linux/video42/name)" != "Remote Webcam" ]]; then
  echo 'Expected Remote Webcam at /dev/video42; refusing a different device.' >&2
  echo 'If upgrading CamLink, update card_label="Remote Webcam" in the receiver modprobe configuration and reboot. See README: Common receiver setup.' >&2
  exit 1
fi
[[ -w /dev/video42 ]] || { echo 'No write access to /dev/video42.' >&2; exit 1; }
command -v ffmpeg >/dev/null
v4l2-ctl -d /dev/video42 --set-ctrl=timeout=2000
'''


@dataclass(frozen=True)
class StreamConfig:
    camera: str
    width: int
    height: int
    fps: Fraction
    receiver: str = ""
    transport: str = "ssh"
    udp_host: str = ""
    udp_port: int = 5004

    def validate(self):
        if not self.camera.isdecimal():
            raise ValueError("Refresh the camera list and select a camera.")
        if not all(0 < n <= 8192 and n % 2 == 0 for n in (self.width, self.height)):
            raise ValueError("Resolution must have positive, even dimensions up to 8192.")
        if not 0 < self.fps <= 120:
            raise ValueError("Frame rate must be between 0 and 120 fps.")
        if not re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9a-fA-F:]+\])", self.receiver):
            raise ValueError("Enter the receiver as user@hostname or user@IP-address.")
        if self.transport not in ("ssh", "rtp"):
            raise ValueError("Choose SSH or Secure RTP (UDP) transport.")
        if self.udp_host:
            host = self.udp_host.strip("[]")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", self.udp_host):
                    raise ValueError("Enter an RTP destination IP address or hostname, without a port.")
        if not 1024 <= self.udp_port <= 65535:
            raise ValueError("Use an RTP port between 1024 and 65535.")

    @property
    def rtp_host(self):
        return (self.udp_host or self.receiver.rsplit("@", 1)[-1]).strip("[]")


def sender_arguments(config, preview_path=None):
    config.validate()
    fps = f"{config.fps.numerator}/{config.fps.denominator}"
    mode = f"width={config.width},height={config.height},framerate={fps}"
    bitrate = max(800, min(16000, round(2500 * config.width * config.height * float(config.fps) / (1280 * 720 * 30))))
    # Retimestamp AFTER decoding/conversion: PipeWire camera timestamps can stall.
    datarate = round(config.width * config.height * 3 * config.fps / 2)
    capture = ["-q", "-e", "pipewiresrc", f"target-object={config.camera}", "provide-clock=false",
            "!", f"video/x-raw,{mode};image/jpeg,{mode}", "!", "decodebin", "!", "videoconvert",
            "!", "videoscale", "!", f"video/x-raw,format=I420,{mode}",
            "!", "identity", f"datarate={datarate}", "sync=true"]
    encoder = ["!", "x264enc", "tune=zerolatency", "speed-preset=ultrafast", f"bitrate={bitrate}",
            f"key-int-max={math.ceil(config.fps)}", "!", "h264parse", "config-interval=-1"]
    if config.transport == "rtp":
        encoder += ["!", "rtph264pay", "name=payload", "pt=96", "config-interval=-1", "aggregate-mode=none", "mtu=1200",
                    "!", "srtpenc", "name=secure",
                    "!", "udpsink", f"host={config.rtp_host}", f"port={config.udp_port}", "sync=false", "async=false"]
    else:
        encoder += ["!", "mpegtsmux", "alignment=7", "!", "fdsink", "fd=1", "sync=false"]
    if preview_path is None:
        return capture + encoder
    scale = min(1, 640 / config.width, 480 / config.height)
    width = max(2, int(config.width * scale) // 2 * 2)
    height = max(2, int(config.height * scale) // 2 * 2)
    rate = min(config.fps, Fraction(15))
    preview_mode = f"video/x-raw,width={width},height={height},framerate={rate.numerator}/{rate.denominator}"
    queue = ["!", "queue", "max-size-buffers=2", "max-size-bytes=0", "max-size-time=0"]
    # Both consumers share one capture. The leaky preview queue cannot hold up
    # remote video if the GUI is busy; JPEG bytes travel through a private FIFO.
    return (capture + ["!", "tee", "name=video", "video."] + queue + encoder
            + ["video."] + queue + ["leaky=downstream", "!", "videorate", "drop-only=true",
               "!", "videoscale", "!", preview_mode, "!", "jpegenc", "quality=75",
               "!", "filesink", f"location={preview_path}", "sync=false", "async=false", "buffer-mode=unbuffered"])


def decoder_arguments(config):
    config.validate()
    fps = f"{config.fps.numerator}/{config.fps.denominator}"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-progress", "pipe:2",
               "-stats_period", "1", "-fflags", "nobuffer", "-flags", "low_delay", "-threads", "1",
               "-analyzeduration", "100000", "-probesize", "65536"]
    if config.transport == "rtp":
        # The authenticated H.264 elementary stream needs no second container;
        # regenerate timestamps at the selected rate after RTP reordering.
        command += ["-r", fps, "-f", "h264", "-i", "pipe:0"]
    else:
        command += ["-f", "mpegts", "-i", "pipe:0"]
    command += ["-map", "0:v:0", "-an", "-r", fps, "-c:v", "rawvideo", "-pix_fmt", "yuv420p",
                "-threads", "1", "-f", "v4l2", "/dev/video42"]
    return command


def receiver_command(config):
    config.validate()
    if config.transport == "ssh":
        return "exec " + shlex.join(decoder_arguments(config))
    # Only source code is in the command. The one-session key/configuration
    # travels over this SSH channel's stdin, never through argv or a disk file.
    return "exec " + shlex.join([MEDIA_PYTHON, "-u", "-c", WORKER.read_text(), "receive"])


def receiver_arguments(config):
    return SSH_OPTIONS + [config.receiver, receiver_command(config)]


class StreamController(QObject):
    state_changed = pyqtSignal(str)
    log = pyqtSignal(str)
    failed = pyqtSignal(str)
    preview_frame = pyqtSignal(QImage)
    authentication_requested = pyqtSignal(str, str)
    authentication_dismissed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = "Stopped"
        self.processes = []
        self.config = None
        self.last_frame = 0
        self.error_reported = False
        self.progress_buffer = ""
        self.outputs = {}
        self.master = None
        self.receiver = None
        self.sender = None
        self.sender_pipeline = None
        self.srtp_session = None
        self.auth_buffer = ""
        self.auth = AskpassBridge(self)
        self.auth.requested.connect(self.authentication_prompt)
        self.auth.dismissed.connect(self.authentication_dismissed)
        self.auth.answered.connect(self.authentication_answered)
        self.heartbeat = QTimer(self)
        self.heartbeat.setInterval(3000)
        self.heartbeat.timeout.connect(self.send_heartbeat)
        self.preview = PreviewPipe(self)
        self.preview.frame_ready.connect(self.deliver_preview)
        self.preview.failed.connect(self.fail)
        self.watchdog = QTimer(self)
        self.watchdog.setSingleShot(True)
        self.watchdog.timeout.connect(self.timed_out)
        self.kill_timer = QTimer(self)
        self.kill_timer.setSingleShot(True)
        self.kill_timer.timeout.connect(self.force_stop)

    def set_state(self, state):
        self.state = state
        self.state_changed.emit(state)

    def process(self, name):
        process = QProcess(self)
        process.setObjectName(name)
        process.readyReadStandardError.connect(lambda: self.read_error(process))
        if name == "SSH connection":
            process.readyReadStandardOutput.connect(lambda: self.read_authentication(process))
        if name in ("Receiver setup", "Receiver configuration", "Video verification"):
            process.readyReadStandardOutput.connect(lambda: self.read_output(process))
        process.errorOccurred.connect(lambda error: self.process_error(process, error))
        process.finished.connect(lambda code, status: self.process_finished(process, code))
        self.processes.append(process)
        return process

    def read_output(self, process):
        text = bytes(process.readAllStandardOutput()).decode(errors="replace")
        if text:
            name = process.objectName()
            self.outputs[name] = (self.outputs.get(name, "") + text)[-65536:]
            self.log.emit(f"{name}: {text.rstrip()}")

    def run_remote_script(self, name, script):
        process = self.process(name)
        process.started.connect(lambda: (process.write(script.encode()), process.closeWriteChannel()))
        process.start("ssh", self.ssh_arguments("bash -s"))

    def ssh_arguments(self, command):
        return SSH_OPTIONS + ["-S", self.auth.control_path, "-o", "ControlMaster=no",
                              self.config.receiver, command]

    def authentication_prompt(self, prompt, kind):
        if self.state in ("Stopped", "Stopping"):
            self.auth.respond(None)
            return
        self.watchdog.stop()
        self.set_state("Sign in")
        self.authentication_requested.emit(prompt, kind)

    def authentication_answered(self, accepted):
        if self.state in ("Stopped", "Stopping"):
            return
        if not accepted:
            self.stop()
        else:
            self.set_state("Connecting")
            self.watchdog.start(60000)

    def read_authentication(self, process):
        self.auth_buffer = (self.auth_buffer + bytes(process.readAllStandardOutput()).decode(errors="replace"))[-65536:]
        while "\n" in self.auth_buffer:
            line, self.auth_buffer = self.auth_buffer.split("\n", 1)
            if line.startswith("WEBCAM_SSH_READY ") and self.state == "Connecting":
                fields = line.split()
                if self.config.transport == "rtp" and not self.config.udp_host and len(fields) == 5:
                    self.config = replace(self.config, udp_host=fields[3])
                self.log.emit("SSH authenticated; sharing this connection for receiver control.")
                if self.config.transport == "rtp":
                    self.log.emit(f"Video destination: SRTP/UDP {self.config.rtp_host}:{self.config.udp_port}; fresh session key shared over SSH.")
                # Probe before starting capture: consumers can lock old buffers.
                setup = REMOTE_SETUP + f"v4l2-ctl -d /dev/video42 --try-fmt-video-out={requested_format(self.config)}\n"
                self.run_remote_script("Receiver setup", setup)
                self.watchdog.start(20000)

    def timed_out(self):
        if self.state in ("Connecting", "Sign in"):
            self.fail("SSH connection timed out. Check the remote computer and Connection details.")
        elif self.config and self.config.transport == "rtp":
            self.fail(f"No authenticated video arrived over SRTP. Check the destination and UDP port {self.config.udp_port}, "
                      "the remote firewall, and the selected camera mode.")
        else:
            self.fail("No video arrived. Check the camera, selected mode, and SSH connection.")

    def start(self, config):
        if self.state != "Stopped":
            return
        try:
            config.validate()
            for program in ("ssh", "gst-launch-1.0"):
                if not shutil.which(program):
                    raise ValueError(f"Required program not found: {program}")
        except ValueError as error:
            self.failed.emit(str(error))
            return
        for process in self.processes:
            process.deleteLater()
        self.processes = []
        self.config = config
        self.last_frame = 0
        self.error_reported = False
        self.progress_buffer = ""
        self.outputs = {}
        self.auth_buffer = ""
        self.receiver = None
        self.sender = None
        self.sender_pipeline = None
        self.srtp_session = SrtpSession.create() if config.transport == "rtp" else None
        self.set_state("Connecting")
        try:
            environment = self.auth.start()
        except OSError as error:
            self.fail(f"Cannot prepare SSH sign-in: {error}")
            return
        self.master = self.process("SSH connection")
        self.master.setProcessEnvironment(environment)
        # One authenticated master, kept in the foreground and owned by this
        # controller. Respect the user's SSH config, keys and known_hosts.
        options = ["BatchMode=no" if option == "BatchMode=yes" else option for option in SSH_OPTIONS]
        self.master.start("ssh", options + ["-M", "-S", self.auth.control_path, "-o", "ControlPersist=no",
                                          self.config.receiver, AUTH_COMMAND])
        self.watchdog.start(60000)

    def start_video(self):
        self.set_state("Starting camera")
        self.watchdog.start(20000)
        try:
            preview_path = self.preview.start()
        except OSError as error:
            self.fail(f"Cannot open local preview: {error}")
            return
        receiver = self.process("Receiver")
        self.receiver = receiver
        sender = self.process("Camera")
        self.sender = sender
        self.sender_pipeline = sender_arguments(self.config, preview_path)
        if self.config.transport == "ssh":
            sender.setStandardOutputProcess(receiver)
        else:
            sender.setStandardOutputFile(QProcess.nullDevice())
            receiver.started.connect(self.initialize_receiver)
            sender.started.connect(self.initialize_sender)
        receiver.start("ssh", self.ssh_arguments(receiver_command(self.config)))
        if self.config.transport == "ssh":
            sender.start("gst-launch-1.0", self.sender_pipeline)

    def initialize_receiver(self):
        if self.state != "Starting camera" or self.srtp_session is None:
            return
        self.receiver.write(self.srtp_session.message(port=self.config.udp_port, ipv6=":" in self.config.rtp_host,
                                                      decoder=decoder_arguments(self.config)))
        self.heartbeat.start()

    def initialize_sender(self):
        if self.state == "Starting camera" and self.srtp_session is not None:
            self.sender.write(self.srtp_session.message(pipeline=self.sender_pipeline[2:]))

    def send_heartbeat(self):
        if self.receiver and self.receiver.state() == QProcess.ProcessState.Running:
            self.receiver.write(b"keepalive\n")

    def deliver_preview(self, image):
        if self.state in ("Starting camera", "Checking video", "Streaming"):
            self.preview_frame.emit(image)

    def verify_video(self):
        self.set_state("Checking video")
        self.watchdog.start(10000)
        process = self.process("Video verification")
        process.started.connect(process.closeWriteChannel)
        process.start("ssh", self.ssh_arguments("LC_ALL=C timeout 5s v4l2-ctl -d /dev/video42 --get-fmt-video"))

    def read_error(self, process):
        text = bytes(process.readAllStandardError()).decode(errors="replace")
        if self.srtp_session is not None:
            text = self.srtp_session.redact(text)
        if process.objectName() != "Receiver":
            if text.strip():
                self.log.emit(f"{process.objectName()}: {text.rstrip()}")
            return
        self.progress_buffer += text
        while "\n" in self.progress_buffer:
            line, self.progress_buffer = self.progress_buffer.split("\n", 1)
            if line == "WEBCAM_SRTP_READY":
                if self.state == "Starting camera" and self.config.transport == "rtp" and self.sender.state() == QProcess.ProcessState.NotRunning:
                    self.log.emit("Secure UDP receiver ready; unauthenticated and replayed packets are rejected.")
                    self.sender.start(MEDIA_PYTHON, ["-u", str(WORKER), "send"])
                    self.watchdog.start(20000)
            elif line.startswith("frame="):
                try:
                    frame = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if frame > self.last_frame and self.state in ("Starting camera", "Checking video", "Streaming"):
                    self.last_frame = frame
                    if self.state == "Starting camera":
                        self.verify_video()
                    elif self.state == "Streaming":
                        self.watchdog.start(8000)
            elif line and not re.match(r"^(fps|stream_\d+_\d+_q|bitrate|total_size|out_time(?:_us|_ms)?|dup_frames|drop_frames|speed|progress)=", line):
                self.log.emit(f"Receiver: {line}")

    def process_error(self, process, error):
        if self.state not in ("Stopped", "Stopping"):
            self.fail(f"{process.objectName()}: {process.errorString()}")
        QTimer.singleShot(0, self.finish_stop)

    def process_finished(self, process, code):
        self.read_error(process)
        name = process.objectName()
        if name in ("Receiver setup", "Receiver configuration", "Video verification"):
            self.read_output(process)
        if self.state == "Stopping":
            self.finish_stop()
        elif self.state != "Stopped":
            if name in ("Receiver setup", "Receiver configuration", "Video verification") and code == 0:
                try:
                    validate_remote_format(self.config, self.outputs.get(name, ""))
                except ValueError as error:
                    self.fail(str(error))
                    return
                if name == "Receiver setup":
                    # Set and read back the format as another app may open the
                    # camera between TRY_FMT and this operation.
                    self.run_remote_script("Receiver configuration", configure_script(self.config))
                    self.watchdog.start(20000)
                elif name == "Receiver configuration":
                    self.start_video()
                else:
                    self.set_state("Streaming")
                    self.watchdog.start(8000)
            else:
                if name in ("SSH connection", "Receiver setup"):
                    hint = " Check SSH access and the details below."
                elif name in ("Receiver configuration", "Video verification"):
                    hint = " Release Remote Webcam in Zoom or other video apps, then retry. See details below."
                elif name == "Receiver" and self.config.transport == "rtp":
                    hint = f" Check UDP port {self.config.udp_port}, the firewall, and Connection details."
                else:
                    hint = " Try another camera mode; see details below."
                self.fail(f"{name} exited (code {code})." + hint)

    def fail(self, message):
        if self.state in ("Stopped", "Stopping"):
            return
        if not self.error_reported:
            self.error_reported = True
            self.failed.emit(message)
        self.stop()

    def stop(self):
        if self.state in ("Stopped", "Stopping"):
            return
        self.watchdog.stop()
        self.heartbeat.stop()
        self.set_state("Stopping")
        self.auth.respond(None)
        self.authentication_dismissed.emit()
        for process in self.processes:
            if process.state() != QProcess.ProcessState.NotRunning:
                if process is self.master:
                    continue
                if process is self.receiver and self.config.transport == "rtp":
                    process.closeWriteChannel()
                else:
                    process.terminate()
        self.kill_timer.start(3000)
        self.finish_stop()

    def force_stop(self):
        for process in self.processes:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
        self.finish_stop()

    def finish_stop(self):
        if self.state == "Stopping" and self.master and all(
                p is self.master or p.state() == QProcess.ProcessState.NotRunning for p in self.processes):
            if self.master.state() != QProcess.ProcessState.NotRunning:
                self.master.terminate()
        if self.state == "Stopping" and all(p.state() == QProcess.ProcessState.NotRunning for p in self.processes):
            self.kill_timer.stop()
            self.preview.stop()
            self.auth.stop()
            self.master = None
            self.receiver = None
            self.sender = None
            self.sender_pipeline = None
            self.srtp_session = None
            self.set_state("Stopped")
