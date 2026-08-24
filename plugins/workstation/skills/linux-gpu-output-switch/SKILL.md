---
name: linux-gpu-output-switch
description: Use when the monitor cable moves between GPUs on this Arch/KDE machine — from the discrete card to the board's DisplayPort or back — and the login screen, the desktop or the monitor's sound stops working. Covers a display server that picks its GPU from a config constant instead of from the cable, a display manager that refuses to start when a driver module is missing, and the monitor's audio landing on a different sound card where analog and digital output are mutually exclusive. Do not use for a GPU driver that will not load against the running kernel, or for audio faults with the cable unchanged — those are `linux-workstation-ops`.
---

# Moving the display output between GPUs

## Overview

On a machine with an integrated GPU and a discrete card, the monitor cable is not
a detail the rest of the stack discovers on its own. Three subsystems keep their
own idea of where the display is, and each fails differently when it is wrong:

| Subsystem | What it believes | How it fails |
|---|---|---|
| Display server | a constant in `xorg.conf.d` | login screen renders on a GPU with nothing attached — *"no signal"* |
| Display manager | that the discrete driver loaded | refuses to start at all, so there is no desktop to debug from |
| Sound server | that the monitor's audio is on the card it was on | the monitor disappears from the output list |

None of the three is reported as an error. The desktop simply does not appear, or
the monitor is silent, and every log says the service started fine. Work through
them in order — the first one blocks your ability to see the other two.

## Step 0 — settle where the cable actually is

Everything below depends on this answer, and it is the one thing that cannot be
guessed. The kernel already knows:

```bash
for c in /sys/class/drm/card*-*; do
    [ "$(cat "$c/status" 2>/dev/null)" = connected ] || continue
    n=${c##*/}; card=${n%%-*}
    printf '%-22s driver=%-8s %s\n' "$n" \
        "$(basename "$(readlink -f "/sys/class/drm/$card/device/driver")")" \
        "$(head -1 "$c/modes")"
done
```

One line per connected output, naming the kernel driver that owns it. A cable in
the board's DisplayPort reports the integrated driver (`i915` on this machine); a
cable in the discrete card reports that card's driver. If nothing prints, no
monitor is detected at all and this skill does not apply.

Record the answer. Sections 1 and 4 both need it.

## 1. The display server follows its config, not the cable

**Symptom.** The display manager is `active (running)`, the greeter logs that it
started, and the monitor reports no signal.

**What it looks like in `/var/log/Xorg.0.log`:**

```
(**) OutputClass "…" setting /dev/dri/card0 as PrimaryGPU
(--) …(GPU-0): DFP-0: disconnected          ← every output on that card
(--) …(0): No enabled display devices found; starting anyway because
(--) …(0):     AllowEmptyInitialConfiguration is enabled
(II) …(0): Virtual screen size determined to be 640 x 480
```

with the greeter drawing into it:

```
…-greeter…: Adding view for ":0.0" QRect(0,0 0x0)
```

`Option "PrimaryGPU" "yes"` forces the server onto that card. `AllowEmptyInitial‑
Configuration` then lets it start with **zero** outputs instead of failing, so
nothing anywhere reports an error — the session is running, on a screen no cable
can reach.

**Check it in one line:**

```bash
grep -iE 'PrimaryGPU|Virtual screen|No enabled display' /var/log/Xorg.0.log
```

**Fix.** Remove the `PrimaryGPU` option so the server picks the GPU that owns the
connected output:

```bash
sudo cp -a /etc/X11/xorg.conf.d/<file>.conf{,.bak}
sudo sed -i '/Option[[:space:]]*"PrimaryGPU"/d' /etc/X11/xorg.conf.d/<file>.conf
sudo systemctl restart <display-manager>
```

Section 3 replaces this manual edit with a rule that decides per boot.

### The duplicate that hides in plain sight

A driver package ships its own `OutputClass` snippet in
`/usr/share/X11/xorg.conf.d/`. A local copy of the **same filename** under
`/etc/X11/xorg.conf.d/` does not always replace it — both get read, and the log
shows the section applied twice:

```
(**) OutputClass "…" ModulePath extended to "…,…"
(**) OutputClass "…" ModulePath extended to "…,…,…,…"      ← twice
(II) Applying OutputClass "…" to /dev/dri/card0
(II) Applying OutputClass "…" to /dev/dri/card0
```

If the local copy exists only to add one option, delete it and add the option in
a **separate, higher-numbered** file. Then the vendor snippet keeps receiving
package updates, and there is exactly one place holding the local decision.

## 2. The display manager must survive a driver that is not there

A drop-in that preloads the discrete driver before the display manager starts is
common, and it is written wrong nearly every time:

