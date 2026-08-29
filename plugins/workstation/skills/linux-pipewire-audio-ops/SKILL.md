---
name: linux-pipewire-audio-ops
description: Use when audio on this Arch/KDE machine loses a device, forgets a setting between reboots, pauses on its own, or records silence — choosing between automatic card profiling and raw PCM nodes, finding which of three layers actually holds a setting, and a Bluetooth speaker that connects without any audio endpoint — including how to keep that one from recurring. Do not use when the monitor's sound moved because the display cable changed GPU (that is `linux-gpu-output-switch`), nor when a capture device returns I/O errors at the driver level with no samples at all (that is the wedged-device section of `linux-workstation-ops`).
---

# Sound server operations

## Overview

Three faults account for nearly everything that goes wrong with audio here, and
each has a different owner:

| Symptom | Owner | Section |
|---|---|---|
| a device is missing from the list | the card's profile, or Bluetooth | 1, 5 |
| a setting is forgotten on every boot | the wrong layer holds it | 2, 3 |
| the microphone records silence | the mixer route, not the sound server | 4 |

The recurring mistake is fixing the wrong layer. A profile chosen in the GUI and
a level set with the sound server's tools live in different places, persist by
different mechanisms, and one of them is discarded on every start.

## 1. Two ways to run one sound card

An analog codec is presented in one of two modes, and the choice is a real
trade-off rather than a better/worse:

| | Automatic profiling (default) | Raw PCM nodes (`api.alsa.use-acp = false`) |
|---|---|---|
| Devices exposed | one profile at a time | every PCM at once |
| Analog jack + digital output together | **impossible** — profiles are exclusive | yes, both listed, switch by clicking |
| Jack detection (headphones auto-switch) | yes | **none** |
| Input route and levels | managed for you | **yours to maintain** (see §2) |
| Volume | hardware mixer, restored | software, **not** restored |

Switching a card to raw nodes:

```
monitor.alsa.rules = [
  {
    matches = [ { device.name = "alsa_card.pci-0000_XX_XX.X" } ]
    actions = { update-props = { api.alsa.use-acp = false } }
  }
]
```

in a file under `~/.config/wireplumber/wireplumber.conf.d/`. Expect the raw nodes
to be named `…playback.<n>.<m>` and to carry unhelpful descriptions; both are
fixable with `node.description`, and unused PCMs can be hidden with
`node.disabled = true`. Confirm the names first — they are the match keys:

```bash
pactl list short sinks
pactl list sinks | grep -E 'Name:|Description:|api.alsa.pcm.(id|name)'
```

**Pick raw nodes only when listing two outputs at once is worth losing jack
detection.** For everything else, automatic profiling is less to maintain.

## 2. Which of three layers holds a setting

This is the question to answer before changing anything, because each layer
persists differently and only one of them is right for a given setting:

| Layer | Lives in | Holds | Survives by |
|---|---|---|---|
| Sound-server settings | `~/.local/state/wireplumber/sm-settings` | policy toggles | `wpctl settings --save` |
| Sound-server state | `~/.local/state/wireplumber/default-{nodes,profile,routes}` | chosen device, profile, per-route volume | written on change |
| ALSA mixer | `/var/lib/alsa/asound.state` | input source, capture level, boost | `alsactl store`, restored at boot |

**With automatic profiling on**, the sound server manages routes and volumes, and
its own state files are enough.

**With raw PCM nodes**, nothing reapplies the mixer, so the input route and the
levels exist only in the third layer:

```bash
amixer -c <n> sset 'Input Source' 'Rear Mic'
amixer -c <n> sset 'Capture' 90%
sudo alsactl store          # without this, boot-time restore undoes all of it
```

And because raw-node volume is a *software* attenuation that is not restored,
setting a level with the sound server's own tool is the wrong layer: it resets on
every restart. Put the level in the hardware control and leave the software
volume at 1.0.

## 3. Three settings that silently override a choice

Inspect all of them, with their values and whether they were saved:

```bash
wpctl settings
```

A `[Saved: …]` marker means someone wrote that value deliberately — it is an
override, not a default, and it is the first place to look when the machine
"forgets" something.

- **`api.acp.auto-profile`** — stock default **false**. A drop-in setting it
  `true` makes the card re-pick a profile *by priority* on every start, so a
  saved choice loses every boot. Analog duplex outranks the digital profiles, so
  the symptom is always "it goes back to the analog jack".
