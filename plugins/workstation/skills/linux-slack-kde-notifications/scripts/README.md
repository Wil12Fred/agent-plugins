# scripts/

Standalone helpers that back this skill's procedures, kept as real files so they
can be run, linked and tested. They travel with the skill on both install paths
(plugin copy and `make install-agents`).

Install one onto your `PATH` by symlinking it, so the running tool and the
repository are one file:

```bash
ln -sfn "$PWD/<script>" ~/.local/bin/<script>
```

## Index

| File | Does | Doc |
|---|---|---|
| [`slack-notif-click`](slack-notif-click) | User service: when Slack swallows a notification click on KDE, raise Slack and open the message's deep link itself. | [`slack-notif-click.md`](slack-notif-click.md) |
| [`slack-notif-click.service`](slack-notif-click.service) | The systemd `--user` unit that runs it with the graphical session. | same |
