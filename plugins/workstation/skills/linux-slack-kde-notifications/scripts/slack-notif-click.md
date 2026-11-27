# `slack-notif-click`

A user service that makes every click on a Slack desktop notification land.
KDE Plasma delivers the click, but sometimes Slack has dropped the handler and
nothing happens. When that happens, this service opens the message itself. Why
Slack drops the click is in `SKILL.md` §7. This file is the tool's own
reference.

## Verified against

Measured on 2026-11-27 with 3 of 3 swallowed clicks rescued, from a popup, from
a thread reply and from a 13-minute-old history entry. It ran on:

| Component | Version |
|---|---|
| OS | Arch Linux, kernel 6.18.65-1-lts |
| Desktop | KDE Plasma 6.8.3, KWin 6.8.3, **Wayland** session |
| XWayland | 24.1.15 |
| Slack | `slack-desktop` 4.51.191 (AUR), Electron 43.4.0 / Chromium 150 |
| Slack launch flag | `--ozone-platform=x11` (required, see below) |
| Tools | Python 3.14.10 (stdlib only), systemd 262.2 (`busctl`), wmctrl 1.07 |

It is Linux-only and KDE-only. macOS and Windows do not have this bug. GNOME
behaves differently: Mutter also needs the activation token applied, which this
script does not do.

## What it does

1. **Pairs every Slack notification with its message.** On D-Bus it records
   Slack's `Notify` call and the id Plasma returns for it. From Slack's own log
   (`~/.config/Slack/logs/default/browser.log`) it reads the
   `Store: NEW_NOTIFICATION {json}` block, which carries `teamId`, `channel`,
   `msg` and `thread_ts`. Plasma's id and the log entry are paired first in,
   first out, when they arrive within 3 s of each other.
2. **Raises Slack at once on a click.** On `ActionInvoked(id, "default")` for a
   Slack id it runs `wmctrl -x -a slack.slack` (about 3 ms). That is the same
   signal for a click on the popup body and for `View` in the history.
3. **Routes to the message if Slack did not.** After `GRACE` seconds (0.5 by
   default) it checks for `Store: CLICK_NOTIFICATION` with that notification's
   id. A handled click appears in the log within ~1 ms. If it is absent, the
   service runs
   `/usr/bin/slack 'slack://channel/?team=…&id=…&message=…[&thread_ts=…]'`.
   Slack's single-instance handoff takes ~0.17 s.

## Two deep-link traps, both measured

- **The slash before `?` is required.** `slack://channel?team=…` is the form
  documented everywhere, and Slack 4.51 drops it with
  `[DEEP-LINK] Missing command in payload, skipping deep link` (`"cmd":""`).
  `slack://channel/?team=…` parses as `"cmd":"channel"` and navigates.
- **Do not go through `xdg-open`.** On KDE it rewrites the URL, so even the
  slash form arrives with `"cmd":""`. Pass the URL straight to `/usr/bin/slack`.

Both show up in `~/.config/Slack/logs/default/webapp-console.log` under
`[DEEP-LINK]`.

## Install

```bash
S=.../skills/linux-slack-kde-notifications/scripts
ln -sfn "$S/slack-notif-click" ~/.local/bin/slack-notif-click
cp "$S/slack-notif-click.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now slack-notif-click
```

Requirements:
- Slack must already run with `--ozone-platform=x11`, from the launcher
  override in `SKILL.md` §2.
- `wmctrl` must be installed (`pacman -S wmctrl`).
- The user manager needs `DISPLAY` and `WAYLAND_DISPLAY` set. Plasma imports
  them; check with `systemctl --user show-environment`.

Uninstall: `systemctl --user disable --now slack-notif-click`, then remove the
symlink and the unit.

## Reading its log

```bash
journalctl --user -u slack-notif-click -f -o cat
```

| Line | Meaning |
|---|---|
| `paired plasma#N -> <slack id>` | the notification is tracked and can be rescued |
| `slack handled click <slack id>` | Slack worked on its own, so the service did nothing |
| `slack ignored click on plasma#N; opening slack://…` | a rescue happened |
| `… untracked plasma#N; raising Slack` | no log pairing, so it only raised the window |

## Limits

- Notifications that arrived before the service started, or before it was
  restarted, are not tracked. A click on one of those does nothing extra.
- It depends on Slack's internal log strings and their JSON shape. If a Slack
  update changes them, pairing stops: `paired` lines no longer appear.
- Slack's own load time for the channel or thread after the link arrives is
  outside its control.
- Environment knobs: `SLACK_NOTIF_GRACE` (seconds) and `SLACK_NOTIF_DRY_RUN=1`
  (log only, open nothing).

## When to remove it

Remove it when Slack handles every click itself. Run the service for a day and
grep its log:

```bash
journalctl --user -u slack-notif-click --since today -o cat | grep -c 'ignored click'
```

Zero rescues over many clicks, after a Slack or Electron update, means the
upstream bug is fixed and the service can go. Record the version that fixed it
in `SKILL.md`.
