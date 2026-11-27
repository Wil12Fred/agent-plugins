---
name: linux-slack-kde-notifications
description: Use when Slack desktop notifications on Linux with KDE Plasma do not open or focus Slack when clicked, whether every click fails (launcher or X11 vs Wayland flags, KDE cache) or only some do (Slack swallows the click; the slack-notif-click user service rescues it). Covers popup vs history diagnosis with D-Bus and Slack's own logs. Not for Slack webhooks, the Slack API, or macOS/Windows.
---

# Linux Slack KDE Notifications

## Overview

Use this skill when local Slack notifications stop opening the app or redirecting to the message after a Slack update on KDE Plasma.

This skill is local-environment troubleshooting, not application-code troubleshooting.

## When to Use

Use this skill when one or more of these happen:
- Slack popup notifications appear but clicking them does not open or focus Slack.
- `Notifications History > View` dismisses the notification but does not open Slack.
- Slack updates reintroduce duplicate launchers or breaks `.desktop` associations.
- Slack works only after reinstalling, or only some notification paths work.

Do not use this skill for Slack webhooks, Slack API integrations, or repository code named `slack`.

## Workflow

1. Confirm session and launcher state
- Check whether the desktop session is Wayland or X11.
- Inspect both `~/.local/share/applications/slack.desktop` and `/usr/share/applications/slack.desktop`.
- Check for duplicate `Slack.desktop` vs `slack.desktop` entries.
- **The local override can be missing with no Slack update to blame.** On
  2026-11-25 it was simply gone: the package was installed 2026-08-22 and
  nothing in it writes to `~/.local`. Slack then ran from the system launcher,
  natively on Wayland, and every `View` click was swallowed. Its absence is the
  first thing to check, not a post-update corner case.

2. Normalize the active launcher
- Prefer a single active local override at `~/.local/share/applications/slack.desktop`.
- If Slack notification activation is broken after update, prefer:
  `Exec=/usr/bin/slack --ozone-platform=x11 %U`
- Keep `MimeType=x-scheme-handler/slack;` and set `StartupWMClass` to the real
  window class. Under XWayland that is `slack` in lowercase
  (`xprop` on the window gives `WM_CLASS = "slack", "slack"`), not the `Slack`
  in the system launcher. Slack's `Notify` hint is `desktop-entry: "slack"`, which
  matches `slack.desktop`.

3. Refresh KDE caches
- Run `update-desktop-database ~/.local/share/applications`
- Run `kbuildsycoca6 --noincremental`

4. Kill stale Slack processes
- Fully terminate Slack before retesting:
  `pkill -f '/usr/lib/slack/slac[k]'`
- The running binary is `/usr/lib/slack/slack`, not `/usr/bin/slack` (that is
  a wrapper that execs it). A pattern on `/usr/bin/slack` matches nothing.
- Keep the `[k]` bracket: a plain `pkill -f '/usr/lib/slack/slack'` inside a
  `bash -c` that also contains that string kills its own shell (exit 144).
- Relaunch from the launcher, not from an existing stale process.

5. Verify runtime flags
- After reopening Slack, confirm it is running with:
  `pgrep -af '/usr/lib/slack/slac[k]' | grep -v -- '--type='`
- Expect `/usr/lib/slack/slack --ozone-platform=x11` when this workaround is active.
- Give Slack time to finish loading before judging the fix: on 2026-11-25 the
  first two test clicks right after the restart did nothing, and every click
  after that worked.

6. Distinguish popup vs history behavior
- If popup click works but history does not, capture `org.freedesktop.Notifications` D-Bus traffic.
- Check whether Plasma emits `ActionInvoked` and `ActivationToken` on `View`.
- If Plasma emits them, the failure is downstream in Slack/Electron.
- In the known working fix for this environment, relaunching Slack under X11 restored both popup and history activation.
- Capture with `dbus-monitor --session "interface='org.freedesktop.Notifications'"`.
  A healthy click shows `ActivationToken`, `ActionInvoked "default"` and
  `NotificationClosed` (reason 3) for the same id. A `Notify` with no
  `ActionInvoked` after it means the click never happened or never reached
  Plasma, not that Slack ignored it.
- To produce a test notification, post as a bot (not as the user, whose own
  messages do not notify them) with an `<@USERID>` mention.
- **The popup has no `View` button, by design.** Slack sends one action,
  `default`/`View`; Plasma binds the default action to clicking the popup body
  and only renders it as a button in the history. A missing button in the popup
  is not a symptom.

