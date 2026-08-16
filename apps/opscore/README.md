# opscore

What every read-only operations tool in this repository needs, written once.

| Module | |
|---|---|
| `errors` | a small taxonomy where every error carries a `detail` — "klembord is not installed" is half an error; the other half is the command that installs it |
| `output` | one JSON envelope on stdout under `--json`, a readable table otherwise, and a failure path that never emits two envelopes |
| `secrets` | resolve a token from the environment or the OS keyring, and `redact` — the only way one may reach a human |
| `env` | find the project root by walking up for a marker, and export a `.env` without letting it override a real environment variable |
| `guard` | a confirmation gate that names what a write actually touches, because a wrong-but-scary warning teaches people to skim |

It exists because the alternative was three copies. Extracted from the Slack
bridge, which had already trimmed it down to what a tool that does not own a
database actually needs.
