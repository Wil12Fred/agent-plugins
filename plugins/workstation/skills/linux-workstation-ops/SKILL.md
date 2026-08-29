---
name: linux-workstation-ops
description: Use for local Arch Linux / KDE workstation operational issues on this machine — Wi-Fi/network recovery after a system update (MediaTek mt7925), a device whose DMA has wedged (no data, I/O errors, while the driver looks healthy), an out-of-tree kernel module missing after a kernel upgrade, enabling/disabling a USB webcam per-device (a camera that will not start, or one held open by Chrome, and the `camctl` tool), pasting into a terminal program, and the known gap where agent background-task notifications do not reach the Slack session system. Local-environment ops, not product/application code. Do not use for sound-server profiles, levels or Bluetooth audio (that is `linux-pipewire-audio-ops`), nor for a display cable moved between GPUs (that is `linux-gpu-output-switch`).
---

# Linux Workstation Ops

## Overview

Local operational knowledge for this Arch Linux + KDE workstation: environment-level fixes and known
gaps, not application or product code. Sibling of `linux-slack-kde-notifications` (which covers Slack
notification click-activation on KDE).

## 1. Agent background-task notifications are NOT relayed to the Slack session system

**Gap (documented for a future fix).** When the agent runs work in the background (e.g. a `Bash` call
with `run_in_background`, a CI/deploy wait, a long E2E), the harness injects a `<task-notification>`
event into the **agent's** conversation when that task finishes. **This event is not forwarded to the
user's Slack session-control system** (``slackbridge sessions``, channels `#claude-sessions` /
`#ia-sessions`). So when monitoring from Slack, the user does not see "background task completed" — the
agent reacts to it internally, and today the user relays it manually.

- **Why it matters:** background completions (deploy/pipeline waits, long-running E2E) are invisible on
  the Slack side; only the agent sees them.
- **Future logic (to build):** hook the session bridge to forward `<task-notification>` events
  (task id, status, one-line summary, output-file path) to the Slack channel, so completions show up
  there automatically.
- **Until then:** relay relevant task completions to Slack manually.

## 2. Wi-Fi recovery after `sudo pacman -Syu` (MediaTek mt7925)

**Symptom chain.** After a `sudo pacman -Syu`, the internet sometimes hangs → restarting NetworkManager
(`sudo systemctl restart NetworkManager`) makes the **Wi-Fi interface disappear** (the mt7925 driver
does not come back cleanly after the kernel/firmware update).

**Recovery (works in this environment; root cause not fully confirmed):**

```bash
sudo modprobe -r mt7925e mt7925_common mt792x_lib mt76_connac_lib mt76
echo 1 | sudo tee /sys/bus/pci/devices/0000:83:00.0/reset
sudo modprobe mt7925_common disable_clc=1
sudo modprobe mt7925e disable_aspm=1
nmcli device status
```

**What each step does (best current understanding):**

- `modprobe -r …` — unload the full mt7925 / mt76 driver stack in dependency order (children first:
  `mt7925e` → `mt7925_common` → `mt792x_lib` → `mt76_connac_lib` → `mt76`).
- `echo 1 | sudo tee …/reset` — PCI function-level reset of the Wi-Fi device at `0000:83:00.0`. This
  clears a wedged adapter state that a plain module reload does not fix. **Verify the PCI address**
  first with `lspci -nnk | grep -iA3 net` (it can differ between machines/boots).
- `modprobe mt7925_common disable_clc=1` — reload with CLC (country/regulatory location control)
  disabled, a known workaround for mt7925 bring-up failures.
- `modprobe mt7925e disable_aspm=1` — reload the PCIe driver with ASPM (PCIe power management) disabled,
  which avoids the low-power link state that wedges the adapter.
- `nmcli device status` — confirm the `wifi` device reappeared (state `disconnected` / `connected`).

**Notes:**

- The **PCI reset** is the step that most likely does the real fix — reloading the modules alone is
  often not enough after a system update.
