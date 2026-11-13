"""Decode stdin MPEG-TS into OBS Virtual Camera. No listener or credentials."""
from fractions import Fraction
import json
import resource
import signal
import subprocess
import sys
import time


def read_frame(source, size):
    frame = bytearray(size)
    view = memoryview(frame)
    offset = 0
    while offset < size:
        count = source.readinto(view[offset:])
        if not count:
            if offset:
                raise ValueError('Decoder ended in the middle of a frame.')
            return None
        offset += count
    return frame


def event(kind, **values):
    print(json.dumps(dict(type=kind, **values)), flush=True)


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    width, height, numerator, denominator = map(int, sys.argv[1:])
    if (not 0 < width <= 8192 or not 0 < height <= 8192 or width % 2 or height % 2
            or width * height > 3840 * 2160 or not 0 < denominator <= 1000000
            or not 0 < numerator <= 120 * denominator):
        raise ValueError('Invalid video mode.')
    import numpy as np
    import pyvirtualcam

    def stop(*_):
        raise SystemExit(0)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)
    fps = Fraction(numerator, denominator)
    try:
        camera = pyvirtualcam.Camera(width=width, height=height, fps=float(fps),
                                    fmt=pyvirtualcam.PixelFormat.I420, backend='obs')
    except Exception:
        event('error', message='Cannot open OBS Virtual Camera. Install OBS 30+, approve its camera extension, '
              'start and stop its virtual camera once, then close OBS and retry.')
        return 1
    decoder = None
    try:
        with camera:
            decoder = subprocess.Popen([
                'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-fflags', 'nobuffer', '-flags', 'low_delay', '-threads', '1',
                '-analyzeduration', '100000', '-probesize', '65536',
                '-f', 'mpegts', '-i', 'pipe:0', '-map', '0:v:0', '-an',
                '-vf', f'scale={width}:{height}',
                '-r', f'{numerator}/{denominator}', '-c:v', 'rawvideo', '-pix_fmt', 'yuv420p',
                '-threads', '1', '-f', 'rawvideo', 'pipe:1'],
                stdin=sys.stdin.buffer, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            event('ready', width=camera.width, height=camera.height, fps_num=numerator, fps_den=denominator)
            count = 0
            last_report = 0
            while True:
                frame = read_frame(decoder.stdout, width * height * 3 // 2)
                if frame is None:
                    break
                camera.send(np.frombuffer(frame, dtype=np.uint8))
                count += 1
                now = time.monotonic()
                if now - last_report >= 1:
                    event('progress', frames=count)
                    last_report = now
    finally:
        if decoder is not None:
            if decoder.poll() is None:
                decoder.terminate()
                try:
                    decoder.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    decoder.kill()
                    decoder.wait()
            decoder.stdout.close()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        event('error', message='Video output failed. Check FFmpeg, pyvirtualcam, and OBS Virtual Camera.')
        raise SystemExit(1)
