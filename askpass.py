"""OpenSSH askpass helper; secrets travel only through a private Unix socket."""
import json
import os
import socket
import sys


def main():
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(os.environ["WEBCAM_ASKPASS_SOCKET"])
            request = {"prompt": sys.argv[1] if len(sys.argv) > 1 else "SSH authentication",
                       "kind": os.environ.get("SSH_ASKPASS_PROMPT", "")}
            connection.sendall(json.dumps(request).encode() + b"\n")
            with connection.makefile("rb") as response:
                answer = json.loads(response.readline(65536))
            if answer is None:
                return 1
            sys.stdout.write(answer + "\n")
            return 0
    except (OSError, ValueError, KeyError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