7. Only some clicks open Slack: rescue the swallowed ones
- Symptom: the launcher and the x11 flag are correct, yet some notifications,
  popup or history, do nothing or only raise the window.
- Diagnose with Slack's own log, `~/.config/Slack/logs/default/browser.log`:
  - each notification logs `Store: NEW_NOTIFICATION`, and each click Slack
    handles logs `Store: CLICK_NOTIFICATION` within ~1 ms of Plasma's
    `ActionInvoked`;
  - a D-Bus `ActionInvoked "default"` with no `CLICK_NOTIFICATION` after it
    means Plasma delivered the click and Slack dropped it.
  - On 2026-11-27 one log held 47 notifications and 1 click.
- Cause: Slack is closed-source Electron. Electron drops a notification's click
  handler while the notification is still listed in Plasma. Its JS
  `Notification` gets garbage-collected, and Slack seems to keep only the newest
  notification per conversation clickable. Another open-source
  Electron client fixed the same bug app-side in Oct 2026 by keeping a reference
  to each live notification. Slack cannot be patched from here, and
  no launcher flag changes it.
  - Keep `--ozone-platform=x11`. Native Wayland swallowed every click on
    2026-11-25.
- Fix: install the `scripts/slack-notif-click` user service; its reference is
  `scripts/slack-notif-click.md`. On a Slack click it raises Slack at once, and
  if Slack logs no `CLICK_NOTIFICATION` within 0.5 s it opens the message's deep
  link itself.
- Deep-link traps, measured on Slack 4.51.191:
  - use `slack://channel/?team=…&id=…&message=…`, because the documented form
    without the `/` is dropped as `Missing command`;
  - pass the link to `/usr/bin/slack` directly, because `xdg-open` on KDE
    rewrites it into the same failure.
- Verified 2026-11-27 on:
  - Arch, kernel 6.18.65-lts, KDE Plasma/KWin 6.8.3 on Wayland, XWayland 24.1.15;
  - `slack-desktop` 4.51.191 (Electron 43.4.0).
  - On these versions, 3 of 3 swallowed clicks were rescued.
  - The service is not needed on macOS or Windows.
  - Re-check whether it is still needed after Slack updates (see "When to
    remove it" in its doc).

## Known Good Fix

For the case documented in this environment, the stable fix was:
- local launcher override in `~/.local/share/applications/slack.desktop`
- `Exec=/usr/bin/slack --ozone-platform=x11 %U`
- regenerate KDE launcher caches
- fully close Slack
- reopen Slack from the launcher

This resolved:
- popup notifications not opening Slack
- `Notifications History > View` not opening Slack

It did not resolve *some* clicks still being swallowed by Slack itself. That
needs §7, the `slack-notif-click` service, on top of the launcher fix.

## Exact File State

Document the current state exactly as follows:
- Local override in `~/.local/share/applications/slack.desktop`
- System launcher in `/usr/share/applications/slack.desktop`
- No `~/.local/share/applications/Slack.desktop`
- No `/usr/share/applications/Slack.desktop`

Current `Exec=` values:
- Local: `Exec=/usr/bin/slack --ozone-platform=x11 %U`
- System: `Exec=/usr/bin/slack --gtk-version=3 -s %U`

Operational rule after Slack updates:
- Do not rename files on every update by default.
- First verify whether the local override `~/.local/share/applications/slack.desktop` still exists.
- Keep the active override lowercase as `slack.desktop`.
- If an uppercase `Slack.desktop` reappears, treat it as suspect and verify whether it creates duplicate launchers or steals associations.
- After any update, restart Slack and confirm runtime flags with `pgrep -af '/usr/lib/slack/slac[k]' | grep -v -- '--type='`.

## Safety Rules

- Treat this as local workstation troubleshooting.
- Do not edit unrelated project repositories.
- Do not assume `Slack.desktop` and `slack.desktop` are equivalent.
- Do not trust a running Slack process after changing launcher flags; always restart it.

## Output Requirements

Include:
- Current session type (`Wayland` or `X11`)
- Active launcher path and exact `Exec=`
- Whether duplicate desktop entries exist
- Whether popup click works
- Whether history `View` works
- Whether `slack-notif-click` is active (`systemctl --user is-active slack-notif-click`)
  and how many rescues its log shows
- Final verification command output summary

## References

- `references/slack-kde-notification-recovery.md`
- `scripts/slack-notif-click.md`: the rescue service, its install, and when to remove it
