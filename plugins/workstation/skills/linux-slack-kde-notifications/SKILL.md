---
name: linux-slack-kde-notifications
description: Use when Slack desktop notifications stop opening/focusing correctly on Linux with KDE Plasma, especially after a Slack update. Focus on launcher normalization, X11 vs Wayland flags, KDE cache refresh, and popup vs history diagnosis.
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
- Keep `StartupWMClass=Slack` and `MimeType=x-scheme-handler/slack;`.

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
- Final verification command output summary

## References

- `references/slack-kde-notification-recovery.md`