- Why it is needed specifically after an update is **not confirmed**; treat this as a reliable
  workaround, not a root-cause fix. If the root cause is later identified (firmware/ASPM/CLC
  regression), update this section.
- This is local workstation ops — **do not touch product repositories** while applying it.

## 3. An out-of-tree kernel module missing after a kernel upgrade

**Symptom.** Something that needs a kernel module fails right after an upgrade,
and the module is simply not there for the kernel now running:

```
modprobe: FATAL: Module <name> not found in directory /lib/modules/<running-kernel>
```

**Cause.** An out-of-tree module package is built against **one exact kernel
package release**, pkgrel included. When the kernel gets a pkgrel bump before the
module package is rebuilt, the repository briefly ships a pair that does not
match, and an upgrade installs it. Nothing warns: both packages satisfy their
declared dependencies.

**Confirm it in one command** — the module directory the package installed into
is not the one that is running:

```bash
uname -r
ls /usr/lib/modules/*/extramodules/
pacman -Qo /usr/lib/modules/*/extramodules/<name>.ko.zst
```

**Recover** by installing the matching pair from the package cache, which keeps
previous versions:

```bash
ls /var/cache/pacman/pkg/ | grep -E '<kernel>|<module-package>'
bsdtar -tf <cached-module-package> | grep -oE 'usr/lib/modules/[^/]+' | sort -u   # verify the target kernel
sudo pacman -U /var/cache/pacman/pkg/<kernel>-<ver>.pkg.tar.zst \
               /var/cache/pacman/pkg/<kernel>-headers-<ver>.pkg.tar.zst \
               /var/cache/pacman/pkg/<module-package>-<ver>.pkg.tar.zst
```

**Then pin, and write down how to unpin.** A downgrade that a later `-Syu` undoes
is not a fix:

```
# /etc/pacman.conf, inside [options]
IgnorePkg = <kernel> <kernel>-headers <module-package>
```

The pin is released when the module package becomes **newer than** the kernel
package in the repository — not when its version merely changes. Compare the
publication dates rather than the version strings, which can move for unrelated
reasons.

**The rule this leaves behind:** never let a service that a desktop session needs
depend on a module that may be absent. Give the unit's `ExecStartPre` a `-`
prefix so a missing module degrades the session instead of removing it. See
`linux-gpu-output-switch` for the display-manager case in full.

## 4. A device whose DMA is wedged

**Symptom.** The driver is loaded, the device is enumerated, the log is clean —
and the device transfers nothing. Reads fail immediately, or block forever:

```
read error: Input/output error
```

**The measurement that separates this from configuration.** A wedged transfer
shows a stream that is running with a hardware pointer that never advances:

```bash
cat /proc/asound/card<n>/pcm0c/sub0/status
```

```
state: RUNNING      ← the stream started
hw_ptr: 0           ← and moved nothing
avail: 0
```

Any equivalent counter works for other subsystems; the shape is the same —
started, zero progress, no error logged. Once you have that, **stop looking at
configuration.** No setting produces it.

**Recover by re-initialising the single device**, which is far less invasive than
unloading a shared module stack:

```bash
# stop whatever holds the device first
echo 1 | sudo tee /sys/bus/pci/devices/0000:XX:XX.X/remove
sleep 3
echo 1 | sudo tee /sys/bus/pci/rescan
```

Then verify by exercising the device, not by reading its state — a device can
re-enumerate and still not transfer.

**Notes:**

- Section 2 is an instance of this same pattern: the PCI reset, not the module
  reload, is what fixes the wedged network adapter.
- **Verify the PCI address before running it.** Removing the wrong function takes
  out whatever it serves until the next boot.
- If a rescan does not bring the device back, a warm reboot may not either.
  Controllers that hold state across reset often need a full power cycle.
- Applications that enumerate devices at startup keep the old view. After the
  device returns, restart them too, or they report it as still missing.

## 5. Pasting into a program running in a terminal

**Symptom.** The clipboard holds something, another application pastes it fine,
and a terminal program receives nothing.