```ini
[Service]
ExecStartPre=/usr/bin/modprobe <discrete-driver>     # ← no leading dash
```

A failing `ExecStartPre` **aborts the unit**. So a driver that does not match the
running kernel — after an upgrade, or when a rebuild has not landed yet — turns
into no graphical session at all:

```
modprobe: FATAL: Module … not found in directory /lib/modules/<running-kernel>
…service: Control process exited, code=exited, status=1/FAILURE
…service: Start request repeated too quickly.
…service: Failed with result 'start-limit-hit'.
```

**Fix — one character per line.** The `-` prefix makes systemd ignore the failure:

```ini
[Service]
# "-" so a failure does not abort the unit: if the discrete driver does not
# match the running kernel, the session still starts on the integrated GPU
# instead of leaving the machine with no desktop.
ExecStartPre=-/usr/bin/modprobe <discrete-driver>
ExecStartPre=-/usr/bin/modprobe <discrete-driver>_modeset
ExecStartPre=-/usr/bin/modprobe <discrete-driver>_drm
ExecStartPre=-/usr/bin/udevadm settle --timeout=10
```

Then `sudo systemctl daemon-reload && sudo systemctl reset-failed <unit>`.

**The rule worth keeping:** on a machine with an integrated GPU, never make the
login screen conditional on the discrete driver. The integrated GPU can always
draw a login screen, and a machine that reaches one is a machine you can repair
without a second computer.

## 3. Deciding the primary GPU from the cable, per boot

The manual edit in section 1 is wrong again the moment the cable moves back. This
reads the same source as Step 0 and writes the option only when it is true.

`/usr/local/bin/gpu-primary-autodetect`:

```bash
#!/bin/bash
# Chooses the primary GPU for the display server from where the cable actually
# is. Run as ExecStartPre of the display manager, after the driver modprobes.
# Never exits non-zero: if anything is unclear, it leaves the config alone.
CONF=/etc/X11/xorg.conf.d/11-<driver>-primary.conf
DRIVER=<discrete-kernel-driver>          # as it appears under /sys/…/device/driver
has_display=0 found=0

for conn in /sys/class/drm/card*-*; do
    [[ -r "$conn/status" ]] || continue
    [[ "$(<"$conn/status")" == connected ]] || continue
    found=1
    card="${conn##*/}"; card="${card%%-*}"
    drv="$(basename "$(readlink -f "/sys/class/drm/$card/device/driver" 2>/dev/null)" 2>/dev/null)"
    echo "gpu-primary-autodetect: ${conn##*/} connected (card=$card driver=$drv)"
    [[ "$drv" == "$DRIVER" ]] && has_display=1
done

if [[ $found -eq 0 ]]; then
    echo "gpu-primary-autodetect: no connected output yet; leaving config untouched"
    exit 0
fi

if [[ $has_display -eq 1 ]]; then
    [[ -f $CONF ]] || cat > "$CONF" <<CONF_EOF
# Generated by gpu-primary-autodetect. Do not edit: rewritten or removed on
# every start of the display manager, according to where the cable is.
Section "OutputClass"
    Identifier "discrete-primary"
    MatchDriver "<driver>-drm"
    Driver "<driver>"
    Option "PrimaryGPU" "yes"
EndSection
CONF_EOF
    echo "gpu-primary-autodetect: display on the discrete card -> PrimaryGPU ON"
else
    rm -f "$CONF"
    echo "gpu-primary-autodetect: display on the integrated GPU -> PrimaryGPU OFF"
fi
exit 0
```

Wired in after the modprobes, and with the same `-` prefix as everything else:

```ini
ExecStartPre=-/usr/bin/udevadm settle --timeout=10
ExecStartPre=-/usr/local/bin/gpu-primary-autodetect
```

It writes a **separate** file rather than editing the vendor snippet, which is
what keeps section 1's duplicate from coming back.

**Its limit, stated rather than discovered:** it runs when the display manager
starts, not on hotplug. Moving the cable in a live session needs a restart of the
display manager. Detecting it hot would need a udev hook and would restart the
display server anyway, so it buys nothing.

## 4. The monitor's audio moves to a different sound card

Easy to miss, because it is not a display problem. **The monitor's sound travels
over the same cable**, so it belongs to whichever card drives the port. Move the
cable to the board and the audio leaves the discrete card's HDA device for the
on-board codec — a different ALSA card, with different rules.

### First: is the monitor seen at all?

The codec publishes what the monitor told it. This is hardware truth, before any
sound server is involved:

```bash
grep -l 'monitor_present.*1' /proc/asound/card*/eld* 2>/dev/null | \
    xargs -r grep -E 'monitor_name|connection_type|eld_valid|sad0_'
```

