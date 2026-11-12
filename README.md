# CamLink

PyQt6 desktop app for sending a local webcam over SSH or encrypted SRTP/UDP to the `Remote Webcam`
virtual camera (`/dev/video42`) on the remote computer.

## Quick start

The **sender** is the Linux desktop with the physical webcam. The **receiver**
is the Linux computer running Zoom or another video app. Choose the Ubuntu or
Fedora commands separately for each computer; their distributions can differ.
The Ubuntu commands target Ubuntu 24.04 LTS and compatible releases.

**Use Secure RTP (UDP) for video.** It is recommended for low latency,
especially on lossy networks. Use SSH video only as a fallback when UDP is
unavailable. Both transports still require SSH for sign-in and receiver setup;
see [Video transport and sign-in](#video-transport-and-sign-in) for details.

### 1. Install the sender dependencies

Run **one** of these blocks on the computer with the webcam. These include
libcamera integration for built-in cameras; the camera's driver and firmware
must already work with PipeWire.

**Ubuntu sender:**

```bash
sudo apt update
sudo apt install software-properties-common
sudo add-apt-repository -y universe
sudo apt update
sudo apt install python3 python3-venv python3-pip python3-gi \
  gir1.2-gstreamer-1.0 openssh-client pipewire pipewire-bin wireplumber \
  gstreamer1.0-tools gstreamer1.0-pipewire gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly libspa-0.2-libcamera libxcb-cursor0 \
  libxkbcommon-x11-0 desktop-file-utils
```

**Fedora sender:**

```bash
sudo dnf install \
  "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm"
sudo dnf install python3 python3-pip python3-gobject openssh-clients \
  pipewire pipewire-utils wireplumber pipewire-gstreamer \
  pipewire-plugin-libcamera gstreamer1 gstreamer1-plugins-base \
  gstreamer1-plugins-good gstreamer1-plugins-bad-free \
  gstreamer1-plugins-ugly xcb-util-cursor libxkbcommon-x11 desktop-file-utils
```

Download or clone this repository on the sender, open a terminal in its root
directory (the folder containing `requirements.txt`), and install the app in
its own virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 2. Install the receiver dependencies

Run **one** of these blocks on the remote computer. It does not need a CamLink
checkout or a Python virtual environment.

**Ubuntu receiver:**

```bash
sudo apt update
sudo apt install software-properties-common
sudo add-apt-repository -y universe
sudo apt update
sudo apt install openssh-server ffmpeg v4l-utils v4l2loopback-dkms \
  "linux-headers-$(uname -r)" kmod python3 python3-gi \
  gir1.2-gstreamer-1.0 gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
sudo systemctl enable --now ssh
```

**Fedora receiver:** The conditional command replaces `ffmpeg-free` if it is
installed; review DNF's proposed package changes before accepting.

```bash
sudo dnf install \
  "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm"
if rpm -q ffmpeg-free >/dev/null 2>&1; then
  sudo dnf swap ffmpeg-free ffmpeg --allowerasing
fi
sudo dnf install openssh-server ffmpeg v4l-utils akmod-v4l2loopback \
  kernel-devel-matched python3 python3-gobject gstreamer1 \
  gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free
sudo systemctl enable --now sshd
sudo akmods --force
modinfo v4l2loopback
```

The module needs headers/development files matching the running kernel. If
the build fails or Secure Boot rejects it, follow the distribution notes in
[Install](#install) before continuing.

### 3. Create the receiver's virtual webcam

On **either receiver distribution**, run this as the user CamLink will SSH
into. It creates the device now and configures it to return after reboot:

```bash
printf '%s\n' 'v4l2loopback' | sudo tee /etc/modules-load.d/camlink.conf
printf '%s\n' 'options v4l2loopback devices=1 video_nr=42 card_label="Remote Webcam" exclusive_caps=1' \
  | sudo tee /etc/modprobe.d/camlink.conf
sudo modprobe v4l2loopback devices=1 video_nr=42 \
  card_label="Remote Webcam" exclusive_caps=1
sudo udevadm settle
sudo usermod -aG video "$USER"
```

Log out and back in, and reconnect any existing SSH session, so the new group
membership takes effect. If v4l2loopback was already loaded with different
settings, see [Common receiver setup](#common-receiver-setup).

Allow SSH and your chosen SRTP UDP port through the receiver's firewall and
any intervening router/firewall. The app defaults to UDP **5004**; it lets you
choose a different port. Copyable UFW and firewalld examples are in
[Common receiver setup](#common-receiver-setup).

### 4. Launch and stream

Back on the **sender**, run from the repository directory as your normal
desktop user:

```bash
./camlink
```

Choose the camera, resolution and frame rate, enter the receiver as
`user@hostname`, select **Secure RTP (UDP)** and the allowed UDP port, then
click **Start streaming**. In Zoom on the receiver, select **Remote Webcam**.
Use **SSH** video only if the direct UDP path is unavailable.

For package explanations, optional desktop shortcuts and troubleshooting,
see [Install](#install).

## Using CamLink

The launcher uses this project's `.venv` automatically. Choose a camera,
resolution, frame rate and video transport, enter the remote computer as
`user@hostname`, then click **Start streaming**. New installations have no
preconfigured receiver.
**Stop** and closing the window stop capture and the remote receiver. Opening the app never starts
the camera. Use **Refresh** after connecting or disconnecting a webcam.

The **Local preview** panel shows the same camera view while streaming. It
preserves the picture's aspect ratio and clears when the stream stops. The
preview shares the existing capture, with its own queue, and is capped at
640×480 and 15 fps to keep the interface responsive. The remote output still
uses your selected resolution and frame rate. Preview frames pass through a
private local pipe and are not saved as video or image files.

External cameras are preferred initially. Your camera name, resolution, frame
rate, receiver, transport, RTP destination and port are remembered; unavailable saved cameras fall back to the
first detected camera. Existing installations retain their settings and desktop
identity through the legacy internal identifiers. For an existing receiver,
follow the camera-label migration in [Common receiver setup](#common-receiver-setup).

The selectors use PipeWire's advertised modes. Cameras that omit frame rates
offer 15, 24, 25, 30 and 60 fps, with a notice that these are requests rather than
verified capabilities. Unsupported modes produce a startup error. Raw video
and MJPEG are supported. The stream regenerates timestamps to work around the
built-in camera's broken PipeWire timestamps.

## Video transport and sign-in

**Secure RTP (UDP) is the recommended video transport**, especially on lossy
networks. It sends encrypted, authenticated H.264 SRTP packets directly to the
remote computer over UDP. UDP avoids waiting for TCP retransmissions before
delivering newer video packets, which can reduce latency and stalls when
packets are lost. Loss can still cause dropped frames or visible artifacts;
SRTP does not recover missing packets. See
[RFC 9317's discussion of media transport](https://www.rfc-editor.org/rfc/rfc9317.html#section-6.2).
Saved RTP settings automatically use SRTP; there is no unencrypted UDP fallback.

**SSH video is recommended only as a fallback** when UDP is blocked or the
receiver cannot be reached directly over UDP. It sends H.264 video in MPEG-TS
through the encrypted SSH connection. TCP's ordered delivery can hold up
newer video while lost packets are retransmitted.

Both modes use SSH to prepare the virtual camera, start FFmpeg, check the video
format, and stop the receiver. Both include the local preview.

Select **Secure RTP (UDP)** to reveal **RTP port** and **RTP destination**. The port can
be any number from 1024 through 65535, including odd ports;
5004 is the initial default. The receiver binds the selected port, and the
sender uses that same port. This video-only SRTP path uses one UDP port.
Allow inbound UDP to the selected RTP port from the laptop through any
firewalls between the two computers. No firewall rules are changed by the app.

Each Start generates a new cryptographically random 128-bit master key and
112-bit master salt, plus a new stream identifier. SRTP uses
`AES_CM_128_HMAC_SHA1_80`: AES-128 encryption and an 80-bit HMAC authentication
tag. GStreamer's libsrtp receiver checks authentication and replay protection
before jitter buffering, H.264 depayloading or video decoding. Packets with a
wrong key, a modified payload, or a reused sequence number are discarded;
unknown stream identifiers cannot create extra receiver contexts. Old session
keys cannot authenticate a new session. Rejection counts appear in Connection
details. Authentication does not prevent traffic from consuming network
bandwidth, and SRTP leaves RTP routing headers visible.

The session key travels to the receiver only through the authenticated SSH
connection, and to the local sender through its stdin pipe. Keys are never put
in command arguments, environment variables, logs, settings, or temporary
files. The app releases its session credentials on Stop; workers exit and a
subsequent Start generates new credentials. Core dumps and GStreamer debug
caps dumps are disabled where key material is held. A missing key, missing
SRTP component, or exhausted key stops the stream rather than downgrading it.

Leave **RTP destination** empty to use the remote computer's address from the
authenticated SSH connection. Set it explicitly when you need another interface
or use an SSH jump host. The UDP destination must be directly reachable from
the laptop; SSH connectivity alone does not establish a UDP path.

OpenSSH uses your existing SSH configuration, agent and keys. If the server asks
for a password, key passphrase, or keyboard-interactive MFA/TOTP verification
code, the app displays a masked sign-in dialog. Respond to each prompt in order;
the server controls which factors are required. A new host's fingerprint is
presented for confirmation, and changed host keys are rejected by OpenSSH as
usual. Canceling a prompt stops the connection. The app does not store passwords
or codes in settings, files, command arguments, or logs. Prompts and answers
travel through a private local Unix socket; one authenticated SSH connection
is shared by setup, streaming and verification for that session.

For SRTP, the app starts a small Python/GStreamer receiver through SSH and
sends the key and stream settings over that channel. The receiver passes only
authenticated H.264 data to FFmpeg through a local pipe. No plaintext UDP
relay, SDP file, persistent service, or installed remote application is used.
The sender starts once the secure receiver is listening. A small control
heartbeat continues over SSH while the video travels directly over UDP.
Closing that channel stops the receiver and FFmpeg. Missing heartbeats also
stop them after approximately 15 seconds, with bounded child-process cleanup.

## Install

CamLink currently targets Linux on both ends. **Sender** means the computer
with the physical webcam and GUI; **receiver** means the computer running Zoom
or another consumer of the virtual webcam. Choose the instructions for each
computer's distribution independently; the two distributions need not match.

### Dependencies by role

| Dependency | Sender | Receiver |
| --- | --- | --- |
| Python 3.10+ and a venv | GUI and launcher | System Python for SRTP; no venv needed |
| PyQt6 from `requirements.txt` | Required inside `.venv` | Not required |
| Running PipeWire and WirePlumber session; `pw-dump` | Required for camera discovery/capture | Not required by CamLink |
| OpenSSH | Client | Server and a reachable user account |
| GStreamer tools, base/good/bad plugins | Capture, conversion, preview, H.264 parsing, SRTP | SRTP reception and H.264 depayloading only |
| GStreamer `x264enc` (ugly plugins) | Required for either transport | Not required |
| System PyGObject and `Gst-1.0` introspection | SRTP worker | SRTP worker |
| FFmpeg with H.264 decoder and V4L2 output | Needed for integration tests only | Required for either transport |
| `v4l2loopback`, `v4l2-ctl`, `modprobe`, `udevadm` | Not required for sending | Required to provide `/dev/video42` |
| Bash and ordinary Linux command-line utilities | Launcher | Setup scripts |

The GUI and PyQt runtime are isolated in `.venv`. Media workers deliberately
use **`/usr/bin/python3`**, with GI/GStreamer installed by the OS package manager.
Installing GI into the GUI venv is not a substitute. No Qt installation or
CamLink checkout is needed on the receiver. The optional GStreamer Python
override packages (`python3-gst-1.0` on Ubuntu, `python3-gstreamer1` on Fedora)
are not required by the current worker; PyGObject and the Gst typelib are.

### Ubuntu sender

These package names target Ubuntu 24.04 LTS and compatible releases. Some are
in Ubuntu's **universe** repository; enable it first if it is disabled. These
are desktop-session instructions, not a replacement for the machine's camera
driver or firmware.

Both Ubuntu roles need universe. If it is not already enabled:

```bash
sudo apt install software-properties-common
sudo add-apt-repository universe
```

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip python3-gi \
  gir1.2-gstreamer-1.0 openssh-client pipewire pipewire-bin wireplumber \
  gstreamer1.0-tools gstreamer1.0-pipewire gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly libxcb-cursor0 libxkbcommon-x11-0 \
  desktop-file-utils
```

For libcamera-managed built-in cameras, also install `libspa-0.2-libcamera`.
USB UVC webcams generally use the kernel V4L2 driver. A camera must appear as a
PipeWire video source before CamLink can discover it. Run the GUI as the normal
desktop user so it can access that user's PipeWire session.

Ubuntu packages include the [PipeWire GStreamer plugin](https://packages.ubuntu.com/noble/gstreamer1.0-pipewire)
and [ugly codec plugins](https://packages.ubuntu.com/noble/gstreamer1.0-plugins-ugly).

### Ubuntu receiver

```bash
sudo apt update
sudo apt install openssh-server ffmpeg v4l-utils v4l2loopback-dkms \
  "linux-headers-$(uname -r)" kmod python3 python3-gi \
  gir1.2-gstreamer-1.0 gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
sudo systemctl enable --now ssh
```

Ubuntu builds the virtual-camera module with
[v4l2loopback-dkms](https://packages.ubuntu.com/noble/v4l2loopback-dkms). The headers
must match the running kernel. Ubuntu derivatives with custom kernels may use
different header packages; install those from the distribution supplying the
kernel. Proceed to the common receiver setup below after the module builds.

### Fedora sender

Fedora's base repositories do not supply the `x264enc` plugin used by CamLink.
Enable **RPM Fusion Free** for your Fedora release before installing
`gstreamer1-plugins-ugly`. The installed `gstreamer1-plugins-ugly-free` package
alone is not sufficient for this encoder. See the
[RPM Fusion setup instructions](https://rpmfusion.org/Configuration).

```bash
sudo dnf install \
  "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm"
sudo dnf install python3 python3-pip python3-gobject openssh-clients \
  pipewire pipewire-utils wireplumber pipewire-gstreamer \
  gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good \
  gstreamer1-plugins-bad-free gstreamer1-plugins-ugly \
  xcb-util-cursor libxkbcommon-x11 desktop-file-utils
```

For libcamera cameras, also install `pipewire-plugin-libcamera`. Fedora's
[pipewire-gstreamer](https://packages.fedoraproject.org/pkgs/pipewire/pipewire-gstreamer/)
and [pipewire-plugin-libcamera](https://packages.fedoraproject.org/pkgs/pipewire/pipewire-plugin-libcamera/)
package names differ from Ubuntu's. Fedora's `gstreamer1` supplies the core
Gst introspection data used with `python3-gobject`.

### Fedora receiver

Enable RPM Fusion Free as above for the full FFmpeg build and
`akmod-v4l2loopback`; Fedora uses **akmods**, rather than Ubuntu's DKMS package.

```bash
sudo dnf install openssh-server ffmpeg v4l-utils akmod-v4l2loopback \
  kernel-devel-matched python3 python3-gobject gstreamer1 \
  gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free
sudo systemctl enable --now sshd
sudo akmods --force
modinfo v4l2loopback
```

If `ffmpeg-free` is already installed and DNF reports a conflict with `ffmpeg`,
use the RPM Fusion replacement transaction before retrying the install:

```bash
sudo dnf swap ffmpeg-free ffmpeg --allowerasing
```

Review that transaction's package changes. `kernel-devel-matched` tracks the
packaged kernel; after a kernel update, boot the matching kernel or install
development files for the kernel you actually run before rebuilding modules.
RPM Fusion publishes [akmod-v4l2loopback packages](https://download1.rpmfusion.org/free/fedora/updates/43/x86_64/a/),
and Fedora's [v4l-utils](https://packages.fedoraproject.org/pkgs/v4l-utils/v4l-utils/)
provides `v4l2-ctl`.

### Common receiver setup

Run these commands on the receiver, using the account that CamLink will SSH
into. The current application expects device number **42**, card label
**Remote Webcam**, and `exclusive_caps=1`:

```bash
sudo modprobe v4l2loopback devices=1 video_nr=42 \
  card_label="Remote Webcam" exclusive_caps=1
sudo udevadm settle
sudo usermod -aG video "$USER"
```

Reconnect SSH after changing group membership; log out and back in for GUI
applications that also need the new group. Verify access with
`test -w /dev/video42` and `v4l2-ctl -d /dev/video42 --get-fmt-video-out` before
starting a stream. If the module is already loaded with different parameters,
adjust the existing module configuration and reboot instead of unloading a
module used by other cameras.

For a persistent device after reboot:

```bash
printf '%s\n' 'v4l2loopback' | sudo tee /etc/modules-load.d/camlink.conf
printf '%s\n' 'options v4l2loopback devices=1 video_nr=42 card_label="Remote Webcam" exclusive_caps=1' \
  | sudo tee /etc/modprobe.d/camlink.conf
```

**Upgrading an existing receiver:** Older versions used the camera label
`Laptop Webcam`. Run the persistence commands above and update any existing
v4l2loopback options in other files under `/etc/modprobe.d/` to use
`card_label="Remote Webcam"`. Keep the device number and other module options
consistent. Stop CamLink and reboot the receiver when convenient; running
`modprobe` again does not rename a device whose module is already loaded.
Do not unload a module used by other cameras. After reboot, verify the name:

```bash
cat /sys/class/video4linux/video42/name
```

It should print `Remote Webcam`. Start CamLink and select **Remote Webcam**
again in Zoom or other video apps. CamLink rejects a device with the old label
and shows upgrade instructions in Connection details until the migration is
complete. Saved sender settings and desktop shortcuts are preserved.

With the device already loaded and writable, streaming requires no sudo.
If it is missing, CamLink attempts `sudo -n modprobe ...`; an SSH password/MFA
prompt does **not** answer a sudo prompt. Preloading the module at boot avoids
granting passwordless sudo to the app's account. With Secure Boot enabled, use
the distribution's module-signing/enrollment procedure if the module is
rejected. See the [v4l2loopback project's build and module documentation](https://github.com/v4l2loopback/v4l2loopback)
for matching kernel headers, module parameters, and signing details.

Allow SSH to the receiver. For SRTP, also allow the chosen **UDP destination
port** from the sender through the receiver firewall and any intervening
router/firewall. Ubuntu commonly uses UFW, while Fedora commonly uses
firewalld. For example, replacing `192.0.2.10` with the sender address and
`5004` with your chosen port. On Fedora, replace `public` with the receiving
interface's zone shown by `firewall-cmd --get-active-zones`:

```bash
# Ubuntu receiver, if UFW is enabled:
sudo ufw allow from 192.0.2.10 to any port 5004 proto udp

# Fedora receiver, if firewalld is enabled (use the receiving interface's zone):
sudo firewall-cmd --get-active-zones
sudo firewall-cmd --permanent --zone=public --add-rich-rule='rule family="ipv4" source address="192.0.2.10/32" port port="5004" protocol="udp" accept'
sudo firewall-cmd --reload
```

SSH working does not prove UDP is reachable. IPv6 networks require matching
IPv6 firewall rules. The app does not change any firewall rules itself.

### Create the sender's venv and launch

Run from the checkout root after installing that computer's sender packages:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./camlink
```

The launcher resolves its checkout location, including when launched through
a symlink. To add a per-user command and application-menu entry:

```bash
mkdir -p ~/.local/bin ~/.local/share/applications ~/.local/share/icons/hicolor/scalable/apps
ln -s "$PWD/camlink" ~/.local/bin/camlink
cp packaging/laptop-webcam.desktop ~/.local/share/applications/
cp webcam.svg ~/.local/share/icons/hicolor/scalable/apps/camlink.svg
update-desktop-database ~/.local/share/applications
```

Ensure `~/.local/bin` is on the graphical session's PATH. If a `camlink` launcher
already exists, update it to the intended checkout instead of creating a
second one. The desktop filename stays `laptop-webcam.desktop` for existing
shortcut compatibility; the displayed application name is CamLink.

### Verify dependencies before troubleshooting video

On the sender:

```bash
pw-dump >/dev/null
for element in pipewiresrc decodebin videoconvert videoscale identity tee queue \
  videorate jpegdec jpegenc filesink x264enc h264parse mpegtsmux fdsink \
  rtph264pay srtpenc udpsink; do
  gst-inspect-1.0 "$element" >/dev/null || exit 1
done
```

On both computers for SRTP:

```bash
/usr/bin/python3 -c 'import gi; gi.require_version("Gst", "1.0"); from gi.repository import Gst; Gst.init(None); print(Gst.version_string())'
```

On the receiver:

```bash
for element in udpsrc srtpdec rtpjitterbuffer rtph264depay h264parse fdsink; do
  gst-inspect-1.0 "$element" >/dev/null || exit 1
done
ffmpeg -hide_banner -decoders | grep -w h264
v4l2-ctl -d /dev/video42 --get-fmt-video-out
```

Live verification has been performed with a Fedora sender and an
Ubuntu-24.04-based Pop!_OS receiver. The Ubuntu-sender and Fedora-receiver
instructions reflect their package names and setup differences, but those
complete installation combinations have not yet been tested on clean systems.

Before changing resolution, switch Zoom (or other remote video apps)
to another camera or close their camera preview. An open consumer can lock the
virtual camera to its previous size. The app checks the accepted format before
capture, after configuring the device, and after video starts. If the device
returns a different size or layout, the app stops with instructions instead of
reporting a mismatched stream as working. Start the stream at the new resolution,
then select **Remote Webcam** again in Zoom. 1080p remains available when the
camera supports it and the remote device is free to change modes.

## Tests

Install the sender dependencies plus FFmpeg (`sudo apt install ffmpeg` on
Ubuntu, or `sudo dnf install ffmpeg` with RPM Fusion on Fedora). Tests also need
the system GI/GStreamer runtime and receiver SRTP elements listed above, but
do not need a physical camera, SSH server, virtual-camera module, or root.

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
```

Tests include password followed by TOTP prompts through the actual askpass
helper, cancellation and cleanup, saved transport/port controls, the existing
remote-format checks and preview, session-key rotation, and real encrypted
GStreamer-to-FFmpeg decoding on a local UDP port. Negative tests send old-key,
modified, unauthenticated, malformed, unknown-source, and replayed packets to
the actual SRTP decoder. Tests need permission to create local Unix and UDP
sockets. SRTP integration tests additionally need the system media bindings
and local FFmpeg.

See [AGENTS.md](AGENTS.md) for the architecture, security invariants, and
required regression coverage. Report skipped integration tests explicitly;
missing media tools are not a passing security or streaming test.

Protocol references: [OpenSSH askpass and connection sharing](https://man.openbsd.org/ssh.1),
[GStreamer SRTP encryption](https://gstreamer.freedesktop.org/documentation/srtp/srtpenc.html),
[GStreamer SRTP authentication/decryption](https://gstreamer.freedesktop.org/documentation/srtp/srtpdec.html),
[FFmpeg network protocols](https://ffmpeg.org/ffmpeg-protocols.html).
