---
name: linux-workstation-ops
description: Use for local Arch Linux / KDE workstation operational issues on this machine — Wi-Fi/network recovery after a system update (MediaTek mt7925), pacman update hiccups, and the known gap where agent background-task notifications do not reach the Slack session system. Local-environment ops, not product/application code.
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

## Safety rules

- Local workstation troubleshooting only; never edit product repos as part of these fixes.
- Re-verify hardware paths (`0000:83:00.0`, interface names) before running — they can change.
- Use `sudo` only for the documented commands; do not broaden scope.

## References / related

- `linux-slack-kde-notifications` — Slack desktop notification click-activation on KDE Plasma.
- ``slackbridge sessions`` — the Slack session-control toolkit referenced in §1.
