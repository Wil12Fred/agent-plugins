# `camctl`

List video-capture devices (webcams) and enable/disable **one specific device**
via the USB `authorized` flag — without unloading `uvcvideo` and so without
affecting any other camera. The reasoning behind the mechanism is in
`SKILL.md` §7; this is the tool's own reference.

## Usage

```bash
camctl                 # list: ON/OFF, node, id, name, and who holds it
camctl off             # disable (frees the holder, e.g. Chrome, then authorized=0)
camctl on              # enable (authorized=1, and modprobe uvcvideo if unloaded)
camctl toggle
camctl off 093a:2700   # target by id  (or by node: camctl off 3-7)
```

With exactly one camera present, `on`/`off`/`toggle` need no argument.

## Install

Symlink it onto your `PATH` so the running tool and the repository are one file:

```bash
ln -sfn ".../skills/linux-workstation-ops/scripts/camctl" ~/.local/bin/camctl
```

## How it works

- **Enumerates by USB Video class (`0e`)**, not by `/dev/video*`, so it lists a
  camera even when its driver is unloaded.
- **`off`** frees any process holding the device (`fuser -k` on its `/dev/videoN`),
  then writes `authorized=0` on the USB device node. **`on`** loads `uvcvideo` if
  needed and writes `authorized=1`.
- **Resolves the camera by `vendor:product` or node**, so a node path that renumbers
  across a replug (observed: `3-9` → `3-7`) does not point the tool at the wrong
  device.

### The one subtlety: a camera that is already OFF has no interfaces

Deauthorizing a USB device tears its interfaces down, so an *already-off* camera
exposes no `0e` interface to recognise it by — a naive interface-only lister would
not show it, and could not turn it back on. `camctl` covers this two ways:

1. **A state file** at `${XDG_CACHE_HOME:-~/.cache}/camctl/off` records the
   `vendor:product` of every camera it turned off (keyed by id, so it survives a
   node renumber), and clears it on `on`.
2. **A product-name fallback**: a deauthorized device whose `product` string looks
   like a camera (`/cam/i`) is still listed, so a camera someone deauthorized by
   hand also reappears.

The `vendor:product` and `product` descriptors stay readable while a device is
deauthorized; only the interfaces go away.

## Privilege

Three actions need root: writing `authorized`, `modprobe uvcvideo`, and
`fuser -k` on the video nodes. To make it a passwordless one-click (for a
`.desktop` launcher or a KDE shortcut running `camctl toggle`), narrow a sudoers
rule to exactly those and nothing else:

```bash
# /etc/sudoers.d/camctl  (chmod 440; replace <user>)
<user> ALL=(root) NOPASSWD: /usr/bin/tee /sys/bus/usb/devices/*/authorized, /usr/bin/modprobe uvcvideo, /usr/bin/fuser -k /dev/video*
```

## Limits

- One physical camera occupies one USB port; `off` deauthorizes that device but does
  not free the port for another device until it is physically unplugged (at which
  point the deauthorization is gone — it travels with the device, not the port).
- The tool does not change resolution or framerate. A feed that is slow because the
  camera only offers uncompressed `YUYV` at `640x480` is a hardware limit; inspect
  with `v4l2-ctl -d /dev/video0 --list-formats-ext`.