**Why.** The clipboard is typed by content: whoever copies offers a list of
formats, and whoever pastes picks one it understands. A graphical application is
a display-server client and can ask for any format it supports. **A program
running inside a terminal is not a client at all** — the terminal emulator is.
When you paste, the emulator fetches the clipboard and writes it into the
pseudo-terminal, which carries characters. There is no channel in that interface
for anything else, so a clipboard holding only an image delivers nothing.

**Check what is actually on the clipboard** before assuming the program is at
fault. With no clipboard tool installed, the desktop's clipboard manager answers
over its message bus, and reports images as a placeholder glyph plus dimensions:

```bash
busctl --user call org.kde.klipper /klipper org.kde.klipper.klipper getClipboardContents
```

**The escape hatch some terminal programs use.** A program can bypass the
terminal and read the clipboard itself by invoking a helper. Whether a given one
does is answerable, not guessable:

```bash
for h in wl-paste xclip xsel; do printf '%-10s ' "$h"; grep -ac -- "$h" "$(readlink -f "$(command -v <program>)")"; done
```

A binary that references the helpers needs them installed (`wl-clipboard` for a
Wayland session) and needs the keystroke to **reach it** — the emulator's own
paste shortcut is intercepted before the program sees it, so the program's own
binding is the one to use. A binary that references none of them will not gain
the ability by installing them.

Treat this as evidence, not proof: a string in a binary is not a code path. The
test costs seconds — install, restart the program, paste.

## 6. Small facts about this machine's peripherals

- **A compact keyboard has no navigation cluster; its modifier carries it.**
  `End` is the modifier plus right arrow, `Home` plus left, and page movement plus
  the vertical arrows — so a chord like `Ctrl+End` becomes a three-key
  combination. The top row's behaviour is a module parameter rather than a
  keyboard setting; read it before assuming the keyboard is at fault:
  ```bash
  cat /sys/module/hid_apple/parameters/fnmode    # 1 media keys first, 2 function keys first
  ```
  Persist a change with a file under `/etc/modprobe.d/` and rebuild the initramfs.

- **Wireless signal: the percentage is derived, the dBm is measured.** The
  connection manager also serves a *cached* scan, so its number can sit still
  while the link changes. For a live reading, ask the driver:
  ```bash
  iw dev <iface> link      # signal in dBm, and the negotiated bitrate
  ```
  Better than -50 dBm is excellent, -50 to -60 good, -60 to -70 workable, below
  -70 starts to hurt. The negotiated bitrate says more about the link than either.
  `/proc/net/wireless` and `iwconfig` are empty or obsolete on this hardware.

- **Per-process GPU accounting only covers drivers that publish it.** The kernel
  exposes per-client engine counters in `/proc/<pid>/fdinfo`, which is what the
  desktop's process list reads; a driver that talks to its hardware through its
  own character devices publishes nothing there, and its processes appear to use
  no GPU at all. Use the vendor's own tool for those, and remember that any
  fdinfo-based reader also silently skips processes whose `/proc` entry it cannot
  read.

## 7. Enabling and disabling a webcam (per-device)

A USB webcam is a UVC device driven by the in-tree `uvcvideo` module — generic,
loaded automatically, the same driver for every UVC camera (a vendor driver is
almost never needed; anything from ~2008 on is UVC, and pre-UVC cameras fall to
the `gspca_*` family instead). Two facts shape every "the camera won't start"
report:

- **A camera is exclusive-access.** Only one process may stream from `/dev/video0`
  at a time. A second app does not queue — it fails to open.
- **Chrome keeps the device open after the tab is gone.** Its capture process
  (`chrome --type=utility --utility-sub-type=video_capture.mojom.VideoCaptureService`)
  lingers, and sometimes never exits, holding `/dev/video0`. This is the number-one
  cause of "I plugged the camera in and nothing else can use it", and it is also
  what makes `modprobe -r uvcvideo` fail with `Module is in use` — the driver will
  not unload while any device it owns is open. Find the holder, then free it:
  ```bash
  fuser -v /dev/video0        # who has it open
  kill <pid>                  # or close Chrome entirely
  ```

