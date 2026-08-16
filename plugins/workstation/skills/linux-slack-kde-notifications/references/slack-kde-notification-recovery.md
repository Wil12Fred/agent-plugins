# Slack KDE Notification Recovery

## Known Symptom Pattern

- Slack updates and notification click behavior regresses.
- Popup notifications may stop opening Slack.
- `Notifications History > View` may dismiss the entry without opening Slack.
- A stale launcher or stale Slack process can preserve the broken behavior even after editing `.desktop` files.

## Exact Current State

Current launcher files on this machine:

```text
~/.local/share/applications/slack.desktop
/usr/share/applications/slack.desktop
```

Files that should currently not exist:

```text
~/.local/share/applications/Slack.desktop
/usr/share/applications/Slack.desktop
```

Current `Exec=` lines:

```text
~/.local/share/applications/slack.desktop
Exec=/usr/bin/slack --ozone-platform=x11 %U

/usr/share/applications/slack.desktop
Exec=/usr/bin/slack --gtk-version=3 -s %U
```

Interpretation:
- The lowercase local file is the active override and is the one that works.
- The lowercase system file is the package-provided launcher.
- The uppercase variants are not part of the desired final state.

## Commands

Inspect launchers:

```bash
sed -n '1,220p' ~/.local/share/applications/slack.desktop
sed -n '1,220p' /usr/share/applications/slack.desktop
find ~/.local/share/applications /usr/share/applications -maxdepth 1 -iname '*slack*.desktop' | sort
```

Recommended local override:

```ini
[Desktop Entry]
Name=Slack
StartupWMClass=Slack
Comment=Slack Desktop
GenericName=Slack Client for Linux
Exec=/usr/bin/slack --ozone-platform=x11 %U
Icon=slack
Type=Application
StartupNotify=true
Categories=GNOME;GTK;Network;InstantMessaging;
MimeType=x-scheme-handler/slack;
```

Refresh KDE caches:

```bash
update-desktop-database ~/.local/share/applications
kbuildsycoca6 --noincremental
```

Restart Slack cleanly:

```bash
pkill -f '^/usr/bin/slack'
gtk-launch slack
```

Verify active flags:

```bash
pgrep -af '/usr/bin/slack|slack$'
```

Expected result:

```text
/usr/bin/slack --ozone-platform=x11
```

## Post-Update Rule

After a Slack update, do not assume you need to rename files immediately.

Check in this order:

1. Does `~/.local/share/applications/slack.desktop` still exist?
2. Does it still contain `Exec=/usr/bin/slack --ozone-platform=x11 %U`?
3. Is Slack currently running with `--ozone-platform=x11`?
4. Did an unexpected `Slack.desktop` uppercase file reappear locally or globally?

Only if an uppercase `Slack.desktop` reappears or duplicate entries return should you re-open the launcher state investigation.

## D-Bus Validation

Use this when popup and history behave differently:

```bash
dbus-monitor --session "interface='org.freedesktop.Notifications'"
```

Check whether clicking the notification causes Plasma to emit:
- `ActionInvoked`
- `ActivationToken`

Interpretation:
- If neither appears, the issue is likely in Plasma notification handling.
- If they do appear and Slack still does not open, the issue is in Slack/Electron or in the currently running Slack flags.

## Case Notes

Observed working end state for this machine:
- session originally `Wayland`
- local Slack override was accidentally forced to Wayland and notification activation stayed broken
- switching launcher to `--ozone-platform=x11`
- refreshing KDE caches
- killing the old Slack process
- reopening Slack from the launcher

Result:
- popup notification click worked
- `Notifications History > View` also worked after reopening Slack on the updated X11 launcher