- **`device.restore-profile`** — stock default **true**. A saved `false`
  disables profile restoration entirely; the choice is written and never read.
  These two together produce the same symptom for two unrelated reasons, and
  fixing one leaves the other.
- **`linking.pause-playback`** — stock default **true**: *pause media players if
  their target sink is removed*. This is the "the speaker disconnected and I had
  to press play again" report. It exists so that unplugging headphones does not
  blast audio through the speakers; setting it `false` makes playback follow to
  the fallback device instead, and accepts that risk.

```bash
wpctl settings --save <id> <value>
```

## 4. A microphone that records silence

Three unrelated faults produce identical silence, in increasing order of depth.
Do not skip ahead — each check is cheap and rules out a whole layer.

**a. The mixer is capturing from a different jack than the one reported.** The
sound server can report the rear port while the codec captures the front one. It
is silent, and nothing logs it:

```bash
amixer -c <n> sget 'Input Source'      # what the codec actually captures
pactl list sources | grep -E 'Name:|Active Port:'   # what the server believes
```

Re-applying the port makes the server rewrite the mixer path:

```bash
pactl set-source-port <source> <other-port>   # force a change…
pactl set-source-port <source> <wanted-port>  # …then the one you want
```

**b. The boost is at zero.** Separate from the capture level, and easy to miss:

```bash
amixer -c <n> sget 'Rear Mic Boost'
```

**c. The device delivers no samples at all.** If the route and levels are right
and it is still silent, stop treating it as configuration and measure:

```bash
cat /proc/asound/card<n>/pcm0c/sub0/status
```

`state: RUNNING` with `hw_ptr: 0` and `avail: 0` means the hardware is not
transferring — that is a wedged controller, not a setting. See the wedged-device
section of `linux-workstation-ops`.

## 5. A Bluetooth speaker that connects but produces no device

The device pairs, connects, shows as connected — and the sound server never
publishes a sink. Nothing is broken in the speaker: **a Bluetooth audio
connection completes in two independent layers, and only the first one is what
`bluetoothctl` reports.**

| Layer | What proves it | Reported by |
|---|---|---|
| the radio link | `Connected: yes` | `bluetoothctl info` |
| the audio transport | a `sep*/fd*` endpoint | `busctl tree org.bluez` |

Without the second, there is nothing for the sound server to build a card from.
So `bluetoothctl` saying "connected" while the sound panel shows no speaker is
not a contradiction — the two are looking at different layers, and both are
telling the truth.

### Reading which layer failed

Current state comes from the object tree:

```bash
busctl tree org.bluez | grep -A4 dev_XX_XX_XX_XX_XX_XX
```

A working audio connection has stream endpoints:

```
dev_…
├── avrcp/player0     ← remote control only
├── sep1/fd0          ← audio endpoint: this is what must exist
└── sep2
```

**Only `avrcp` means the connection was negotiated without audio.**

After the fact the tree is useless — it shows now, not what happened. The
Bluetooth daemon's journal is what says whether the transport ever came up, and
the one line that settles it is the file descriptor going ready:

```bash
journalctl -b -o short-iso | grep -iE "bluetoothd|dev_XX_XX_XX_XX_XX_XX"
```

```
# failed: the link came up, the transport never did
…  Unable to get Hands-Free Voice gateway SDP record: Host is down
…  a2dp.c:load_remote_sep() Unable to load LastUsed: rseid 1 not found
   (and no fd line at all)

# succeeded, moments later, same device
…  No SDP records found for Hands-Free Voice gateway     ← benign: a speaker has no HFP
…  /org/bluez/hci0/dev_…/sep1/fd0: fd(30) ready          ← THE line to grep for
```

Do not read the Hands-Free complaints as the fault. A speaker legitimately has
no headset profile, and both a working and a failing connection log something
about it. **`fd(N) ready` is the only signature that separates them.**

### The immediate fix: renegotiate

The sound server registers the audio endpoints with the Bluetooth daemon; a
device that connects while those endpoints are not registered — just after the
sound server restarted, or racing it at login — completes with the control
profile alone. There is nothing to add and no event to wait for:

```bash
bluetoothctl disconnect XX:XX:XX:XX:XX:XX
sleep 3
bluetoothctl connect XX:XX:XX:XX:XX:XX
pactl list short cards | grep bluez     # a bluez_card must now exist
```

### Why it keeps happening, and what does not fix it

