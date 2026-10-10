# Upgrading to Silicon 7.0

Silicon 7 uses Carbon and Silicon accounts throughout. Silicon Accounts authenticates identities, Silicon Apps installs packages, and the Developer portal manages publishing.

1. Stop the interpreter with `silicon stop`. Back up each `silicon.yaml` and its entire `SILICON_HOME` before editing.
2. Use the complete Silicon Accounts identity (`silicon.id: si:handle`) and its current token. Carbon IDs use `c:handle`.
3. Delete `silicon.org_id` and `silicon.SILICON_ORG` from YAML. Remove old environment-selection flags and variables from app wrappers. The new model has no organization or testing-environment configuration.
4. Keep bare catalog app IDs such as `dm`. If explicitly configured, replace the retired `iam` app with `silicon-accounts`. Install current app CLIs from [Silicon Apps](https://apps.teamofsilicons.com/).
5. Keep the original YAML path and Silicon home. Install 7.0, run `silicon compile /absolute/path/to/silicon.yaml`, and reconnect with `silicon connect /absolute/path/to/silicon.yaml`.
6. Use the event URL returned by the connection. A DNS-safe account handle uses `http://handle.si.localhost/events`. Update callers that retain an old hostname.
7. Check app login status. A valid session for the configured account is reused. Explicitly authenticate expired or logged-out apps with `si auth setup APP` inside an ISI.

The interpreter does not rewrite your YAML or delete prior session history. When a single legacy delivery namespace exists, the interpreter safely migrates it into the account's active state and resumes pending delivery. The migration can resume after interruption. Multiple legacy namespaces or conflicting destination files stop migration with a diagnostic rather than merging unrelated state or overwriting data.

The local dashboard saves access until logout, including across browser and interpreter restarts. Logout revokes the current local management token. `silicon web` opens the dashboard with the current token.

Configurations predating version 6 also need the [flow migration](GUIDE.md#upgrading-from-5x). Configurations with individual app webhooks need the [Ting migration](GUIDE.md#ting-migration).
