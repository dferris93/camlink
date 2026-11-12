"""Per-stream SRTP credentials; never placed in settings or command lines."""
import base64
from dataclasses import dataclass, field
import json
from pathlib import Path
import secrets

MEDIA_PYTHON = "/usr/bin/python3"
WORKER = Path(__file__).with_name("media_worker.py")


@dataclass(frozen=True)
class SrtpSession:
    key: bytes = field(repr=False)
    ssrc: int

    @classmethod
    def create(cls):
        # AES_CM_128_HMAC_SHA1_80: 16-byte master key + 14-byte master salt.
        return cls(secrets.token_bytes(30), secrets.randbits(32))

    def message(self, **settings):
        return (json.dumps(dict(settings, key=base64.b64encode(self.key).decode(), ssrc=self.ssrc)) + "\n").encode()

    def redact(self, text):
        return text.replace(self.key.hex(), "[redacted]").replace(base64.b64encode(self.key).decode(), "[redacted]")
