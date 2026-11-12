"""PipeWire camera discovery and advertised capture modes (no Qt dependency)."""
from dataclasses import dataclass
from fractions import Fraction
import re

COMMON_SIZES = ((320, 240), (640, 480), (800, 600), (1280, 720), (1920, 1080), (2560, 1440), (3840, 2160))
COMMON_RATES = tuple(Fraction(n) for n in (15, 24, 25, 30, 50, 60))
FALLBACK_RATES = tuple(Fraction(n) for n in (15, 24, 25, 30, 60))


def rate_text(rate):
    return str(rate.numerator) if rate.denominator == 1 else f"{float(rate):.3f}".rstrip("0")


def fraction(value):
    if isinstance(value, dict) and "num" in value:
        return Fraction(value["num"], value["denom"])
    if isinstance(value, (int, float, str)):
        return Fraction(str(value))
    raise ValueError("Not a frame rate")


def choices(value, leaf):
    """Expand SPA scalar, enum and range choices into useful UI choices."""
    if isinstance(value, list):
        return [item for entry in value for item in choices(entry, leaf)]
    if isinstance(value, dict) and "default" in value:
        return choices(value["default"], leaf) + choices(value.get("values", []), leaf)
    try:
        return [leaf(value)]
    except (ValueError, TypeError, KeyError, ZeroDivisionError):
        return []


def dimensions(value):
    return int(value["width"]), int(value["height"])


def sizes_from(value):
    sizes = choices(value, dimensions)
    if isinstance(value, dict) and "min" in value and "max" in value:
        low, high = dimensions(value["min"]), dimensions(value["max"])
        step = dimensions(value.get("step", {"width": 1, "height": 1}))
        sizes += [size for size in COMMON_SIZES if all(
            low[i] <= size[i] <= high[i] and (size[i] - low[i]) % max(1, step[i]) == 0
            for i in (0, 1))]
    return {size for size in sizes if all(0 < n <= 8192 and n % 2 == 0 for n in size)}


def rates_from(value):
    rates = choices(value, fraction)
    if isinstance(value, dict) and "min" in value and "max" in value:
        low, high = fraction(value["min"]), fraction(value["max"])
        rates += [rate for rate in COMMON_RATES if low <= rate <= high]
    return {rate for rate in rates if 0 < rate <= 120}


@dataclass(frozen=True)
class Mode:
    width: int
    height: int
    rates: tuple[Fraction, ...]
    rates_reported: bool


@dataclass(frozen=True)
class Camera:
    name: str
    serial: str
    description: str
    kind: str
    modes: tuple[Mode, ...]


def parse_cameras(objects):
    devices = {str(obj["id"]): obj.get("info", {}).get("props", {})
               for obj in objects if obj.get("type") == "PipeWire:Interface:Device"}
    cameras = []
    for obj in objects:
        info = obj.get("info", {})
        node = info.get("props", {})
        if obj.get("type") != "PipeWire:Interface:Node" or node.get("media.class") != "Video/Source":
            continue
        props = devices.get(str(node.get("device.id")), {}) | node
        if props.get("device.api") not in ("libcamera", "v4l2"):
            continue
        if str(props.get("node.virtual")).lower() == "true" or props.get("api.v4l2.cap.driver") == "v4l2 loopback":
            continue
        if not node.get("node.name") or node.get("object.serial") is None:
            continue
        description = props.get("node.description") or props.get("device.description") or node["node.name"]
        location = props.get("api.libcamera.location")
        bus = " ".join(str(props.get(key, "")) for key in ("device.bus", "device.bus-path", "api.v4l2.cap.bus_info"))
        if location in ("front", "back") or re.search(r"built[- ]?in|integrated|internal", description, re.I):
            kind = "Built-in"
        elif location == "external" or "usb" in bus.lower():
            kind = "External"
        else:
            kind = "Camera"
        mode_rates, reported = {}, {}
        for fmt in info.get("params", {}).get("EnumFormat", []):
            if fmt.get("mediaSubtype") not in ("raw", "mjpg", "jpeg"):
                continue
            rates = rates_from(fmt.get("framerate"))
            for size in sizes_from(fmt.get("size")):
                mode_rates.setdefault(size, set()).update(rates)
                reported[size] = reported.get(size, False) or bool(rates)
        # Some backends expose no mode table until capture starts.
        if not mode_rates:
            mode_rates = {size: set() for size in ((640, 480), (1280, 720), (1920, 1080))}
        modes = tuple(Mode(w, h, tuple(sorted(rates or FALLBACK_RATES)), reported.get((w, h), False))
                      for (w, h), rates in sorted(mode_rates.items(), key=lambda item: item[0][0] * item[0][1]))
        cameras.append(Camera(node["node.name"], str(node["object.serial"]), description, kind, modes))
    return sorted(cameras, key=lambda camera: ({"External": 0, "Camera": 1, "Built-in": 2}[camera.kind], camera.name))
