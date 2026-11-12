"""Validate the format actually accepted by the remote virtual webcam."""
import re


def validate_remote_format(config, output):
    dimensions = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", output)
    pixel_format = re.search(r"Pixel Format\s*:\s*'([^']+)'", output)
    stride = re.search(r"Bytes per Line\s*:\s*(\d+)", output)
    size = re.search(r"Size Image\s*:\s*(\d+)", output)
    if not all((dimensions, pixel_format, stride, size)):
        raise ValueError("Could not verify the remote camera's video format. See Connection details.")
    width, height = map(int, dimensions.groups())
    if (width, height) != (config.width, config.height):
        raise ValueError(
            f"Requested {config.width}×{config.height}, but the remote camera is fixed at {width}×{height}. "
            "Switch Zoom or other video apps to another camera, or close their camera preview, then retry. "
            f"You can also select {width}×{height} here."
        )
    if (pixel_format[1] != "YU12" or int(stride[1]) != config.width
            or int(size[1]) != config.width * config.height * 3 // 2):
        raise ValueError(
            "The remote camera accepted an incompatible pixel format or frame layout. "
            "Release Laptop Webcam in Zoom or other video apps, then retry."
        )


def requested_format(config):
    config.validate()
    return f"width={config.width},height={config.height},pixelformat=YU12"


def configure_script(config):
    return ("set -euo pipefail\nexport LC_ALL=C\n"
            f"v4l2-ctl -d /dev/video42 --set-fmt-video-out={requested_format(config)}\n"
            "v4l2-ctl -d /dev/video42 --get-fmt-video-out\n"
            f"v4l2-ctl -d /dev/video42 --set-output-parm={float(config.fps):.6f}\n")
