"""remote-webcam-srtp: GStreamer worker; configuration and key arrive on stdin.

Runs with the system Python/GStreamer bindings, locally or through SSH. No
credentials appear in arguments, environment variables, SDP files or logs.
"""
import base64
import json
import os
import resource
import signal
import subprocess
import sys
import time


def configuration():
    # Do not buffer beyond the first line: later bytes are control heartbeats.
    data = bytearray()
    while len(data) < 65536:
        byte = os.read(0, 1)
        if byte == b"\n":
            return json.loads(data)
        if not byte:
            raise ValueError("Missing secure stream configuration")
        data.extend(byte)
    raise ValueError("Secure stream configuration is too large")


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Gst debug/caps dumps can contain key buffers. Disable them in this worker.
    for name in ("GST_DEBUG", "GST_DEBUG_FILE", "GST_DEBUG_DUMP_DOT_DIR"):
        os.environ.pop(name, None)
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
    Gst.init(None)
    Gst.debug_set_active(False)
    settings = configuration()
    key = base64.b64decode(settings.pop("key"), validate=True)
    ssrc = settings["ssrc"]
    if len(key) != 30 or not isinstance(ssrc, int) or not 0 <= ssrc < 2**32:
        raise ValueError("Invalid secure stream configuration")
    role = sys.argv[-1]
    if role not in ("send", "receive"):
        raise ValueError("Invalid secure stream role")
    key_buffer = Gst.Buffer.new_wrapped(key)
    loop = GLib.MainLoop()
    pipeline = None
    decoder = None
    decryptor = None
    last_rejection = 0
    failed = False
    last_heartbeat = time.monotonic()
    control = bytearray()

    def fail(message):
        nonlocal failed
        failed = True
        # Never print caps or Gst debug details, which can include the key.
        message = str(message).replace(key.hex(), "[redacted]").replace(base64.b64encode(key).decode(), "[redacted]")
        print(message, file=sys.stderr, flush=True)
        loop.quit()
        return False

    def stop(*_):
        loop.quit()
        return False

    def control_ready(fd, condition):
        nonlocal last_heartbeat
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            return True
        if not chunk:
            return stop()
        control.extend(chunk)
        if len(control) > 4096:
            return fail("Invalid secure stream control message")
        while b"\n" in control:
            line, _, rest = control.partition(b"\n")
            control[:] = rest
            if line != b"keepalive":
                return fail("Invalid secure stream control message")
            last_heartbeat = time.monotonic()
        return True

    def health():
        nonlocal last_rejection
        if role == "receive" and time.monotonic() - last_heartbeat > 15:
            return stop()
        if decoder is not None and decoder.poll() is not None:
            if decoder.returncode == 0:
                return stop()
            return fail(f"Video decoder exited (code {decoder.returncode})")
        if decryptor is not None:
            rejected = decryptor.get_property("stats").get_value("recv-drop-count")
            if rejected != last_rejection:
                last_rejection = rejected
                print(f"SRTP rejected packets: {rejected}", file=sys.stderr, flush=True)
        return True

    def message_received(bus, message):
        if message.type == Gst.MessageType.ERROR:
            if decoder is not None and decoder.poll() == 0:
                stop()
                return
            error, _ = message.parse_error()
            fail(f"Secure video: {error.message}")
        elif message.type == Gst.MessageType.EOS:
            stop()

    try:
        if role == "send":
            pipeline = Gst.parse_launchv(settings["pipeline"])
            encryptor = pipeline.get_by_name("secure")
            payloader = pipeline.get_by_name("payload")
            if encryptor is None or payloader is None:
                raise ValueError("Secure sender elements are missing")
            payloader.set_property("ssrc", ssrc)
            encryptor.set_property("key", key_buffer)
            encryptor.set_property("rtp-cipher", "aes-128-icm")
            encryptor.set_property("rtp-auth", "hmac-sha1-80")
            encryptor.set_property("rtcp-cipher", "aes-128-icm")
            encryptor.set_property("rtcp-auth", "hmac-sha1-80")
            encryptor.connect("soft-limit", lambda *_: fail("SRTP key limit reached; restart streaming for a new key"))
        else:
            port = settings["port"]
            if not isinstance(port, int) or not 1024 <= port <= 65535:
                raise ValueError("Invalid secure receiver port")
            decoder = subprocess.Popen(settings["decoder"], stdin=subprocess.PIPE)
            address = "::" if settings.get("ipv6") else "0.0.0.0"
            pipeline = Gst.parse_launch(
                f'udpsrc address={address} port={port} buffer-size=2097152 '
                f'caps="application/x-srtp,media=video,encoding-name=H264,clock-rate=90000,payload=96,ssrc=(uint){ssrc}" '
                '! srtpdec name=secure replay-window-size=128 '
                '! rtpjitterbuffer latency=100 drop-on-latency=true '
                '! rtph264depay ! h264parse config-interval=-1 '
                '! video/x-h264,stream-format=byte-stream,alignment=au '
                f'! fdsink fd={decoder.stdin.fileno()} sync=false async=false')
            decryptor = pipeline.get_by_name("secure")

            def request_key(element, incoming_ssrc):
                # Only this stream can create an SRTP context. Authenticate
                # before jitter buffering, depayloading or video decoding.
                if incoming_ssrc != ssrc:
                    return None
                caps = Gst.Caps.from_string(
                    'application/x-srtp,media=(string)video,encoding-name=(string)H264,'
                    'clock-rate=(int)90000,payload=(int)96,'
                    'srtp-cipher=(string)aes-128-icm,srtp-auth=(string)hmac-sha1-80,'
                    'srtcp-cipher=(string)aes-128-icm,srtcp-auth=(string)hmac-sha1-80')
                caps.set_value("srtp-key", key_buffer)
                return caps

            decryptor.connect("request-key", request_key)
            # Hard/soft key exhaustion must never fall back to plain RTP.
            decryptor.connect("soft-limit", lambda *_: (fail("SRTP key limit reached; restart streaming"), None)[1])
            decryptor.connect("hard-limit", lambda *_: (fail("SRTP key exhausted; restart streaming"), None)[1])
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", message_received)
        os.set_blocking(0, False)
        GLib.io_add_watch(0, GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR, control_ready)
        GLib.timeout_add(250, health)
        try:
            gi.require_version("GLibUnix", "2.0")
            from gi.repository import GLibUnix
            add_signal = GLibUnix.signal_add
        except (ImportError, ValueError, AttributeError):
            add_signal = GLib.unix_signal_add
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            add_signal(GLib.PRIORITY_DEFAULT, signum, stop)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            return 1
        if role == "receive":
            # udpsrc binds during the synchronous READY -> PAUSED transition.
            print("WEBCAM_SRTP_READY", file=sys.stderr, flush=True)
        loop.run()
        return int(failed)
    finally:
        # Terminate the reader first to unblock any pending pipe write.
        if decoder is not None:
            if decoder.poll() is None:
                decoder.terminate()
                try:
                    decoder.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    decoder.kill()
                    decoder.wait()
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        if decoder is not None:
            decoder.stdin.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Do not include the exception/configuration: it may contain key data.
        print("Secure video could not start. Check Python GStreamer bindings and SRTP plugins on both computers.", file=sys.stderr, flush=True)
        raise SystemExit(1)