This is a startup race between the sound server and the Bluetooth daemon,
reported upstream as **[bluez#1545](https://github.com/bluez/bluez/issues/1545)
and still open** — no patch, no version to upgrade to, and the only resolution
the issue documents is manual intervention. So renegotiating is not a
workaround for want of a better one; it is the remedy that exists.

Two dead ends worth not re-walking:

- **A first-connection A2DP failure was also a real bluez bug — 5.80, fixed in
  5.82.** Anything newer has that one already. Check the installed version
  before believing a forum thread that matches the symptom.
- **The saved state is usually innocent.** A `bluez5.profile = off` property on
  the device is an initial value, not the active profile; confirm against
  `pactl list cards` (`Active Profile`) and the sound server's `default-routes`
  before deleting any state file.

### Making it not recur

Two layers, because the declarative half narrows the race but cannot close it.

**Narrow the window** — have the sound server connect the profile itself rather
than wait for the negotiation to come out right:

```
# ~/.config/wireplumber/wireplumber.conf.d/51-bluetooth-autoconnect.conf
monitor.bluez.rules = [
  {
    matches = [ { device.name = "~bluez_card.*" } ]
    actions = { update-props = { bluez5.auto-connect = [ a2dp_sink ] } }
  }
]
```

**Catch what still slips through** — a small user service that renegotiates when
a connected audio device has no card. Three constraints make it safe rather than
a new source of faults:

- **Act only on devices advertising `Audio Sink`.** A Bluetooth mouse or
  keyboard is connected and correctly has no card; a guard that does not filter
  will disconnect input devices in a loop.
- **A cooldown between attempts per device**, so a genuinely mute device is not
  hammered.
- **A retry ceiling**, after which it gives up and leaves the state readable.

```bash
is_audio_sink() { bluetoothctl info "$1" | grep -q "Audio Sink"; }
has_card()      { pactl list short cards | grep -q "bluez_card.${1//:/_}"; }
```

### Proving the guard, which is the part that gets skipped

A guard that has never fired is not evidence of anything. This failure is
**reproducible on demand** — restarting the sound server drops the card of an
already-connected device, which is the exact condition:

```bash
systemctl --user restart wireplumber
# watch: the card disappears, the guard renegotiates, the card returns (~12s)
```

Warn whoever is listening first; it interrupts audio for those seconds.

Related: `bluetooth.autoswitch-to-headset-profile` trades stereo quality for a
microphone when something records. If music degrades the moment a call starts,
that is this setting working as designed, not a fault.

## 6. Order to work in

1. `wpctl status` — is the device listed at all? If not: §1 (profile) or §5
   (Bluetooth). For Bluetooth, do not stop at `Connected: yes` — grep the
   daemon's journal for `fd(N) ready`, which is the only line that proves the
   audio transport came up (§5).
2. `wpctl settings | grep Saved` — any override explaining "it forgets"? §3.
3. For a silent input: `amixer` route, then boost, then `/proc/asound/…/status`. §4.
4. Before declaring it fixed, restart the sound server and check the setting held:
   ```bash
   systemctl --user restart wireplumber
   ```
   A setting that survives a restart is not yet proven to survive a boot if it
   lives in the ALSA layer — that needs `alsactl store` as well. The same
   restart is how a Bluetooth reconnect guard is proven to fire at all (§5).

## Safety rules

- Local workstation ops only; never edit a project repository as part of this.
- Card indices and PCI addresses differ between machines and can change across
  boots. Read them, never copy them from a document.
- Back up before replacing a configuration directory, and keep the sound-server
  **state** directory in the backup too — the volumes and route choices live
  there, and deleting it silently resets levels people had tuned.
- Restarting the sound server interrupts audio for a moment. Harmless, but say so
  before doing it while someone is listening or in a call.

## References / related

- `linux-gpu-output-switch` — when the monitor's audio moves to another card
  because the display cable changed GPU.
- `linux-workstation-ops` — wedged devices, package/kernel skew, and the rest of
  this machine's operations.
- [bluez#1545](https://github.com/bluez/bluez/issues/1545) — the open upstream
  race between the sound server and the Bluetooth daemon behind §5. Check
  whether it has closed before assuming renegotiation is still required.
- [WirePlumber — Bluetooth configuration](https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/bluetooth.html)
  — the authority for `bluez5.*` properties, including `bluez5.auto-connect`.
