from fractions import Fraction
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from PyQt6.QtCore import QBuffer, QIODevice, QProcess
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from preview import JpegFrames, PreviewPipe
from stream import StreamConfig, sender_arguments


def jpeg(color):
    image = QImage(64, 48, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "JPEG")
    return bytes(buffer.data())


class FrameParserTests(unittest.TestCase):
    def test_split_markers_and_multiple_frames_keep_latest(self):
        first, second = jpeg("red"), jpeg("blue")
        parser = JpegFrames()
        self.assertIsNone(parser.feed(b"noise" + first[:1]))
        self.assertIsNone(parser.feed(first[1:-1]))
        self.assertEqual(parser.feed(first[-1:] + second), second)
        self.assertFalse(parser.buffer)

    def test_partial_following_frame_is_preserved(self):
        first, second = jpeg("red"), jpeg("green")
        parser = JpegFrames()
        self.assertEqual(parser.feed(first + second[:100]), first)
        self.assertEqual(parser.feed(second[100:]), second)

    def test_corrupt_unterminated_frame_cannot_grow_forever(self):
        parser = JpegFrames()
        self.assertIsNone(parser.feed(b"\xff\xd8" + b"x" * parser.MAX_BUFFER))
        self.assertLessEqual(len(parser.buffer), parser.MAX_BUFFER)
        valid = jpeg("blue")
        self.assertEqual(parser.feed(valid), valid)


class PreviewTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait(self, predicate, timeout=8):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.01)
        self.assertTrue(predicate(), "Preview did not arrive before timeout")

    def test_private_fifo_delivers_image_and_cleans_up(self):
        pipe = PreviewPipe()
        images = []
        pipe.frame_ready.connect(images.append)
        path = Path(pipe.start())
        try:
            self.assertTrue(path.is_fifo())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with path.open("wb", buffering=0) as output:
                output.write(jpeg("red"))
            self.wait(lambda: bool(images))
            self.assertEqual((images[-1].width(), images[-1].height()), (64, 48))
            self.assertGreater(images[-1].pixelColor(0, 0).red(), 240)
        finally:
            pipe.stop()
        self.assertIsNone(pipe.fd)
        self.assertFalse(path.parent.exists())

    @unittest.skipUnless(shutil.which("gst-launch-1.0"), "GStreamer is required")
    def test_real_pipeline_splits_one_source_into_video_and_preview(self):
        pipe = PreviewPipe()
        images = []
        pipe.frame_ready.connect(images.append)
        path = pipe.start()
        process = QProcess()
        try:
            with tempfile.TemporaryDirectory() as directory:
                video = Path(directory) / "test.ts"
                args = sender_arguments(StreamConfig("1042", 1280, 720, Fraction(30), "test@receiver.example"), path)
                # Replace only the physical source with a moving test pattern;
                # exercise the app's actual decoder, tee, encoder and FIFO sink.
                args = ["-q", "-e", "videotestsrc", "num-buffers=45", "is-live=true", "pattern=ball",
                        "!", "video/x-raw,width=1280,height=720,framerate=30/1"] + args[args.index("decodebin") - 1:]
                process.setStandardOutputFile(str(video))
                process.start("gst-launch-1.0", args)
                self.wait(lambda: process.state() == QProcess.ProcessState.NotRunning)
                self.assertEqual(process.exitCode(), 0, bytes(process.readAllStandardError()).decode())
                self.assertGreater(len(images), 5)
                self.assertEqual((images[-1].width(), images[-1].height()), (640, 360))
                self.assertGreater(video.stat().st_size, 1000)
        finally:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
                process.waitForFinished(2000)
            pipe.stop()


if __name__ == "__main__":
    unittest.main()