**Three levels of "disable", from blunt to surgical.** They differ in blast
radius, and only the last one is per-device:

| Action | Turns off | Scope |
|---|---|---|
| `sudo modprobe -r uvcvideo` | the **driver** | **every** UVC camera at once |
| `unbind` in `/sys/bus/usb/drivers/uvcvideo/` | the driver **for this camera** | this camera's video interfaces only |
| `authorized=0` on the USB **device** node | the **USB device itself** | this camera only (video *and* audio); closest to unplugging |

```bash
# surgical: deauthorize just this camera (leaves uvcvideo and other cameras alone)
echo 0 | sudo tee /sys/bus/usb/devices/3-7/authorized   # off
echo 1 | sudo tee /sys/bus/usb/devices/3-7/authorized   # on
```

**`authorized` travels with the device, not the port.** Setting it to 0
deauthorizes *that connected device instance*. Unplug it and plug something else
into the same physical port and the new device re-enumerates with the default
(`authorized=1`) — the "off" does not stick to the socket, so it never blocks an
unrelated device. And `modprobe -r uvcvideo` only affects cameras: a flash drive
or keyboard in that port uses a different driver and is unaffected.

**The node path is not stable — locate by ID.** The USB node moved from `3-9` to
`3-7` across a single unplug/replug in testing. A tool that hard-codes the path
will act on the wrong device; resolve the camera by its `idVendor:idProduct`
(here `093a:2700`) and read the node fresh.

**A slow/laggy feed is usually the hardware, not the system.** A cheap UVC camera
may offer only uncompressed `YUYV` at `640x480` (no MJPEG): that is ~147 Mbps of
raw video over a shared 480 Mbps USB bus plus CPU to decode it. `v4l2-ctl -d
/dev/video0 --list-formats-ext` shows the ceiling; there is nothing to tune —
either force the app to that resolution or use a camera with MJPEG/H.264.

### `camctl` — list and toggle capture devices by device

[`scripts/camctl`](scripts/camctl) lists every video-capture device — **even when
the driver is unloaded**, since it enumerates by USB Video class (`0e`), not by
`/dev/video*` — and toggles one via `authorized`, freeing any holder first. It
resolves the camera by USB node *or* by `vendor:product`, so a changed node path
does not matter.

```bash
camctl                 # list: ON/OFF, node, id, name, and who holds it
camctl off             # deauthorize (frees the holder, e.g. Chrome, then authorized=0)
camctl on              # authorize, and modprobe uvcvideo if it is unloaded
camctl toggle
camctl off 093a:2700   # target by id (or by node: camctl off 3-7)
```

With one camera present, `on`/`off`/`toggle` need no argument. Full reference,
the passwordless-`sudo` rule, and how it handles an already-off camera:
[`scripts/camctl.md`](scripts/camctl.md).

**One subtlety worth stating here**, because it is a property of the mechanism,
not of the tool: deauthorizing a device tears its interfaces down, so a camera
that is *already* OFF exposes no `0e` interface to find it by. A naive
interface-only lister will not show an off camera and cannot turn it back on;
`camctl` covers it with a small state file plus a product-name fallback (the
`vendor:product` and `product` descriptors stay readable while a device is
deauthorized — only the interfaces disappear).

## Safety rules

- Local workstation troubleshooting only; never edit product repos as part of these fixes.
- Re-verify hardware paths (`0000:83:00.0`, interface names) before running — they can change.
- Use `sudo` only for the documented commands; do not broaden scope.

## References / related

- `linux-slack-kde-notifications` — Slack desktop notification click-activation on KDE Plasma.
- `linux-pipewire-audio-ops` — sound-server profiles, where a setting really lives, silent microphones, Bluetooth audio.
- `linux-gpu-output-switch` — what breaks when the display cable moves between GPUs.
- ``slackbridge sessions`` — the Slack session-control toolkit referenced in §1.
