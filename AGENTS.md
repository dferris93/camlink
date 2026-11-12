# CamLink contributor and agent instructions

## Purpose and runtime

CamLink is a Linux PyQt6 desktop application that shares a local PipeWire webcam
with a remote Linux computer. Users select the camera, resolution, frame rate,
SSH destination, and either encrypted SSH video or authenticated SRTP over UDP.
Both transports use SSH for remote setup and lifecycle control. The remote
output is the v4l2loopback device `/dev/video42`, labeled `Laptop Webcam`, for
Zoom and other video applications. Local preview shares the camera capture.

Run the GUI through `./camlink`; it uses the checkout's `.venv`. SRTP workers
intentionally use `/usr/bin/python3` with distribution-provided PyGObject and
GStreamer bindings on both computers. The receiver does not need a checkout or
PyQt: its worker source and runtime configuration are sent over SSH. Consult
README.md for sender/receiver dependencies on Ubuntu and Fedora.

## Code map

| File | Responsibility |
| --- | --- |
| `app.py` | Qt window, selectors, settings, sign-in dialogs, discovery orchestration |
| `cameras.py` | PipeWire inventory parsing, camera classification, supported modes |
| `stream.py` | Process ownership, transport pipelines, state transitions, watchdogs |
| `remote.py` | Virtual-camera format probing, configuration and validation |
| `auth.py`, `askpass.py` | Private Unix-socket bridge for OpenSSH prompts |
| `srtp.py` | Fresh session keys, stream identifiers, private configuration messages |
| `media_worker.py` | System-GStreamer sender/receiver, SRTP authentication and replay checks |
| `preview.py` | Private preview FIFO, bounded JPEG parsing, aspect-correct display |
| `camlink`, `packaging/` | Portable venv launcher and desktop-entry source |

## Invariants

- Opening the app must not capture video. Stop, cancellation, failure and window
  close must clean up owned processes, preview FIFOs, sockets and session state.
  Keep Qt responsive; do not block the UI thread for SSH, discovery or media work.
- Use OpenSSH's host verification and existing agent/configuration. One owned
  authenticated connection serves a stream session, including password and
  keyboard-interactive MFA/TOTP prompts. Cancel must stop connection attempts.
- SRTP must fail closed. Generate a fresh random key/salt and SSRC for every
  Start, reject unknown SSRCs, authenticate before jitter buffering/decoding,
  and preserve replay protection. Never add a plaintext UDP fallback.
- Never put passwords, TOTP responses or SRTP keys in argv, environment
  variables, logs, settings, fixtures or disk files. Preserve core/debug-dump
  protections and bounded cleanup. Use stdin/private IPC for credentials.
- Probe and read back the remote pixel format, dimensions, stride and buffer
  size before capture, after configuration, and after video starts. Zoom can
  retain buffers and silently clamp a new mode. Do not declare Streaming when
  the accepted format differs, and do not blacklist supported resolutions.
- Preserve timestamp repair after raw I420 conversion. Some PipeWire cameras
  have stalled timestamps; inserting `videorate` before repair can duplicate
  black frames and consume excessive CPU. Preview rate limiting happens later.
- Use camera-advertised frame rates where available. Fallback rates, including
  60 fps, are requests, not proof that a camera physically produces that rate.
- Keep the public CamLink name separate from compatibility identifiers:
  `QSettings("LocalTools", "LaptopWebcam")`, desktop ID `laptop-webcam`, and the
  remote camera label remain intentional. Changes need a migration strategy.
- Keep machine-specific receivers, usernames, addresses and paths out of the
  checkout. Use user settings and explicit configuration; tests use reserved
  example destinations and loopback sockets.

## Required test coverage

Maintain behavior-based regression coverage for the paths affected by a change.
Do not substitute a line-coverage percentage or tests that merely repeat the
implementation for meaningful failure and lifecycle checks.

| Area | Required coverage |
| --- | --- |
| Camera discovery and modes | Built-in/USB classification, external preference, virtual-device exclusion, empty inventory, scalar/list/range modes, fractional and missing frame rates |
| Qt and process lifecycle | First-run destination validation, persisted selectors, start/stop/restart, capture/setup failures, watchdog timeout, window close and resource cleanup |
| Remote format safety | Silent resolution clamping before capture, races during configuration and after startup, invalid pixel format/stride/buffer size |
| Authentication | Password followed by verification code through the actual askpass helper, one shared connection, masked fields, cancellation and no credential leakage |
| Preview | Fragmented JPEG boundaries, bounded malformed-input buffering, private FIFO permissions/cleanup, and a real pipeline splitting one source into stream plus preview |
| SRTP | Real encrypted H.264 decoding on a selected odd UDP port, fresh keys/SSRCs on restart, no keys in argv/logs, receiver shutdown without video |
| SRTP rejection | Actual decoder rejects previous/wrong keys, modified packets, plaintext RTP, malformed packets, unknown SSRCs and replays; rejected input does not stop or poison valid authentication |

Current suites are `tests/test_app.py`, `tests/test_preview.py` and
`tests/test_rtp.py`. Qt/process tests use isolated QSettings and controlled
subprocess fixtures. Media tests use generated test patterns and real local
GStreamer/FFmpeg; they do not require a camera, remote account or root.
Authentication fixtures exercise IPC and dialogs, not a live server's PAM/MFA
policy. Do not describe them as proof of a particular server configuration.

Before committing behavioral changes, run:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
git diff --check
```

Install the README's test dependencies first. Tests need permission to bind
local Unix/UDP sockets. Missing media dependencies may skip integration tests;
skips are not evidence that the corresponding feature passed. Report counts,
failures and skips accurately, and require the relevant real integration tests
to run before declaring media/security changes validated. Rerun the full suite
after shared controller, authentication, transport or mode-validation changes.
For documentation and low-impact launcher changes, use focused checks such as
`bash -n camlink` and `desktop-file-validate packaging/laptop-webcam.desktop`.

For hardware/remote behavior changes, use an explicitly authorized camera and
receiver for a live smoke test when available: confirm local preview, real
frames and accepted format remotely, then verify shutdown releases the camera,
FFmpeg and UDP port. Preserve user settings and avoid recording camera images.
Do not silently connect to a contributor's machine or change firewalls, SSH
policy, kernel modules or active video-app sessions as part of automated tests.

## Repository hygiene

Keep `.venv`, Python caches, logs, captures, runtime credentials and local
configuration out of commits. Review staged files and run
`git diff --cached --check` before committing. Update README.md when dependencies, setup steps,
transport semantics or user-visible behavior change; keep Ubuntu and Fedora
instructions distinct. Never claim a distro combination was tested solely
because its package names were checked.