A valid entry names the monitor and the link type, and lists the formats it
accepts. If this is empty the sound server can do nothing — that is a cable or
monitor problem.

### The trap: on the on-board codec the outputs are mutually exclusive

The discrete card's HDA device is digital-only, so its outputs never compete. The
on-board codec is not: its profiles pair **one** output with one input, and there
is no profile combining the analog jack with the digital output. So after the
move, the monitor is not missing — it is in a profile that is not active:

```bash
pactl list cards | grep -E 'Name: alsa_card|Active Profile'
pactl list cards | grep -E 'hdmi-stereo.*available: yes'
```

Switch to it, and **keep the `+input:…` half** or the microphone disappears with
the jack:

```bash
pactl set-card-profile alsa_card.pci-0000_XX_XX.X output:hdmi-stereo+input:analog-stereo
```

### Why the choice does not survive a reboot

The sound server saves the chosen profile and restores it. Something overriding
that is almost always this:

```
api.acp.auto-profile = true      # in some local drop-in
api.acp.auto-profile = "false"   # the stock default
```

With it on, the card re-picks by **priority** on every start, and the analog
duplex profile outranks the digital one — so the saved choice loses every boot,
and the symptom reads as "it forgets my setting". Delete the drop-in that sets
it; the default already does the right thing. Confirm the save works:

```bash
pactl set-card-profile <card> <profile>
cat ~/.local/state/wireplumber/default-profile
```

### If both outputs must be listed at once

Only by turning the automatic profiling off for that card, so every PCM becomes
its own device:

```
monitor.alsa.rules = [
  {
    matches = [ { device.name = "alsa_card.pci-0000_XX_XX.X" } ]
    actions = { update-props = { api.alsa.use-acp = false } }
  }
]
```

**Price, which is not optional and is easy to discover too late:** there is then
no port management on that card *in either direction*. Three consequences:

- **No jack detection.** Plugging headphones no longer switches anything.
- **The input route is yours to keep.** Nothing reapplies it, so the capture
  source can sit on an empty front-panel jack while the sound server reports the
  rear one. Check `amixer -c <n> sget 'Input Source'` against the port the sound
  server claims — a mismatch here is silent, and it looks exactly like a dead
  microphone.
- **Levels live in hardware.** Raw nodes use a *software* volume that is not
  restored, so it resets on every restart. Put the level in the hardware control
  and persist the lot:

  ```bash
  amixer -c <n> sset 'Capture' 90%
  amixer -c <n> sset 'Input Source' 'Rear Mic'
  sudo alsactl store          # or the boot-time restore undoes all of it
  ```

### Rules pinned to the old card stop matching, silently

Sound-server rules match on node names, and a node name contains the **PCI
address of its card**. Every rule written for the monitor on the discrete card
matches nothing once the cable moves — no warning, no log line, the tuning just
stops applying. Match both cards, so the rule survives the cable:

```
matches = [
  { node.name = "~alsa_output\\.pci-0000_AA_AA\\.A\\..*" }   # discrete card
  { node.name = "~alsa_output\\.pci-0000_BB_BB\\.B\\..*" }   # on-board codec
]
```

## 5. After moving the cable — the short version

1. `Step 0` — confirm which driver owns the connected output.
2. `grep -iE 'PrimaryGPU|Virtual screen' /var/log/Xorg.0.log` — if the primary GPU
   is not the one from step 0, fix section 1 or install section 3.
3. Confirm every `ExecStartPre` in the display manager's drop-in has a `-`.
4. `pactl list cards | grep -E 'Name: alsa_card|Active Profile'` — select the
   profile for the card that now carries the monitor.
5. Update any sound-server rule that names the old card's PCI address.
6. If the card runs without automatic profiling: verify `Input Source` against
   the reported port, then `sudo alsactl store`.

## Safety rules

- Local workstation ops only. Never edit a project repository as part of this.
- **Re-verify every hardware path before running it.** Card numbers, PCI
  addresses and connector names change between machines and across boots; the
  ones in a document are examples, not values.
- Back up before editing anything under `/etc`, and keep the backup next to the
  original with a dated suffix.
- Leave a way back in: the `-` prefix of section 2 exists so a mistake in this
  procedure still ends at a login screen instead of a black one.

## References / related

- `linux-pipewire-audio-ops` — the sound side in general: profiles versus raw
  PCM nodes, which layer holds a setting, silent microphones, Bluetooth audio.
- `linux-workstation-ops` — network recovery after a system update, pacman,
  wedged devices, and other operations on this machine.
- `linux-slack-kde-notifications` — desktop notification activation on KDE.
