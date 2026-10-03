# Silicon 6.1.0

A local interpreter for connected Silicons, powered by Rust and Silicon Omni.

## Start here

Silicon 6.1.0 uses the IAM and Honeycomb identifier contract introduced in 5.0, Omni 0.9.1, and Ting for notification delivery. It is built to run unattended: it starts at login or boot, restarts after a crash, and recovers by itself from network loss, sleep, and updates; see [Running unattended](#running-unattended). Existing 5.x Silicons need the [flow migration](#upgrading-from-5x) when updating. Existing 4.x Silicons must also complete the [identifier migration](#identifier-migration) before upgrading or reconnecting.

1. [Install Silicon and its dependencies](#installation) with the one-line command below.
2. [Create your first Silicon](#first-silicon) in a new directory, with your own IAM identity and token.
3. Run `silicon compile PATH`, then `silicon connect PATH`. Connection output shows setup results, app installation and authentication, and Ting registration.
4. Use `silicon web` for the dashboard or `silicon --help` for the command tree.

For configuration details, read the [field reference](#configuration-reference). To integrate an application, follow the [IAM contract](#iam-application-contract). To build clients, use the [HTTP interface](#local-dashboard-and-http-interface). Source and contribution details are in [development and compatibility](#development-and-compatibility).

Existing configurations using `handle:org` Silicon IDs or `org>app` application IDs require the [identifier migration](#identifier-migration).

## What runs on your machine

Silicon is a local interpreter for a `silicon.yaml` file. One interpreter can connect several Silicons. Each Silicon has its own identity, home directory, internal Silicons (ISIs), access rules, event flow, sessions, and logs.

The `silicon` command manages the interpreter from your terminal. The `si` command is provided inside an ISI so it can communicate with permitted ISIs, inspect work, manage its session, install or remove applications, and ask the interpreter to authenticate an application. The local dashboard exposes the same management operations through the interpreter's control API.

The interpreter listens on `127.0.0.1:1823`. If that port is occupied, it tries 1822, 1821, and so on, down through 1024. A dedicated Caddy process provides `http://silicon.localhost` for the dashboard and `http://<handle>.<org-id>.localhost` for each connected Silicon whose handle and owning organization are DNS labels. Other valid handles or organizations use a collision-free encoded host; use the hostname returned by `connect`. Caddy uses HTTP port 80; it does not search for an alternative proxy port.

Each active ISI session has an owned Omni daemon and uses Silicon Omni's Rust package. Omni starts and communicates with the selected inference provider. Silicon subscribes to Omni events, delivers new messages, assembles DNA, tracks sessions, and records progress. These are real provider processes; the interpreter does not simulate model output in normal use.

No terminal window is opened for each ISI. Each receives an independent process context, an Omni session, and the environment needed by its commands. Persistent session data survives stopping the interpreter. Global ephemeral work is discarded when it finishes.

## Installation

### Public binary installation

> Upgrading from 6.0.0: run `silicon update` and reconnect. Existing YAML remains compatible, including `silicon.apps`; moving apps into ISI definitions is optional.
>
> Upgrading from 5.x: prepare the [6.0 flow migration](#upgrading-from-5x), then stop the old interpreter before updating and applying it. Default send aggregation and catch behavior have changed; the installer does not rewrite your flow.
>
> Upgrading from 4.x to 6.1.0: disconnect and stop the old interpreter, then follow the [identifier migration](#identifier-migration). It changes Silicon IDs, adds `silicon.org_id`, and uses bare app IDs. Installers do not rewrite YAML or migrate local state.
>
> Upgrading from 4.0.x or earlier: also migrate per-app webhook configuration and event flows using the [Ting migration](#ting-migration). IAM and Ting are implicit dependencies; the interpreter installs both through Honeycomb.
>
> Upgrading from 4.0.6–4.0.8: replace any `sticky` and `archive_on_end` fields using the [migration table](#legacy-mode-fields-removed), then run `silicon update` and restart the interpreter. No reinstallation is needed.
>
> Upgrading from 4.0.5 or earlier: rerun the 6.1.0 installer into the same prefix. Older embedded updaters expect bundled app executables and cannot install this new layout. Existing YAML, credentials, and session state are preserved.

The public installer downloads a complete, versioned bundle. It does not require a Rust compiler. Install `v6.1.0` with:

```sh
curl -fsSL https://github.com/teamofsilicons/silicon-stemcell/releases/download/v6.1.0/install.sh | sh
```

Find the platform bundles and their checksums on the [v6.1.0 release page](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v6.1.0). An unavailable or incomplete bundle is a hard installation error; the installer does not substitute an older Stemcell release.

Silicon is distributed through GitHub Releases using the installers above. It does not require its own Honeycomb listing or IAM app registration. Honeycomb installs application dependencies such as IAM and Space Station.

**6.1.0** adds [Starter genes, functions, and ISIs](#reusable-starter-blocks), cached locally and refreshed on reconnect, with optional ISI field overrides. Apps can be declared per ISI; their combined set is installed and authenticated for the whole Silicon, including apps from Starter definitions. `si app install` records an app under the calling ISI, and `si app uninstall` removes its configured entries. DNA includes managed apps' Honeycomb names, descriptions, and help commands. Existing 6.0.0 configurations remain compatible; no app-list migration is required.

**6.0.0** makes flow structure native YAML: `for: {list, var, then}`, reusable functions and imports, `switch`, `continue`, `break`, `exit`, `collect`, CEL `groupBy`, and `self.for.index`. Local webhooks accept any valid JSON value. Sends aggregate per ISI and session, are saved before delivery, and retry up to `silicon.max_retries` (default `10`) after the initial attempt. Exhausted messages remain stashed until another send to that destination triggers recovery. `send.catch` covers expression and target-validation failures; all catches use `self.error`. These 6.0 changes require the [5.x flow migration](#upgrading-from-5x) when upgrading from 5.x or earlier.

**5.1.0** runs unattended for months. The first `silicon connect` sets up autostart (a LaunchAgent on macOS, a systemd user service with linger on Linux, cron and a built-in supervisor elsewhere, and a logon task on Windows), so the interpreter starts at login or boot and restarts after a crash; `silicon service` manages it. Saved Silicons restore in the background and are retried until they come back, and a Silicon whose restore fails is never forgotten. Caddy is supervised and an orphaned one is cleaned up, every tool the interpreter runs has a time limit, logs rotate, old releases are pruned, heartbeats keep wall-clock time across sleep and restarts, idle session workers are retired, and the updater backs off and reclaims a stale installer lock. It moves to Omni 0.9.1, whose Codex chats no longer stall after a mid-turn message. Upgrading from 5.0.x needs only `silicon update`; see [Running unattended](#running-unattended) for what changed in behavior.

**5.0.2** shows every failure in full. An app CLI, IAM, Ting, Honeycomb, Caddy, Omni, Bash expression, updater, or installer failure now carries the command, its exit status, and its complete stderr and stdout, plus parser positions, HTTP bodies, and the whole cause chain, in CLI output, `si` output, flow catches, the dashboard, and `silicon.log`. Only literal credential values are masked. See [Reading errors](#reading-errors). Upgrading from 5.0.1 needs only `silicon update` and an interpreter restart.

**5.0.1** strengthens the identifier migration introduced in 5.0.0. Its mapping tool preserves sessions, explicit executable aliases and Ting delivery state; authentication checks bind the full Silicon identity, owning organization and selected grant. Existing 5.0.0 YAML and local hostnames remain compatible.

**5.0.0** adopts `si:handle` Silicon IDs, a required explicit `silicon.org_id`, and bare Honeycomb app IDs. Owning and selected organizations are explicit. Old identifiers require the [identifier migration](#identifier-migration).

**4.1.0** upgrades Omni to 0.9.0 and replaces per-app webhooks with Ting batches. It adds `SILICON_ORG`, private `app_configs`, `si app install`/`uninstall`, live flow loading, and CEL list helpers. Ting batches are durably accepted before an empty HTTP 204; the retained hook and pending inbox survive restarts.

**4.0.9** retains session-addressed ephemeral records and event history, archiving them when work retires. Only global ephemeral work is discarded. Logs identify the CLI or daemon, the sending ISI, and the flow branch or assignment. The legacy `sticky` and `archive_on_end` fields are removed; use `primary_send_mode` and `session_type` as described in the [migration table](#legacy-mode-fields-removed).

**4.0.8** moves to Omni 0.8.0. A provider that crashes, hits a rate limit, becomes unavailable, or will not start no longer keeps the chat: Omni closes the turn, sets that provider aside for the rest of the live session, and resolves the same ask over the providers left. The interpreter logs each removal with its reason and the remaining providers. Omni also finds CLIs on the `PATH` the user's login shell reports, so a CLI visible only to a terminal, or installed after the daemon started, is found.

**4.0.7** repairs a package home whose Honeycomb `auto_update` setting 4.0.6 removed. Honeycomb requires that setting, so those homes rejected every command and could not connect or install apps. The interpreter now restores Honeycomb's own default instead of deleting the setting, and leaves later preferences alone.

**4.0.6** removes bundled Honeycomb app versions. Every connection installs configured apps with no version argument, and app automatic updates remain enabled according to their own settings.

**4.0.5** caches successful automatic app-auth checks for 48 hours across reconnects and restarts. Checks run only on YAML connects and new session creation; heartbeats reuse authentication, and explicit `si auth setup` bypasses the cache.

**4.0.3** adds `make_readable` and `convert_time`, fixes the reference YAML expressions, and streams connection progress with completion checkmarks.

**4.0.0** focuses on the local interpreter and adds a Windows launcher using WSL2. The hosted realtime publisher, remote reader commands, and relay service have been removed. Local ping, configuration inspection, logs, the dashboard, and Space Station telemetry remain available. The installer includes every local component needed by the interpreter, including Space Station installed through Honeycomb when the release bundle is built.

**Upgrading from 3.5.x requires running this complete installer once into the same prefix**, even when the old updater has already changed the reported Silicon version to 5.0.2. Its fixed dependency inventory cannot add Honeycomb or Space Station. Follow the [migration instructions](#upgrading-from-35x) below before using the new package features.

The default installation prefix is `~/.local/share/silicon`. Add its `bin` directory to your shell's `PATH`:

```sh
export PATH="$HOME/.local/share/silicon/bin:$PATH"
```

For the default prefix, the installer adds this path once to the startup file for your detected shell. Open a new terminal, or run the printed export command in your current terminal. Set `SILICON_NO_PATH=1` to leave shell startup files alone. Custom prefixes print the path without changing startup files.

To choose another dedicated prefix, set the variable on the `sh` side of the pipeline:

```sh
curl -fsSL https://github.com/teamofsilicons/silicon-stemcell/releases/download/v6.1.0/install.sh \
  | SILICON_PREFIX="$HOME/tools/silicon" sh
```

The target matrix covers macOS, Linux, and Windows on ARM64 and x86-64. Windows runs the Linux bundle through WSL2. Native Unix bundles target macOS 15 and Ubuntu 24.04; the Linux binaries require glibc 2.39 or newer. Platform verification is recorded in the release evidence below; testing one architecture does not verify another.

The binary installation needs `curl`, `tar`, and a SHA-256 verifier (`sha256sum` or `shasum`). Downloads use HTTPS. Before activation, the installer checks the bundle checksum, required members, version marker, and whether the interpreter and Omni daemon can execute. A complete release includes:

| Command | Bundled component |
| --- | --- |
| `silicon`, `si` | Interpreter and internal CLI, 6.1.0 |
| `omnid`, `silicon-omni`, `omni`, `so` | Omni 0.9.1 pinned to commit `62c2adc57983be37c1de2064d169073bddf71291` |
| `caddy` | Caddy 2.11.4 |

Honeycomb is installed separately from its checksum-verified latest release; existing update preferences and services are preserved. Each Silicon connection runs `honeycomb install 'app' --json` for configured and registered canonical app IDs, the IAM issuer (`iam`), and Ting (`ting`). Omitting `--version` selects the latest public package each time; no app version is pinned. Apps are not copied into the interpreter bundle, wrapped to suppress updates, or given environment variables that disable their automatic updates. Omni and Caddy remain bundled runtime dependencies.

Accounts, permissions, remote service availability, and authenticated inference providers must still be available. Installing a CLI does not create an IAM identity or grant provider access.

### Upgrading from 5.x

Prepare these edits in a separate copy of your configuration while 5.x is running. Do not replace its active flow with 6.0 syntax: 5.x does not support `aggregate`, native loops/functions, or `self.error`. Stop the old interpreter before installing 6.1.0 and applying the edited configuration. These changes also apply to older configurations after their identifier and webhook migrations. The installer preserves YAML and does not translate it.

1. Replace bare `{error}` in every catch with `{self.error}`. Result and error values are scoped to their `then` or `catch` branch; save a value with `var` when it is needed later.
2. Review each send that must happen immediately or remain a separate message. Add `aggregate: false` to it; otherwise messages to the same `(isi, session_id)` are joined in order and sent when the flow finishes. An immediate send also flushes earlier queued messages for that destination.
3. Move transport-failure handling out of `send.catch`. It now catches expression and target errors only. The runtime persists outgoing messages, retries delivery, logs failures, and stashes exhausted messages. A later send to the same ISI and session replays the stash first; no timer or alternate destination is used. `silicon.max_retries` defaults to ten retries after the initial attempt; set it explicitly if another limit is needed.
4. Keep existing Ting flows on `request.tings`. Only change their input handling if sending another JSON shape; generic webhook bodies now reach `request` unchanged. Existing CEL-generated steps and `!` Python or shell commands remain supported, so adoption of native loops and functions can be incremental.
5. Run `silicon stop`, then `silicon update` while the interpreter is stopped. Apply the prepared configuration edits, check `silicon --version`, and compile each configuration with the new `silicon compile /path/to/silicon.yaml`. Then reconnect with `silicon connect /path/to/silicon.yaml`. Prepare all saved configurations before the first reconnect, since it starts the interpreter and restores its other saved Silicons.

To hold updates while preparing this migration, keep the interpreter stopped or set `SILICON_AUTO_UPDATE=0` in the environment of the interpreter process before starting it; see [Updates](#updates). Finish the edits before restarting on 6.1.0. Incoming and outgoing journals survive restarts, but crash recovery is at least once; application side effects should use event IDs for idempotency.

### Identifier migration

Use `silicon.id: si:handle`, an explicit `silicon.org_id`, and bare IDs in `apps` and `app_configs`. Carbon recipients use `c:handle`; ISI recipients use `isi@si:handle`. Bundle IDs remain `org>bundle`. Follow the [state-preserving migration procedure](https://github.com/teamofsilicons/silicon-stemcell/blob/main/docs/PUBLIC-IDENTIFIER-MIGRATION.md) before reconnecting an existing home. It uses IAM's verified mapping, preserves sessions and Ting delivery state, and migrates Honeycomb's registry without moving package bytes.

### Ting migration

For configurations from 4.0.x or earlier, before reconnecting on 6.1.0:

1. Remove `silicon.webhooks` and `silicon.webhook`. Keep application IDs under `isi.<name>.apps`; legacy `silicon.apps` remains supported. Applications publish notifications through Ting and no longer register separate interpreter webhooks.
2. Change the flow to read `request.tings`, a list of notification objects with `id`, `type`, `data`, and `metadata`. Other JSON shapes are also accepted, but Ting publishers use the batch envelope. Use native `for` steps or CEL list helpers to process a whole batch; the [first Silicon example](#first-silicon) shows a minimal flow.
3. Check that the Silicon identity can authenticate IAM and Ting. Both are installed through Honeycomb automatically, even with an empty `apps` list. The interpreter registers only Ting at `http://HANDLE.OWNER_ORG.localhost/events`, retaining its hook ID for reconnects.
4. Run `silicon compile /path/to/silicon.yaml`, then reconnect or restart the interpreter. The updater and installer do not rewrite your YAML.

Ting receives an empty HTTP 204 after the complete batch is durably saved, before the flow runs. Follow logs or session progress to inspect processing. The interpreter loads the latest flow and function definitions for every request. Other manual configuration changes require reconnecting; `si app install` and `si app uninstall` update connected app settings immediately.

### Windows installation and project files

Existing Windows installations should rerun this release's PowerShell installer to add the native `ting.exe` entry point. `silicon update` updates the WSL2 runtime but does not add Windows launcher files.

In PowerShell:

```powershell
irm https://github.com/teamofsilicons/silicon-stemcell/releases/download/v6.1.0/install.ps1 | iex
```

Windows uses a native launcher and a dedicated WSL2 distribution named `Silicon`, with the same Linux runtime bundle and independent Honeycomb installation used on Linux. First-time WSL2 setup can require administrator access, hardware virtualization, and a restart; rerun the installer after completing that setup. Ordinary interpreter commands run as the unprivileged Linux user `silicon`.

Keep each project, its YAML, and its `SILICON_HOME` inside the distribution's Linux filesystem. Open `\\wsl.localhost\Silicon\home\silicon` in File Explorer and copy or create your project folder there. For example:

```powershell
silicon compile '\\wsl.localhost\Silicon\home\silicon\assistant\silicon.yaml'
silicon connect '\\wsl.localhost\Silicon\home\silicon\assistant\silicon.yaml'
silicon web
```

`SILICON_HOME: ! pwd` continues to mean the YAML's directory. Setup commands, relative executables, DNA scripts, application CLIs, and ISI sessions execute from that home. The launcher translates explicit Windows paths into WSL paths, but it never rewrites or silently moves your YAML. A configuration or home on a Windows drive is rejected because private credential modes and Unix socket semantics require Linux storage.

Windows data remains accessible: `C:\Users\You\Documents` is `/mnt/c/Users/You/Documents`, and `D:\Projects` is `/mnt/d/Projects`. Reads and writes affect the actual Windows files and respect Windows permissions. Put explicit paths to that data in your scripts as needed. Portable Bash and installed CLI commands can use the same YAML on all platforms; macOS-specific tools and options still need alternatives inside the scripts.

The Linux runtime keeps the managed Unix installation layout. Its updater updates the Linux bundle; rerun the Windows installer to update the Windows launchers and browser bridge. If a later update replaces Caddy and needs to renew its port-80 capability, rerun the Windows installer; it performs that provisioning inside the dedicated distribution.

Windows ARM64 is a preview until an actual ARM64 WSL2 end-to-end run is available. Hosted ARM runners can test the native launcher, and Linux ARM64 has its own native runtime checks, but those do not establish the complete Windows ARM64 installation path. Windows x64 release validation includes the real WSL2 installation and interpreter integration checks. See the release evidence for completed results.

### Migrating from 3.6.x

Version 4 removes the remote `login`, `logout`, and `watch` commands and the `realtime` setting. Use local `silicon logs`, `silicon config`, and `silicon ping`. Saved Silicon configurations, sessions, and managed-app authentication are retained. The interpreter no longer connects to the retired relay.

### Upgrading from 3.5.x

The 3.5.x updater runs its embedded installer, whose fixed file inventory predates Honeycomb and Space Station. It can install a newer interpreter while leaving those new dependencies absent. Downloading the new `install.sh` as part of an update does not execute that script. This is a limitation of the older Silicon updater, not a Honeycomb or Space Station defect.

First complete the [identifier migration](#identifier-migration), including disconnecting the old YAML paths and stopping the interpreter. Rerun the **6.1.0 public installer into the same prefix**:

```sh
silicon stop
curl -fsSL https://github.com/teamofsilicons/silicon-stemcell/releases/download/v6.1.0/install.sh | sh
silicon serve
```

Skip `silicon stop` if no interpreter is running. `silicon serve` runs the new interpreter; reconnect each migrated YAML path after compiling it. The default command uses `~/.local/share/silicon`; if your existing installation uses another prefix, preserve it on the `sh` side of the pipeline:

```sh
curl -fsSL https://github.com/teamofsilicons/silicon-stemcell/releases/download/v6.1.0/install.sh \
  | SILICON_PREFIX="$HOME/tools/silicon" sh
```

Replace that example path with your existing prefix. Run this migration once even if an automatic update or `silicon update` already reports 5.0.2. The current installer installs Honeycomb independently; configured apps are installed when the Silicon connects. Your Silicon YAML and runtime state remain outside the bundle payload.

### Linux and port 80

On Linux, the installer checks whether unprivileged processes can bind port 80. If not, it grants only the bundled Caddy binary `cap_net_bind_service=ep` using `setcap`. This requires the system's libcap tools and suitable privileges; it can ask for `sudo` in an interactive terminal. Silicon itself does not need to run as root.

An automatic, noninteractive update cannot prompt for privileges. When the Caddy binary is unchanged, the installer can reuse the existing capability. If a new Caddy binary needs a capability and unattended privilege is unavailable, the installer exits with status 77 and leaves the current release selected. The updater reports this once with the command to run and does not retry that release until the installed release changes or a newer release appears. Run `silicon update` in a terminal as a user who can use `sudo` (on Windows, rerun the Windows installer). The installer adds `/usr/sbin` and `/sbin` to `PATH` so `getcap` and `setcap` are found under service managers.

### Build the complete installation from source

From this repository's root:

```sh
SILICON_SOURCE_DIR="$PWD" sh install.sh
```

Source installation requires Rust 1.98 or newer, Cargo, a C compiler, and dependencies needed by the pinned Rust crates. Release CI pins Rust 1.98.1. It builds the interpreter and Omni, downloads verified Caddy, and separately installs the latest Honeycomb. Application CLIs are installed when a Silicon connects. Caddy's upstream checksum file uses SHA-512.

`SILICON_GIT_REV` is an alternative to `SILICON_SOURCE_DIR`: supply an exact, lowercase, 40-character Git commit. The installer fetches that commit and verifies the checkout. The two source selectors cannot be used together.

For controlled builds, `SILICON_DEPENDENCY_BIN_DIR` can supply trusted Omni and Caddy executables. This is a reuse mechanism, not independent verification of arbitrary local binaries. The interpreter is still built from the selected source, and Honeycomb is still installed independently at its latest release. `CARGO_TARGET_DIR` can reuse a compilation directory.

For a developer build of only the interpreter and internal CLI:

```sh
cargo build --locked
./target/debug/silicon --help
./target/debug/si --help
```

This does not install the required runtime dependencies. Put the intended `omnid`, Caddy, IAM, and application commands on `PATH`, or use the documented binary overrides. Automatic bundle updates do not replace an unmanaged `target/debug` or `target/release` executable.

### Activation and retained releases

Installed commands are symlinks through `<prefix>/lib/silicon/current/bin`. A new release is prepared under the same prefix, checked, moved into `<prefix>/lib/silicon/releases`, and selected by replacing the `current` symlink. Existing unmanaged files in `<prefix>/bin` are not overwritten; choose another prefix if there is a conflict.

Only one installation may modify a prefix at a time. `.install-lock` records its owner (`pid`, when that process started and, on Linux, the boot it ran in). A lock over a minute old is reclaimed automatically, saying why, when its process is gone, was started before the last boot, or is a different process that reused the number; a lock whose owner cannot be checked (one left by an older installer) is reclaimed after two hours. Installers take turns reclaiming through `.install-lock.reclaim`; if an error says another installer is reclaiming and none is running, remove that directory.

Old releases are pruned. After activation the installer keeps the active release, the one it replaced, the release of the interpreter running the update, and any release a running interpreter recorded in `<prefix>/lib/silicon/running/`; it removes other releases and staging directories older than an hour. The interpreter also prunes when it starts, keeping the active release, its own, and the newest other one. There is no public rollback command.

The installer does not copy the repository's `stemcell` examples into a user's home. It never edits a connected `silicon.yaml`, memories, workspace, or session archive.

## First Silicon

Create your own directory and your own `silicon.yaml`. Keep it separate from the repository's reference fixture. The following is a complete schema example. Replace the identity and token with your values and ensure Omni has an authenticated provider that can satisfy the `fast` model key.

```yaml
silicon:
  id: si:assistant
  org_id: my-org
  token: REPLACE_WITH_YOUR_SILICON_TOKEN
  timezone: Asia/Kolkata
  SILICON_HOME: ! pwd
  SILICON_ORG: my-org
  inference_providers:
    - all-available-providers
  setup:
    - ! mkdir -p workspace

isi:
  assistant:
    model: fast
    primary_send_mode: global
    session_type: persistent
    apps: []
    dna:
      assemble:
        - '! printf "You are a helpful assistant. Keep useful notes in your workspace."'
      next_refresh: 30min

access:
  assistant: []

flow:
  - for:
      list: '{request.tings}'
      var: ting
      then:
        - send:
            isi: assistant
            message: '{make_readable(var.ting)}'
```

Setup creates a workspace directory on each connection; `mkdir -p` is safe to repeat. IAM and Ting are implicit dependencies, even with empty ISI `apps` lists. Add other application IDs under `isi.assistant.apps` when the corresponding Silicon identity and application permissions are ready. Add the optional `space_station` fields shown below only after creating your table and obtaining its key. `token` remains required even if the first test does not use an IAM application.

```sh
silicon compile /absolute/path/to/silicon.yaml
silicon connect /absolute/path/to/silicon.yaml
silicon ls
silicon send si:assistant assistant "Hello"
silicon show si:assistant assistant
silicon web
```

`compile` validates configuration and expressions without connecting to the interpreter. It does execute compile-time Bash expressions, including `SILICON_HOME`, and downloads missing [Starter blocks](#reusable-starter-blocks), so it is not a side-effect-free shell dry run. Local configuration, flow, and imported function syntax are checked before Bash runs. Starter files require the resolved home first; their syntax is checked after downloading and before evaluating the remaining compile-time fields. `setup`, application commands, DNA scripts, and runtime flow actions remain deferred. Provider startup, application authentication, and runtime request values have their own checks later.

`connect` starts the interpreter if necessary, refreshes referenced Starter blocks while compiling the file, runs setup, installs and authenticates managed applications, adds routing, applies app configuration, registers the local route with Ting, and records the connection. Its text output streams local setup, application installation, authentication, and webhook progress while the operation runs. In a terminal, `… Authenticating dm` changes to `✓ Authenticated dm`; failures use `✗`. Redirected output uses separate lines without terminal control codes. `--json` returns the normal complete JSON response with its progress records. Setup, installation, authentication, and Ting commands run with the [time limits](#time-limits) the interpreter enforces. A failed automatic app authentication no longer fails the connection: it shows as `✗` with the app's complete answer, and the app is checked again later (see the [IAM application contract](#iam-application-contract)). Duplicate Silicon IDs, duplicate canonical YAML paths, or two connected Silicons sharing the same canonical `SILICON_HOME` are rejected.

The local identity is the canonical YAML path. The global identity is `silicon.id`; `silicon.org_id` supplies its owning organization separately. Moving a file changes its local identity. The interpreter reads `flow` and optional `functions` definitions from disk for each incoming request, without rerunning compile-time shell expressions. Disconnect and reconnect to apply other manual configuration changes. `si app install` and `si app uninstall` update the file’s app entries and the connected app settings immediately; ordinary connection and interpreter updates do not rewrite it.

`silicon disconnect ./silicon.yaml` resolves the path in your terminal's working directory. Direct control-API requests must supply a Silicon ID or an absolute YAML path. Delayed flow sends, heartbeat work, session capabilities, and ephemeral replies stay bound to their original connection or worker; reconnecting the same ID does not transfer them to the replacement.

Send an event through the Silicon's host:

```sh
curl --fail-with-body \
  --header 'Content-Type: application/json' \
  --data '{"tings":[{"id":"local-test-1","type":"new_message","data":{"message":"Hello from an event"},"metadata":{}}]}' \
  http://assistant.my-org.localhost/events
```

Caddy routes the hostname but does not edit DNS or `/etc/hosts`. If a particular client does not resolve `.localhost` names, the equivalent diagnostic command can add `--resolve assistant.my-org.localhost:80:127.0.0.1`.

## Configuration reference

The four required top-level keys are `silicon`, `isi`, `access`, and `flow`. `functions` is optional and may appear anywhere at the top level. Unknown fields are errors. Ordinary duplicate YAML keys are errors. Flow action order is preserved, including the reference dialect's repeated `if` keys, but the recommended form is an explicit list of steps.

### `silicon`

| Field | Meaning and validation |
| --- | --- |
| `id` | Required complete Silicon public ID, `si:handle`. The handle is 3–50 lowercase ASCII letters, digits, underscores, or hyphens; the prefix does not count toward that limit. |
| `org_id` | Required explicit IAM owning organization: 3–50 lowercase ASCII letters, digits, underscores, or hyphens. It is never inferred from `id`. |
| `token` | Required Silicon credential for IAM. Empty strings, NUL bytes, and the exact placeholder `...` are rejected. It is not an ISI capability or a dashboard token. |
| `timezone` | Required IANA timezone, such as `UTC`, `Asia/Kolkata`, or `America/Los_Angeles`. |
| `SILICON_HOME` | Required existing directory, or an expression producing one. Relative paths resolve against the YAML file's directory. The final path is canonicalized. |
| `SILICON_ORG` | Organization passed to app CLIs, Bash, and provider sessions. Defaults to `silicon.org_id`; an explicit value uses the same 3–50-character IAM organization grammar. |
| `inference_providers` | Required nonempty list of provider names, `all-available-providers`, exclusions, or nested lists. |
| `setup` | Optional ordered list of shell commands. Runs once during each connection attempt; compilation validates expressions without running these commands. |
| `apps` | Legacy list of Honeycomb IAM application IDs, retained for existing configurations. New files declare apps under `isi.<name>.apps`; both contribute to the Silicon's managed apps. |
| `app_configs` | Optional mapping from managed app IDs to configuration objects. Native YAML numbers, booleans, lists, maps, and null retain their types; string values support private compile-time expressions. Applied with `APP config set JSON` after authentication. |
| `space_station` | Optional object with nonempty `table_name` and `table_key` for a user-owned telemetry destination. This destination remains enabled when TOS telemetry is turned off. |
| `max_retries` | Optional nonnegative integer, default `10`; retries after the first outgoing flow delivery attempt before stashing for the next send to that destination. |
| `login` | Legacy list of deferred IAM application commands; retained for existing configurations. |

`SILICON_HOME: ! pwd` runs `pwd` with the YAML's parent directory as its working directory. Once home is resolved, later compile-time commands and runtime scripts use that home. Setup, DNA scripts, flow expressions, managed application commands, and Omni sessions all start with `SILICON_HOME` as their working directory. Use `./tool` or `./scripts/tool` for executables stored there; bare command names resolve through `PATH`, with the home's `.silicon/bin` first. Compile-time fields are evaluated once per compilation, not on every incoming event.

Provider selection walks the list from top to bottom. `all-available-providers` adds Omni's available providers, `except NAME` removes a provider, and a later explicit name adds it back. Nested lists follow the same order:

```yaml
inference_providers:
  - all-available-providers
  - except codex-app-server
  - [claude-code-cli]
```

Every session opens with an explicit provider list. A lone `all-available-providers` sends the providers Omni reports as installed and authenticated when that session starts, so a provider dropped after an authentication error, or logged in later, rejoins at the next session start; an empty result is an error naming what Omni reported. Explicit names are checked against Omni's installed/authenticated providers when a session initializes. An empty final selection is an error. Extra named groups such as `open-weight-models` are not expanded unless Omni exposes that exact name as a provider.

Use canonical application IDs under each ISI in new files. Setup, app configuration, and telemetry settings stay under `silicon`:

```yaml
silicon:
  # ... identity, home, timezone, and inference providers ...
  setup:
    - ! mkdir -p workspace
  app_configs:
    waveform:
      default_tts_provider: google
  space_station:
    table_name: my-silicon-events
    table_key: REPLACE_WITH_YOUR_TABLE_KEY
isi:
  assistant:
    # ... model, modes, and DNA ...
    apps: [dm, waveform]
  researcher:
    # ... model, modes, and DNA ...
    apps: [briefcase, dm]
```

Each application ID is a bare handle of 1–80 lowercase ASCII letters, digits, underscores, or hyphens, beginning with a letter. The owning organization is explicit IAM/Honeycomb data, not part of the app ID. Bundle IDs retain `org>bundle`; Honeycomb release selectors such as `briefcase>test@2.1.0` keep their existing meaning. Duplicate IDs within one list are errors; the same app may appear under several ISIs. The interpreter installs and authenticates the union of all ISI app lists, including those from [Starter ISIs](#reusable-starter-blocks), plus legacy `silicon.apps`. Apps are available to the whole Silicon: these lists organize configuration and do not enforce access restrictions between ISIs. `ting` is always managed and registered, even when omitted from `apps`. The removed `webhooks` and `webhook` keys are configuration errors; delete them and retain application IDs in `silicon.apps` or ISI `apps`. App configuration keys must name a managed canonical ID, including implicit `ting`.

Setup commands run from `SILICON_HOME` with `ISI=interpreter`. A leading `!` is accepted; the first command may also be ordinary shell text. Each shell fallback must have its own `!`; a quoted fallback is literal text. CEL is evaluated before Bash. Command stdout and stderr are captured in the connection log after each command finishes. A failed step's error names `silicon.setup[N]` and carries the command's exit status and complete stderr and stdout. Setup is connection work, so reconnecting runs it again. Make commands safe to repeat. Setup does not run during `silicon compile`, before each message, or on every DNA refresh. An exhausted fallback chain aborts the connection; completed shell side effects are not rolled back.

`login` contains executable names and arguments, not shell programs to run while compiling:

```yaml
login:
  - ! dm
  - 'hook --profile work'
```

This field expands CEL and `!>>` fallbacks, preserves argument quotes, and retains an original leading `!` to distinguish an explicit executable command from a bare managed app ID. The marker is removed before execution; it does not execute Bash during evaluation. For example, `! dm` identifies the command to which Silicon later appends `iam --json`, or a login command; it does not launch bare `dm` during compilation. Command arguments are parsed with shell-style quoting and executed directly as argv. Shell pipelines, redirections, and environment assignments are not command wrappers; use a real executable wrapper when one is required.

### `isi`

Define at least one ISI. Names may contain ASCII letters, digits, `.`, `_`, and `-`, up to 128 bytes; `.` and `..` alone are not allowed.

| Field | Meaning |
| --- | --- |
| `model` | Required Omni model key, passed using `Ask::key`. A key such as `fast` or `code` is resolved by Omni. |
| `primary_send_mode` | Required `global` or `session`. It controls addressing, not retention. |
| `session_type` | Required `persistent` or `ephemeral`. It controls completion behavior, and retention together with `primary_send_mode`: only `global` + `ephemeral` is discarded. |
| `dna` | Required mapping with `assemble` and `next_refresh`. An empty assembly list is permitted; the internal instruction footer is still appended. |
| `apps` | Optional list of bare Honeycomb IAM application IDs. Installed and authenticated for the whole Silicon; an omitted app list does not restrict an ISI's app access. |
| `heartbeat` | Optional mapping with `next` and a nonempty string `message`. Omit it for no heartbeat. |
| `new_session_suggestion` | Optional mapping with `cooldown_minutes`, `min_new_messages`, and a nonempty string `suggestion_message`. Omit it for no suggestions. |

#### Legacy mode fields removed

Addressing and retention are set only by `primary_send_mode` and `session_type`. The former `sticky` and `archive_on_end` fields have been removed; a configuration that still uses one is rejected with the replacement named:

| Removed field | Write instead |
| --- | --- |
| `sticky: true` | `primary_send_mode: global` |
| `sticky: false` | `primary_send_mode: session` |
| `archive_on_end: true` | `session_type: ephemeral` |
| `archive_on_end: false` | `session_type: persistent` |

Note the last row when migrating: `archive_on_end: false` meant persistent, not ephemeral.

### Intervals and scheduled work

Bare numeric intervals are minutes. `30min`, `30m`, `10s`, and `2h` are also accepted. Values must be finite and positive, at most ten years. Schedules run at least one second apart: a shorter literal is accepted with a warning and runs every second. Timing expressions can use Bash, CEL, and fallbacks; the evaluated result must satisfy the same interval rules.

DNA is assembled at session initialization. `next_refresh` is reevaluated after assembly and after each refresh. Refresh work runs separately from the provider event listener, so a slow DNA command does not block receipt processing. A failed refresh is logged and retried after approximately 60 seconds.

Heartbeat `next` is evaluated when its schedule is created and when it becomes due. The first heartbeat waits for its configured interval. A global ISI can be started by its heartbeat. A session-addressed ISI gets independent heartbeat schedules for existing active session addresses; a heartbeat does not invent a missing session ID. Before sending a heartbeat, the interpreter checks managed app authentication using the same cached checks as new sessions. Failed timing evaluations are logged and retried after approximately 60 seconds.

Heartbeat and DNA refresh times follow the wall clock, so system sleep does not stretch them, and heartbeat due times are saved per address in `<SILICON_HOME>/.silicon/heartbeats.json`, so restarts, reconnects, and self-updates neither reset nor skip them. A due heartbeat fires once; missed ticks are not replayed. Heartbeats found overdue when a Silicon connects fire within its first minute, at random offsets. A due time more than one interval ahead (the clock went back) is pulled in and logged. When a reconnect brings a changed `heartbeat.next`, a shorter interval applies from the reconnect and a longer one after the heartbeat already scheduled. A heartbeat is skipped while the previous heartbeat to the same target is still undelivered or unfinished. `next` is evaluated off the scheduler thread; keep interval expressions quick, since they run under the expression [time limit](#time-limits). DNA, heartbeat, and suggestion expressions inside a session-addressed ISI receive `ISI=name:session-id`.

A new-session suggestion is a message, not an automatic archive. It is considered after a normal incoming message. At least `min_new_messages` new messages since the last suggestion are required, and subsequent suggestions must respect `cooldown_minutes`. Heartbeats and suggestion messages do not count toward that threshold. The first suggestion can occur as soon as the message threshold is reached; the cooldown is between suggestions. Counters belong to each session. Archived sessions do not receive these suggestions.

### DNA assembly

Each `dna.assemble` entry is one of:

- A path relative to `SILICON_HOME`, whose contents are read.
- A `starter:gene:ID` reference to a downloaded Starter gene.
- A `!` Bash expression whose stdout becomes prompt text.
- A fallback chain combining files, commands, and explicitly quoted literal text.

```yaml
dna:
  assemble:
    - silicon.md
    - starter:gene:creativity
    - ! cat worker.md
    - ! ./contacts.sh !>> CONTACTS.md !>> "No contacts are available."
  next_refresh: 30min
```

`learn.sh` without `!` is a file to read. `! ./learn.sh` executes it. Quoted fallback text inside the scalar is literal prompt content; ordinary YAML quotes around a path are only YAML syntax.

Each successful entry includes its original scalar text on one line, followed by the evaluated contents. Entries are separated by blank lines, so the ISI can see the source of its instructions:

```text
silicon.md
Contents of silicon.md


! ./contacts.sh !>> "No contacts are available."
No contacts are available.
```

The original entry is preserved even when a later fallback supplies its contents. An exhausted DNA fallback is logged with every candidate's reason, and that assembly entry is skipped. Other entries continue. The interpreter appends a small instruction footer identifying the current ISI, its home, allowed ISI targets and their addressing modes, and the relevant `si --help` entry points. Names outside that ISI's access list are not included in this footer.

DNA also includes the managed apps' Honeycomb names and descriptions, with a help command for each app. This catalog covers the Silicon's shared apps, regardless of which ISI declared them:

```text
App Name: <app name from Honeycomb>
App Id: dm
CLI: run `dm --help` to know about it

About: <app description from Honeycomb>
```

The catalog is saved as `.silicon/apps.md`. When Honeycomb metadata cannot be refreshed, the interpreter logs the failure and keeps previously fetched details; apps without cached details still receive an ID and help command.

### Reusable Starter blocks

Starter supplies genes, functions, and ISIs. Use `starter:gene:ID` in `dna.assemble`, `starter:function:ID` in `call.function`, and `starter:isi:ID` as an ISI key. Each position requires its matching block kind.

```yaml
isi:
  starter:isi:researcher: default
  starter:isi:writer:
    model: code
    apps: [briefcase]
    dna:
      next_refresh: 20min
  assistant:
    model: fast
    primary_send_mode: global
    session_type: persistent
    apps: [dm]
    dna:
      assemble: [starter:gene:creativity]
      next_refresh: 30min

access:
  researcher: [assistant]
  writer: [assistant]
  assistant: [researcher, writer]

flow:
  - call:
      function: starter:function:greet
      args: {name: '{request.name}'}
      then:
        - send: {isi: assistant, message: '{self.result}'}
```

`default` keeps the published ISI definition. A mapping overrides its fields recursively; lists replace the published list, so `apps: [briefcase]` replaces that ISI's default apps. The published definition and overrides together must supply all required interpreter fields, including modes and DNA refresh settings. Use the resolved ISI name, such as `researcher`, in `access` and `send`. Apps remaining in the resolved definition join the Silicon's shared app set. The required `silicon` fields are omitted from this snippet.

Downloads live under `<SILICON_HOME>/.fromstarter`. Compilation reuses cached blocks and downloads missing ones. Connecting or restoring a Silicon downloads referenced blocks fresh; the active connection and live flow reloads use the local cache. To pick up a newly published block, reconnect. Append `@<sha256>` to pin a reference to an exact publication; unpinned references use the latest publication. Downloading does not rewrite `silicon.yaml`.

The interpreter requires Starter 0.3.0 or newer and installs or upgrades it through Honeycomb before downloading blocks. An explicit `SILICON_STARTER` executable override must already meet that version. Honeycomb installation requires access to its Starter listing. If that listing is unavailable to you, install the [public Starter CLI](https://github.com/teamofsilicons/silicon-starter/releases/latest) first:

```sh
curl -fsSL https://starter.teamofsilicons.com/install.sh | sh
starter --version
```

The installer verifies the downloaded archive's SHA-256 checksum. On Windows, first open the interpreter's Linux environment with `wsl --distribution Silicon --user silicon`, then run those commands there. Restart the interpreter if installation changed its `PATH`.

Public blocks are readable anonymously; private blocks require an existing Starter login with access to the owning organization. A gene is Markdown text. An ISI archive contains a root `isi.yaml` defining exactly one ISI named for its ID; a function archive contains a root `function.yaml` defining exactly one function and its parameters. Supporting files stay beside those definitions in the cache. File paths in a downloaded ISI's DNA assembly resolve from its block directory; shell commands still run from `SILICON_HOME`.

Starter functions can be called from the flow or another function without a separate local definition. They may also be imported with `functions: starter:function:greet`, or included in a list of function sources. Downloaded function bodies use the same arguments, results, catches, and send queue as [local functions](#functions-and-scoped-results). Their shell commands still run from `SILICON_HOME`.

### `access`

Every ISI must have an access entry, including an empty one. Entries name ISIs in the same Silicon. Unknown names and duplicate targets are rejected.

```yaml
access:
  coordinator: [researcher, worker.terminal]
  researcher: [coordinator]
  worker.terminal: [coordinator]
```

The server enforces these rules for internal sends and for inspecting or ending another ISI's sessions. An ISI may inspect itself and query its own archived sessions. Other sends to itself require an explicit self entry in its access list. The public management CLI and the trusted event flow act with interpreter authority.

These rules govern the `si` interface. They are not operating-system isolation between arbitrary programs running as the same user.

## Expressions, JSON, Bash, and fallbacks

Strings support CEL interpolation in unescaped `{...}`. Evaluation proceeds from CEL to Bash, where the source explicitly starts with `!`, and finally to the resulting string. A string received from a request that happens to begin with `!` is data; interpolation alone does not turn it into an implicit Bash command.

The runtime CEL environment contains:

| Name | Value |
| --- | --- |
| `request` | The event JSON for this flow invocation. |
| `silicon` | Compiled Silicon settings, with the Silicon token, app configuration, and Space Station table key removed at runtime. |
| `isi` | The configured ISI map. |
| `access` | The configured access map. |
| `var` | Variables created during this flow; starts empty for each event. |
| `args` | The current YAML function's arguments. |
| `self.result` | The current call's return value, scoped to its `then` branch. |
| `self.error` | Complete failure text, scoped to the current catch branch. |
| `self.for.index` | Zero-based index of the nearest active `for` loop. |

Compile-time expressions do not have a live request; `request` and `var` start empty. Required configuration fields cannot depend on an event that has not arrived. Runtime DNA and scheduled expressions also have an empty request object.

Supported helper functions include:

| Expression | Result |
| --- | --- |
| `{tz_time('2026-01-01T00:00:00Z', 'Asia/Kolkata')}` | `05:30:00 01:01:26 Asia/Kolkata` |
| `{convert_time(request.tings[0].data.sent_at, silicon.timezone)}` | Alias of `tz_time`, with the same RFC 3339 input and IANA timezone. |
| `{make_readable(request)}` | Serialize any JSON-shaped value as readable YAML, including nested objects, arrays, and nulls. Alias of `to_yaml`. |
| `{to_json(request.tings[0].data.raw).name}` | Parse a JSON string, then select a field. |
| `{to_yaml(request.tings[0].data)}` | Serialize JSON-shaped data as YAML using standard YAML indentation and newlines. |
| `{request.tings[0].data.to.startswith('worker')}` | The supported Python-style spelling of CEL's string-prefix helper. |
| `{request.tings[0].data.to.split('@')[0]}` | Split a string into a list. |
| `{request.items.groupBy(item, item.owner)}` | Groups as `{key, items}`, in first-key order with input order within each group. |
| `{request.tings.sortBy(ting, ting.id)}` | Stable ascending sort by a CEL key expression; equal keys retain input order. |
| `{request.tings.map(ting, ting.type).distinct().join(", ")}` | Keep first occurrences and join string elements. |
| `{request.tings.slice(0, 2).reverse()}` | Select a half-open range, then reverse it. Indices must be in bounds. |
| `{[[1], [2, [3]]].flatten()}` | Flatten one list level; `.flatten(2)` accepts an explicit nonnegative depth. |

Use CEL syntax, including `null`, `&&`, and `!=`. Python expressions such as `is not None` are not valid CEL.

Escape literal braces as `\{` and `\}`. YAML single-quoted strings are often convenient for CEL and backslashes. YAML double-quoted strings require their own escaping. Bash parameter forms such as `${NAME}` also contain braces; escape literal braces when they are intended for Bash rather than CEL. `$NAME` avoids that particular ambiguity.

For JSON variables, the interpreter recursively evaluates strings in YAML maps/lists and JSON container strings. Object keys are evaluated too; two keys that evaluate to the same name cause an error. A string result containing valid JSON is decoded, so `"123"` can become a number and `"{\"name\":\"Ada\"}"` can become an object. Subsequent CEL expressions can address that structure.

Fallback candidates are separated by unquoted `!>>` outside CEL. Each candidate is tried in order. A failure is logged; the next candidate does not receive `self.error` from the failed candidate. If every candidate fails, the error lists each one's complete reason in the order tried:

```yaml
message: ! ./message.sh !>> ! cat message.txt !>> "No message is available."
```

Inside a flow, exhausted fallbacks go to the action's catch branch, if present, or log and skip the action. Required compile-time settings still have to produce valid values; there is no usable configuration if its required identity or home cannot be evaluated. DNA entries and scheduling failures follow the behavior described above.

In block-style YAML, bare `!` command notation is accepted. Inside inline collections, quote the complete expression:

```yaml
assemble: ["! pwd"]
```

`assemble: [! pwd]` is rejected with a diagnostic because YAML would otherwise discard the anonymous tag. Multiline quoted strings and literal/folded YAML blocks are preserved by the parser. Avoid changing quotation just to silence an error without understanding whether it is YAML syntax, a CEL expression, a literal fallback, or shell argument quoting.

Bash runs as the current operating-system user, with the resolved home as its working directory. It receives `SILICON_HOME`, `SILICON_ORG`, and `ISI`. The runtime also gives Omni/provider sessions the configured `TZ`, `SI_URL`, and an ISI capability in `SI_TOKEN`.

## Event flow

The local webhook accepts any valid JSON value at `POST /` or `POST /events`: an object, array, string, number, boolean, or `null`. Its body becomes `request` unchanged. Requests must use `Content-Type: application/json` and fit within 1 MiB. The flow decides which fields its input requires.

Ting remains a supported input format:

```json
{
  "tings": [
    {"id": "event-1", "type": "dm.new_message", "data": {"message": "Hello"}, "metadata": {}}
  ]
}
```

Only an object containing exactly `tings`, with 1–100 valid records, opts into Ting ID deduplication. Each record needs a string `id` of 1–256 bytes, a nonempty string `type`, and object `data` and `metadata`. Previously accepted IDs are removed from those canonical batches. Other JSON—including an empty/malformed `tings` field or an envelope with extra fields—is preserved whole and does not participate in Ting deduplication.

A flow is an ordered list of steps. Function bodies, loop bodies, `then`, and `catch` use the same structure. Existing expression-generated steps/lists remain supported and are validated before execution.

| Action | Fields | Behavior |
| --- | --- | --- |
| `if` | `condition`, `then`, optional `else`, `catch` | Condition must evaluate to `true` or `false`. |
| `var` | `name`, `value`, optional `catch` | Store a JSON-compatible value under `var`. |
| `for` | `list`, `var`, `then`, optional `catch` | Run steps for each list item, accessible as `var.NAME`. |
| `switch` | `value`, `cases`, optional `default`, `catch` | Run the first matching case; each case has `case` and `then`. |
| `call` | `function`, optional `args`, `then`, `catch` | Call a named YAML function; its return value is `self.result` inside `then`. |
| `return` | Any JSON-compatible value | Finish the current function with that value. |
| `collect` | `name`, `value`, optional `catch` | Append to a list variable in the enclosing loop's parent scope; create the list if absent. |
| `continue` | `reason` | Log the reason and skip to the nearest loop's next item. |
| `break` | `reason` | Log the reason and leave the nearest loop. |
| `exit` | `reason` | Log the reason and finish the entire flow, including when called inside a function. |
| `send` | `isi`, `message`, optional `session_id`, `aggregate`, `catch` | Queue a message; `aggregate: false` requests immediate delivery. |
| `log` | `message`, optional `catch` | Append an entry to the Silicon log. |
| standalone `else` | A branch | Run only if none of the immediately preceding consecutive `if` steps matched. |

Consecutive `if` steps are independent: more than one can run. A trailing standalone `else` belongs to that consecutive chain. Any non-`if` action ends the chain. Use an `else` inside one `if` for a two-way branch, or `switch` for exactly one matching branch.

### Loops and collection

```yaml
flow:
  - var: {name: accepted, value: []}
  - for:
      list: '{request.items}'
      var: item
      then:
        - if:
            condition: '{!var.item.enabled}'
            then:
              - continue: {reason: 'Item {self.for.index} is disabled'}
        - collect:
            name: accepted
            value:
              owner: '{var.item.owner}'
              message: '{var.item.message}'
        - send:
            isi: trainer
            session_id: '{var.item.owner}'
            message: '{var.item.message}'
  - log:
      message: 'Accepted {size(var.accepted)} items'
```

Each iteration gets its own local variables, initialized from the enclosing scope; changes do not leak to the next item. `self.for.index` is zero-based and belongs to the nearest loop. Nested loops restore the outer index afterward. `collect` deliberately writes to the enclosing loop's parent scope, so the list remains available after that loop. An existing collection target must be a list. Ordinary message batching needs no collection variable: `send` already combines messages by destination.

For explicit grouping, `{request.items.groupBy(item, item.owner)}` produces a list of `{key, items}` groups. Keys may be any JSON value. Groups follow first occurrence of their key, and items retain input order. Iterate over the result with another `for`, using `var.group.key` and `var.group.items`.

```yaml
- switch:
    value: '{var.item.type}'
    cases:
      - case: message
        then:
          - log: {message: 'A message arrived'}
      - case: shutdown
        then:
          - exit: {reason: 'The sender requested shutdown of this flow'}
    default:
      - log: {message: 'Unrecognized item type'}
```

`continue` and `break` require an enclosing loop; `exit` finishes the flow without stopping the interpreter or disconnecting the Silicon. All three require a nonempty `reason`, support expressions in it, and log it. `exit` still flushes messages already queued by the flow.

### Functions and scoped results

The optional top-level `functions` section is separate from `flow`. It can appear before or after it; all definitions are registered before any step runs. Function bodies use `do`, parameters use `params`, and arguments are available as `args.NAME`:

```yaml
flow:
  - call:
      function: greeting
      args:
        name: '{request.name}'
      then:
        - log: {message: '{self.result.message}'}
        - var: {name: greeting, value: '{self.result}'}
      catch:
        - log: {message: 'Greeting failed: {self.error}'}

functions:
  greeting:
    params: [name]
    do:
      - return:
          message: 'Hello, {args.name}'
```

Each call has its own local `var` and arguments. Functions can use all flow steps and call other functions. `return` accepts objects, lists, scalars, or null; falling through without a return produces null. An unhandled function error reaches `call.catch` and skips its success branch. Functions share the enclosing flow's outgoing queue: returning or failing does not roll back messages already queued.

`then` exposes `self.result`; catches expose the complete error string as `self.error`. Nested calls/catches restore the outer context afterward. Neither result nor error leaks into later ordinary steps. Use `var` explicitly inside `then` when a later step needs the result. Outside functions, uncaught step errors are logged and the flow continues; inside functions they propagate to the caller. A send's catch handles evaluation and target-validation errors, such as an unknown ISI, missing required session ID, or failed message expression. Actual delivery failures are handled by the runtime, including with `aggregate: false`.

Definitions can also live in a separate file containing a bare function map:

```yaml
functions: ./functions.yaml
```

Or combine imports and inline definitions:

```yaml
functions:
  - ./functions.yaml
  - greeting:
      params: [name]
      do:
        - return: 'Hello, {args.name}'
```

Import paths resolve relative to the file that contains them, and imports may themselves contain a path or list of sources. Duplicate names and circular imports are errors. The interpreter reloads the flow and definitions when their files change, before the next request; YAML key order does not affect availability. Omit `functions` entirely when none are needed. Existing `!` commands, including `! python3 ./route.py`, remain available inside expressions.

### Send aggregation and delivery

By default, all flow sends to the same `(isi, session_id)` are combined, preserving their message order, and flushed when the flow finishes. Different destinations stay separate. No temporary grouping variable or second delivery loop is needed:

```yaml
flow:
  - for:
      list: '{request.tings}'
      var: ting
      then:
        - send:
            isi: coordinator
            message: '{make_readable(var.ting)}'
            catch:
              - log: {message: 'Invalid send: {self.error}'}
  - send:
      isi: audit
      message: 'Routing finished'
      aggregate: false
```

An immediate send first flushes older queued messages for the same destination, then sends its own message. It does not flush unrelated destinations. A flow can create a missing session-addressed session automatically; an internal CLI send to a missing persistent session still requires `--new`. Session-addressed ISIs require `session_id`.

The runtime persists outgoing delivery state before dispatch. `silicon.max_retries` defaults to `10`: ten retries after the first attempt. Set it to `0` to make only the initial attempt before stashing a failure. Failed groups remain on disk across restart, with attempts, failures, stashing, and recovery reported in logs. After exhaustion there is no timer-driven fallback: the next send to the same ISI and session triggers replay of its stashed messages before the new message. No message is redirected to another ISI or session.

Delivery checks wait for provider receipts (`START` or `INJECTED`), not completion of the model's turn; provider delivery has a 60-second deadline. A timeout after dispatch acceptance keeps that pending receipt for a later check instead of immediately submitting the same message again. Active provider work can receive flushed messages mid-turn. The ordinary send CLI retains its own dispatch response behavior; YAML flow aggregation applies to flow `send` steps.

### Durable acceptance and recovery

An empty HTTP 204 means the JSON request was durably accepted, before its flow runs. It does not promise that a provider received a message. Canonical Ting IDs are deduplicated across pending requests, completed requests, and restarts; generic requests are distinct deliveries even when their bodies are identical. Flow-loading and overall processing failures keep the request in the inbox for retry. Accepted requests survive interpreter restarts, and the complete body is logged on the first processing attempt with short notes on subsequent retries.

The inbox holds at most 10,000 pending requests or 256 MiB. A full inbox, failing storage, a Silicon still being restored, or interpreter shutdown returns 503 with `Retry-After`; the sender must retry. The response names the oldest blocked request and its error where available. Invalid JSON is rejected with 400; a valid JSON shape is never rejected just for lacking `tings`.

Outgoing journals are stored under `.silicon/outbox/<silicon-id>/<org>/<request-id>.json`, separately from the incoming inbox. A stable request ID and delivery slot track outgoing state across processing retries. The persisted message remains the delivery plan even if a timestamp or other expression changes on replay; changing that slot's destination raises an error rather than rerouting it. On reconnect, completed delivery receipts whose incoming requests are already gone are cleaned up, while undelivered messages remain. Crash recovery is at least once: a crash between provider receipt and persisting that receipt can still duplicate delivery. Other completed app/shell side effects may also repeat when a flow retries, so use application event IDs for idempotency. There is no transactional rollback of actions or queued sends. Flow nesting is limited to 64 levels.

### Flow migration

For the installation sequence, follow [Upgrading from 5.x](#upgrading-from-5x).

- Replace bare `{error}` in catches with `{self.error}`.
- Sends aggregate by destination unless `aggregate: false` is specified. That override also flushes earlier queued messages for its destination.
- Move delivery-failure handling out of `send.catch`; the runtime retries and stashes delivery failures. Keep expression/target error handling in catches.
- Read the payload shape your webhook actually receives. Ting publishers still send `request.tings`; other JSON payloads require no envelope.
- Native loops and YAML functions can replace generated step lists incrementally. No Python migration is required.

## CLI reference

Use `silicon --help`, `si --help`, and subcommand help as the primary command reference. `--json` is global on both commands.

### Interpreter commands

```sh
silicon compile PATH
silicon connect PATH
silicon disconnect ID_OR_PATH
silicon disconnect
silicon ls 'si:assistant*'
silicon logs show si:assistant
silicon logs show si:assistant --lines 200 --no-follow
silicon web
silicon web --no-open
silicon serve
silicon serve --port 1810 --no-proxy
silicon stop
silicon stop --force
silicon service status
silicon service install
silicon service restart
silicon service uninstall
silicon update
silicon install 'dm'
silicon uninstall 'dm'
silicon ping si:assistant
silicon config si:assistant
silicon settings get
silicon info
```

Without a command, `silicon` lists connections. `silicon list` is an alias for `silicon ls`. Quote globs so your shell does not expand them. `disconnect` without a target lists choices and prints the command to use; it does not disconnect everything.

`install` and `uninstall` operate on Honeycomb-managed applications using bare app IDs. `ping` checks the local interpreter's connection without prompting a model. `config` returns the connected configuration with credentials redacted. `info` returns version, protocol, source, documentation, and dependency details.

Application installation uses the current `SILICON_HOME`, or your ordinary home when it is unset. Set `SILICON_HOME=/path/to/home` when installing for a particular Silicon. Every connection installs each configured or registered canonical app ID through Honeycomb without a version argument, plus the IAM issuer and Ting. When Honeycomb cannot install the latest release but a working copy is installed, the failure is logged in full with `; continuing with the installed <id>` and the connection proceeds, so a restore after a reboot does not depend on the network. Only a configured app (or IAM or Ting) with no installed copy fails the connection; an app only the managed registry lists is logged and left out. Explicit `silicon install` also asks Honeycomb for the latest version even if a matching native command exists. Honeycomb owns package verification, installation, and updates; its registry lives beneath `<home>/.silicon/packages`. App login credentials remain under the original Silicon home. The interpreter preserves app update preferences. It restores Honeycomb's default once where an older interpreter forced this private home's `auto_update` off, and it repairs a home left without that required setting, which Honeycomb otherwise rejects as invalid configuration. Unrelated commands on the global PATH remain untouched; conflicting files inside the Silicon’s private command directory still cause an error. Legacy explicit executable commands are used as supplied. Authentication has its own 48-hour cache and is independent of installation.

Commands are exposed through owned links under `<home>/.silicon/bin`; the interpreter adds those to its command environment. `uninstall` removes packages installed in this managed Honeycomb home and matching interpreter links while preserving application credentials. Remove configured `apps` entries before reconnecting if you do not want an application installed again, or use `si app uninstall` from the running Silicon to update the YAML too.

`serve` stays in the foreground. `connect` normally starts it, through the installed service when there is one; see [Running unattended](#running-unattended). To run it under your own process manager, start `silicon serve` with `SILICON_SERVICE=external`. `--no-proxy` is a development mode with direct localhost access and no Caddy aliases.

`stop` asks the interpreter to stop and waits until it has exited (up to two minutes). `stop --force` sends SIGTERM, a second SIGTERM that ends an orderly stop at once and cleanly, and only then SIGKILL. A stopped interpreter comes back at the next login or boot, or with `silicon connect`; `silicon service uninstall` turns autostart off. `ls` shows each saved Silicon with its state: `connected`, `restoring`, or `waiting` with the error, attempt number, and next retry.

Public work commands specify the Silicon identity first:

```sh
silicon send si:assistant coordinator "Please review this"
silicon send si:assistant worker.terminal "Build it" --id build-17 --new
silicon sessions si:assistant worker.terminal
silicon sessions si:assistant worker.terminal --archived '*release*'
silicon show si:assistant worker.terminal --id build-17
silicon end si:assistant worker.terminal --id build-17
```

### Internal commands

```sh
si app install 'dm'
si app uninstall 'dm'
si auth setup dm
si auth setup 'hook --profile work'
si auth remove dm

si isi send coordinator "Please review this"
si isi send worker.terminal "Build it" --id build-17 --new
si isi ls worker.terminal
si isi show worker.terminal --id build-17
si isi end worker.terminal --id build-17

si session new --archive-current-session \
  --id completed-release \
  --title "Release prepared" \
  --description "What was done, decisions made, and work remaining."
```

`si` needs the ISI context supplied by the interpreter. A normal terminal without `SI_URL` and `SI_TOKEN` cannot impersonate an ISI by setting only `ISI`. The server derives the caller from its capability, not from user-provided target fields.

`si app install` installs through Honeycomb, authenticates the canonical app ID, and adds it to the calling ISI's `apps`. All ISIs can use the installed app. `si app uninstall` removes authentication, the managed package, app entries across ISIs and legacy `silicon.apps`/`login`, and its `app_configs` entry. IAM and Ting cannot be uninstalled through this internal command. App edits validate and atomically update the affected configuration blocks, retaining source expressions and the live flow. Add new app configuration directly to the YAML; reconnect to apply manual settings changes.

`si session new` applies to the calling persistent session. `--id`, `--title`, `--description`, and `--archive-current-session` are required. `--summary` is an alias for `--description`. There is no `si deliberate start-new-session` command in this release.

### Reading errors

A failure carries the words of whatever failed: an app CLI, Honeycomb, IAM, Ting, a Bash expression, Omni and its provider, Caddy, GitHub, or the installer. Nothing is summarized or truncated, and there is no need to rerun a command to see its diagnostic. A command that ran and failed reads as the command, its exit status, then its complete stderr and stdout:

```text
`bash -c 'ledger --json'` failed: exit status: 4
stderr:
ledger: quota exceeded
  at line 2
stdout:
{"ok":false,"code":"quota"}
```

An empty stream reads `stderr: (empty)`. A command that exited successfully but answered wrongly states the problem first, as in `` `dm iam --json` returned invalid JSON: expected value at line 1 column 1 ``, followed by the same status and streams. A command that could not start reads ``could not run `COMMAND`: `` and the operating system's reason. JSON an app prints is shown raw, exactly as printed. Parse errors keep the serde, YAML, or CEL text with its line and column, and configuration errors name the field and the value found. An HTTP failure carries the status and the response body. Each layer adds context before a colon, so the outermost words say what the interpreter was doing and the innermost are the tool's own. An exhausted fallback chain lists every candidate (`all 2 fallback candidates failed:` then `1.`, `2.`), and a second failure during reporting or cleanup follows on an `also:` line instead of replacing the first.

The same text reaches every reader:

- `silicon` and `si` exit with status 1 and print `Error:` and the complete text on stderr, including any `Caused by:` lines. `--json` changes successful output only.
- The control and internal APIs answer with `{"error": "..."}`. The dashboard shows that text whole, or the HTTP status and raw body when an answer is not JSON.
- A flow catch receives expression and validation failures as `{self.error}`; outgoing delivery failures appear in runtime logs and durable delivery state.
- `silicon.log` records it as an `[error]` entry. The file escapes newlines; `silicon logs show`, connection progress, and the dashboard unfold error entries so multi-line output reads as the tool wrote it, and `--json` returns the stored line. Errors logged during `connect` appear as they happen, even when the connection then succeeds.
- Failures with no caller, such as DNA refreshes, heartbeats, restoration, rejected Ting deliveries, and automatic updates, go to `silicon.log`. The last three also go to the interpreter's `daemon.log`.

Only literal credential values are replaced with `[redacted]`: the Silicon token, IAM SLTs, ISI capabilities, Space Station table keys, the JSON argument of `APP config set`, `app_configs` values of eight or more characters, recognized token prefixes such as `stk-`, and the values of credential-named JSON fields such as `access_token`, `password`, or `api_key`. The surrounding message always survives. Configured values shorter than eight characters, such as `en` or `true`, are settings and stay readable; masking every occurrence of them garbles the errors people must read. A credential expression (`silicon.token`, `space_station.table_key`, or an `app_configs` string) reports its exit status, stderr, and parser or CEL text, but its source reads `[credential expression]` and its stdout reads `(withheld; it is the credential)`.

Silicons read these errors through `si`, flow catches, and logs. Read the tool's own words and fix that cause: stderr usually explains the failure, and JSON-speaking tools often answer on stdout. When reporting a failure to a Carbon or another Silicon, pass the complete error text rather than a summary.

## Session behavior

Addressing and retention are separate choices:

| Mode | Sending | End-of-turn behavior |
| --- | --- | --- |
| Global + persistent | `si isi send NAME MESSAGE` reuses the active session or creates it. | Retains the session and conversation for later messages. |
| Global + ephemeral | `si isi send NAME MESSAGE` creates independent disposable work. | Returns final output to an internal caller, then discards recoverable session data. |
| Session + persistent | Use `--id`; add `--new` to create a missing session. | Retains each addressed session separately. |
| Session + ephemeral | Use `--id --title` to create; `--id` sends to an existing running address. | Returns final output to an internal caller, then archives the session under its id. |

Only global ephemeral work is discarded. A session-addressed ephemeral session is created and reached through an id its caller holds, so it keeps its session record and event history and is archived when it retires, exactly like a persistent session; reach it afterwards with `--archived`.

For ephemeral work, an automatic reply goes back to the ISI session that invoked it through `si`, if that caller still exists. An external event or management send has no calling ISI to receive this reply; inspect logs or progress instead. A global ephemeral call always starts independent work, while a session-addressed ephemeral call can address its currently running ID.

Creating session-mode ephemeral work requires a title even with `--new`. Existing running work accepts a titleless follow-up. A flow has no title field, so its automatic creation uses `session_id` as the title.

Persistent sessions have both a logical `id` and an immutable Omni `session_id` UUID. The UUID is used for on-disk filenames. An archive name is data, not a filesystem path.

### Archive and start a successor

`si session new` archives the current persistent session under the requested archive ID, title, and description. It preserves the original first timestamp and Omni session UUID and records the archive/completion timestamp. Duplicate archive IDs for that ISI are rejected.

For a global persistent ISI, the successor gets a new UUID-based logical ID. For a session-addressed persistent ISI, the successor keeps the old live logical ID; the archived predecessor gets the explicitly supplied archive ID. Both successors have a new Omni session UUID and fresh DNA. Authentication, Omni initialization, and DNA assembly finish before the rollover command returns.

Ephemeral sessions cannot create successors. A rollover does not copy the predecessor's entire prompt into the successor; durable memories and the new DNA provide continuity. Finish writing important context before asking to roll over.

### End, inspect, and query history

`show` returns a session record and up to 100 recent provider events, without sending a message. With no ID, it selects the most recent active record. An explicit logical ID or Omni UUID can select an active or archived record.

`end` stops active work without creating a successor. A persistent session is archived; an ephemeral session is discarded. A restored persistent session can be ended without starting its provider. Omitting `--id` ends all active sessions of that ISI; use a logical ID or Omni UUID to select one.

Archived sessions are queried explicitly:

```sh
si isi ls worker.terminal --archived
si isi ls worker.terminal --archived 09:09:2026
si isi ls worker.terminal --archived 01:09:2026-09:09:2026
si isi ls worker.terminal --archived '*release*' 01:09:2026-09:09:2026
si isi send worker.terminal "Explain the deployment decision" --id completed-release --archived
```

An archived send accepts the archive's logical ID or its Omni UUID. It opens the archived conversation without converting it into a new active session. The answer returns to the calling ISI. The worker retires when its query is idle, while the archive and its conversation history remain.

Bare `--archived` selects the previous 72 hours. With explicit filters, the search covers all archive history. Filters intersect: a result must match every supplied date/range and text filter. Dates use `DD:MM:YYYY`; both ends of a range are inclusive. Text filters search title and description case-insensitively and support `*` and `?`. A plain keyword can match a substring. Filters apply to the archive timestamp, using the Silicon's configured timezone unless `--timezone IANA` overrides it. They do not use the invoking shell's `TZ` by default.

## IAM application contract

The interpreter owns orchestration of authentication. IAM issues short-lived application tokens. Each app exchanges and stores its own access/refresh tokens and maintains them afterward.

Applications from all ISI `apps` lists, implicit Ting, legacy `silicon.apps`, and legacy `silicon.login` are checked on YAML connection, new ISI session creation, and before heartbeats, only when their last successful automatic check is at least 48 hours old. Check timestamps are saved per app, full Silicon identity, and owning organization in `.silicon/auth-checked.json`; reuse also requires a matching selected organization grant, so reconnects and interpreter restarts can reuse valid checks. Previously unchecked apps are checked immediately; failed checks do not advance their timestamps. Every app is attempted: an automatic check that fails is logged in full under the app's name, does not stop the connection or session, and is not retried automatically for 10 minutes. An app still signed in under another identity or organization whose new grant cannot be minted is signed out and its grant dropped; if it cannot be signed out, the operation fails as before. Explicit `si auth setup` and `si app install` fail closed. Ordinary messages to existing sessions do not trigger checks. Explicit `si auth setup APP` always bypasses the timestamps. Canonical app IDs resolve through Honeycomb to installed CLI commands. Apps added with `si auth setup` are remembered in the Silicon's managed-app registry and join those checks. A currently authenticated app can be reused only with a current matching grant; a changed identity or organization requires a fresh IAM grant. Removing an app from the dynamic registry does not override a configured entry; that app can be authenticated again at the next eligible boundary once its cached check expires.

The supported discovery contract is:

```sh
APP iam --json
```

It must succeed and return a JSON object with a valid bare string `app_id`, for example `{"app_id":"dm"}`. Extra public discovery fields are allowed. Secrets must not be returned as ordinary discovery data.

The interpreter probes status in this order:

```sh
APP login status --json
APP auth status --json
```

One must provide a boolean `authenticated`. A false state can be reported with a nonzero exit code. A true state must also have a successful exit status. Invalid JSON, an absent boolean, or a transport error must not masquerade as successful authentication.

When authentication is needed, Silicon invokes IAM with its complete identity, owning organization (`silicon.org_id`), and selected application organization (`SILICON_ORG`, defaulting to `silicon.org_id`):

```text
iam --output json --org OWNER_ORG silicon-login --sid si:HANDLE --stk SILICON_TOKEN --app-id APP_ID --grant-org SELECTED_ORG
```

For IAM versions that expose the flag, Silicon adds `--approve-scopes` after detecting support in local command help. IAM must return `slt` and a positive `expires_in` of at most 120 seconds. Only IAM receives the long-lived Silicon credential. Depending on the working status contract, the interpreter then invokes exactly one of:

```text
APP login SLT
APP auth token SLT
```

It checks that the app reports `authenticated: true` afterward. It does not retry a different login spelling with the same potentially consumed one-use token. A failed login or mint reports the command, its exit status, and its complete stderr and stdout, with the SLT, the Silicon token, and other credential values replaced by `[redacted]`.

Removal supports `auth remove`, `auth logout`, or `logout`, selected through command help, followed by a false-status check. `si auth setup` returns the public app ID; it does not return the SLT or application tokens.

Application commands receive `SILICON_HOME`, `SILICON_ORG`, and the common isolated IAM home. `SILICON_ORG` supplies their default organization; `ISI` is optional context. App state should live under `SILICON_HOME` in an app-owned hidden directory. The interpreter sets `SILICON_IAM_HOME` to `<SILICON_HOME>/.silicon-iam` so it does not borrow the user's personal Carbon IAM session. A symlink used to redirect that credential directory is rejected.

For test worlds, the selected IAM environment and each application's paired test environment must agree. Merely importing an application into IAM does not create the app backend's own test plane. The real app test-plane creation APIs may require an authorized production control-plane identity; do not infer test success from `iam --json` alone.

Initial 3.5 verification exposed an IAM defect: the issuer created unscoped Silicon grants that its shared OAuth subject-authority function rejected. [IAM PR #19](https://github.com/teamofsilicons/silicon-iam/pull/19) fixed that function and is now merged and live at `b5b5537`. On 10 September, the unchanged public 3.5.0 bundle successfully authenticated all six apps in the retained production organization. Briefcase, DM, Hook, Remind, and Waveform also completed logout and reported unauthenticated afterward.

Commit's original published 0.1.0 CLI has no logout command. [Commit PR #1](https://github.com/teamofsilicons/silicon-commit/pull/1) is merged, and its CLI passes production and hosted testing logout through the unchanged interpreter, including checks that previous access tokens become inactive. The 3.5.1 bundle includes this merged revision; the 3.5.0 bundle contains the original CLI.

All six paired hosted testing environments now pass login. [Commit PR #2](https://github.com/teamofsilicons/silicon-commit/pull/2), merged and externally deployed as `cbe3cd1`, stores and selects the encrypted imported IAM test-app credential. The retained Commit sandbox was paired through its production-owner API; its version advanced from 1 to 2 while both linked environment keys were preserved. The unchanged public interpreter and corrected Commit CLI then passed login, authenticated status, a protected todos read, logout, and previous-token inactivity. The other five apps passed login/logout with their released CLIs. Existing unpaired Commit environments still require their owner or authorized manager to supply the matching imported app credential.

Separate Silicon homes isolate application state, but an application's local relay may also need a distinct TCP port. A DM testing login saved valid credentials and then failed because a production-test relay still occupied its port. A retry with a separate available relay port passed; the owned test relays were stopped afterward.

### Ting registration and app notifications

Proactive IAM apps publish notifications through [Ting](https://ting.teamofsilicons.com/). Ting handles app notification delivery and registration; application CLIs no longer need their own `webhook` or `unhook` commands. The local endpoint also accepts other JSON webhook callers.

The interpreter installs `ting` through Honeycomb and authenticates its CLI just like other IAM apps. After the Silicon route is ready, it inspects `ting webhook list --limit 100 --json` and registers with `ting webhook http://HANDLE.OWNER_ORG.localhost/events --json`. A saved hook is reused with `--id WEBHOOK_ID`; the stable ID is retained across reconnects and restarts. Ting delivers `tings` batches to that endpoint with an empty HTTP 204 acknowledgment after durable acceptance.

Disconnect runs `ting unhook WEBHOOK_ID --json`. It still removes the local connection and ISI capabilities if unhooking fails, reporting the cleanup error. A failed registration rolls back the local connection and routing. Ting owns remote delivery, retries, and its local bridge; app-specific transport daemons are not registered with Silicon.

Apps that accept configuration expose `APP config set JSON`. The interpreter sends each configured object after authentication. If the app rejects it, the error names `silicon.app_configs.APP`, shows the command with its JSON argument as `'[redacted]'`, and includes the app's exit status and complete stderr and stdout, with configured values masked as described in [Reading errors](#reading-errors). Apps store credentials and configuration in their own state beneath `SILICON_HOME` (or their chosen remote configuration service), respect `SILICON_ORG`, and work when `ISI` is absent.

## Settings and telemetry

Settings apply to the interpreter and are stored in `settings.json` beneath its state directory. Both settings default to enabled:

```sh
silicon settings get
silicon settings get telemetry
silicon settings set telemetry --off
silicon settings set telemetry --on
silicon settings set auto_update --off
```

| Setting | Controls |
| --- | --- |
| `telemetry` | TOS-owned Space Station telemetry from this installation. |
| `auto_update` | The interpreter's hourly stable-release check and automatic bundle activation. |

`SILICON_TELEMETRY=0` and `SILICON_AUTO_UPDATE=0` also disable their corresponding interpreter features. Keys in `settings.json` that this release does not know are ignored and kept when `silicon settings set` writes the file, so a newer release's setting never stops an older interpreter. App CLIs may have their own telemetry and update settings; these are documented by each app's `--help`.

TOS telemetry uses separate destinations for interpreter/CLI/daemon activity, Silicon runtime and session activity, and the documentation frontend. Events include the source, operation, timestamp, version, Silicon identity, ISI context where applicable, and diagnostic context. Silicon inputs, flow logs, provider/session events, tool or command information, and `silicon.log` error entries with their complete tool output can be included, masked as described in [Reading errors](#reading-errors). Telemetry records diagnostic activity; it does not replace durable session history or local logs. Space Station queues records durably. A short-lived CLI command waits at most 100 ms during flush and may exit with records still queued; the running interpreter's client drains that queue, so delivery need not be immediate.

To also send one Silicon's activity to your own Space Station table, add:

```yaml
silicon:
  # ...the other required fields...
  space_station:
    table_name: assistant-events
    table_key: REPLACE_WITH_YOUR_TABLE_KEY
```

`table_key` authorizes ingestion; `table_name` identifies the configured destination. `silicon settings set telemetry --off` disables TOS telemetry and keeps this explicitly configured destination active. Remove `space_station` and reconnect to stop the user destination. The Space Station Rust client handles buffering, its local spool, delivery, and retries. When Space Station itself is a managed app, the interpreter supplies its required organization during login.

The interpreter removes Silicon tokens, app configuration, and Space Station table keys from runtime CEL and public configuration responses. Known credential values, credential-shaped fields, and recognized token prefixes are redacted from diagnostic events, logs, and errors. Redaction is the only reduction: errors retain their complete text in the relevant CLI, `si`, dashboard, flow catch, or `silicon.log`, as described in [Reading errors](#reading-errors). Configured values shorter than eight characters are treated as settings and are not redacted. Compile-time Bash logs `running: [compile-time expression]` and its exit status, because runtime redaction is not yet registered; if it fails, the error shows the command with known credentials masked, its status, and both streams. A credential expression logs `running: [credential expression]`; its error shows its exit status, stderr, and parser or CEL text, but names the source `[credential expression]`, masks the source's words of eight or more characters wherever the parser quotes them, and never shows its stdout, which is the credential. Runtime Bash records its redacted command; setup additionally records stdout and stderr after each command finishes. Ordinary prompt and event content remains useful for diagnosis and can be included. Do not put credentials into ordinary message fields or command arguments intended for logs.

The documentation website has its own **Share documentation usage with TOS** checkbox in the footer. Its preference is saved in that browser's local storage and is independent of local interpreter settings. The page submits telemetry to its same-origin `/api/telemetry` endpoint; the destination credential stays on the server.

## Files, storage, and logs

The default interpreter state directory is `~/.silicon-interpreter`. `SILICON_INTERPRETER_HOME` selects another directory, useful for an independent test installation.

| Location | Contents |
| --- | --- |
| Interpreter `daemon.json` | Running PID, chosen port, local management token, version, start time, and the supervisor that started it. Removed on orderly shutdown. |
| Interpreter `daemon.lock` | Held by the running interpreter; its first line is the holder's PID. |
| Interpreter `connections.json` | Every saved Silicon, whether connected or still waiting to be restored, until an explicit disconnect. One that does not parse is moved to `connections.json.corrupt-<UTC>`. |
| Interpreter `service.json`, `service.env`, `service.log`, `stopped` | The installed autostart service, the environment it gives the interpreter (0600), its supervisor's log, and a `silicon stop` marker for this boot. |
| Interpreter `update-state.json` | The update schedule, ETag, failure history, and a release that needs a person. |
| Interpreter `daemon.log` | Background interpreter stdout/stderr, including restoration, update, and rejected webhook delivery failures. Those failures also go to the affected Silicon's `silicon.log`; automatic-update failures go to every connected Silicon's log. |
| Interpreter `updates.log` | Bundle installer output for updates. |
| Interpreter `settings.json` | Telemetry and automatic-update preferences. |
| Interpreter `space-station/` | Space Station client state and durable telemetry spool. |
| Interpreter `caddy/` | Owned Caddy configuration, logs, data, storage, and `owner.json`/`caddy.pid` recording the running Caddy so a later start can stop one left behind. |
| `<SILICON_HOME>/.silicon/silicon.log` | Silicon event, flow, send, runtime, error, and provider log; rotated at 64 MiB. |
| `.silicon/heartbeats.json` | Wall-clock due times of this Silicon's heartbeats. |
| `.silicon/auth-apps.json` | Commands for configured/dynamically managed application authentication. |
| `.silicon/auth-checked.json`, `.silicon/auth-grants.json` | Recent authentication checks and granted organizations bound to the complete Silicon identity and owning organization; changing `SILICON_ORG` renews the application grant. |
| `.silicon/bin/` | Owned command links for applications installed through Honeycomb. |
| `.silicon/packages/` | Private Honeycomb package/authentication state, retaining Honeycomb's update settings. |
| `.silicon/apps.md`, `.silicon/app-descriptions.json` | Shared app DNA catalog and the last successful Honeycomb metadata for each app. |
| `<SILICON_HOME>/.fromstarter/` | Downloaded Starter genes, ISI/function definitions, and their supporting files; refreshed when the Silicon connects. |
| `.silicon/sessions/active/<isi>/<UUID>.json` | Durable active persistent-session records. |
| `.silicon/sessions/archived/<isi>/<UUID>.json` | Archive metadata. |
| `.silicon/sessions/events/<UUID>.jsonl` | Persistent-session provider events. |
| `.silicon/omni/<UUID>/` | Omni's session data and its daemon log. |
| `<SILICON_HOME>/.silicon/ting/<silicon-id>/<org>/` | `pending/` (one file per accepted JSON request, run oldest first), `seen.json` (ting IDs processed in the last 100 days), and `hook.json` (the retained webhook ID). An earlier `inbox.json` is moved into these on first use. A damaged file is moved to `NAME.corrupt-<UTC>` and reported, and the inbox carries on. |
| `<SILICON_HOME>/.silicon/outbox/<silicon-id>/<org>/<request-id>.json` | Durable outgoing flow messages, delivery attempts, and undelivered groups retained for destination-triggered replay. |
| `<SILICON_HOME>/.silicon/org.json` | Default organization propagated to app commands. |
| `<SILICON_HOME>/.silicon-iam/` | Isolated IAM CLI configuration/state for this Silicon. |

Session metadata includes the logical ID, Omni UUID, ISI, title, description, first/last timestamps, archive timestamp, status, normal-message count, and suggestion counters. Persistent data is saved atomically through a private temporary file and rename, with filesystem synchronization. State directories use mode 0700 and newly written state/log files use mode 0600 on Unix.

Short private aliases under `/tmp/silicon-<uid>/` keep Omni socket paths within macOS's Unix-socket limit; session data remains under the Silicon home. Caddy gets a separate private temporary admin-socket directory. The interpreter never contacts the machine's default Caddy admin API.

Global ephemeral work does not leave recoverable session records, event-history files, or its Omni session directory after retirement. Session-addressed ephemeral work is retained: it writes a session record and an event-history file and is archived when it retires. Discarded work's operational activity can still appear in the append-only Silicon log. “Discarded” is a retention choice, not a promise that no log of the work exists.

Log entries have this shape:

```text
[type] [origin/process] [UTC timestamp] [message]
```

The origin names who produced the entry — `interpreter`, an ISI name, or an app — and is followed by the process that wrote it: `daemon` for the interpreter started by `silicon serve`, `cli` for any other `silicon` or `si` invocation. Both append to the same file, so the suffix tells apart, for example, the configuration compile `silicon connect` performs before contacting the interpreter from the interpreter's own compile of the same YAML. Entries written before 4.0.9 carry no suffix.

Embedded message newlines are escaped so an entry stays on one line. `[error]` entries keep a failing tool's complete output; `silicon logs show` and the dashboard unfold them onto indented lines, while `--json` returns the stored line. `silicon logs show ID` prints the latest 100 entries and follows new ones. On a terminal, the type/origin prefix is colored, and the Silicon ID and log location remain in a footer. Ctrl-C restores the terminal and exits the viewer. `--no-follow` prints the tail and exits.

With `--json --no-follow`, the result is an object with `silicon`, `path`, and `lines`. With `--json` while following, each line is emitted as an independent JSON object containing `silicon`, `path`, and `line`. The follower handles appends, rotation, file replacement/truncation, and partial lines.

Logs rotate at 64 MiB: the file moves to `NAME.1`, older copies shift up, and a new file starts. `silicon.log`, `daemon.log`, `caddy.log`, and `updates.log` keep five copies; each session's Omni `daemon.log` and `.silicon/sessions/events/<UUID>.jsonl` keep two, and `si isi show` reads the event history across a rotation. A torn line in the event history (a full disk, a power cut) is shown beside the rest with the parser's reason instead of failing the command.

## Local dashboard and HTTP interface

`silicon web` opens the direct localhost dashboard with the current interpreter token in the URL fragment. `--no-open` prints that URL. The dashboard consumes the fragment, removes it from the displayed URL, and stores the token in browser session storage. Treat the printed URL and stored token as a management credential.

The dashboard can list, compile, connect, disconnect, send events/messages, inspect/end sessions, search archives, start successor sessions, authenticate/remove apps, and read/follow logs. Its log following polls the latest tail; the CLI follower is the continuous file-oriented view.

The underlying control API is `POST /control` with `Authorization: Bearer <interpreter-token>` and a JSON body of `{ "action": "...", "args": {...} }`. The internal API is `POST /si` with the corresponding ISI capability. Events use `POST /` or `POST /events` on a connected Silicon hostname.

Requests require `Content-Type: application/json`. JSON webhook requests are limited to 1 MiB; control and internal requests are limited to 16 MiB. Cross-origin browser requests are rejected. During shutdown/restart, requests are rejected with 503 so callers can retry after the interpreter resumes. More than 256 requests in flight also get 503. The CLI never sends its requests through `HTTP(S)_PROXY`, and quick actions such as `ping` and `list` time out after 15 seconds. Every interpreter error answer is JSON `{"error": "..."}` whose text is the complete failure, masked only for credential values; see [Reading errors](#reading-errors).

| HTTP status | Typical cause |
| --- | --- |
| 200 | Control or internal CLI operation completed. |
| 204 | A valid JSON webhook request durably saved for processing. |
| 400 | Invalid JSON, unreadable body, configuration error, or failed control/runtime operation. |
| 401 | Invalid management token or ISI capability. |
| 403 | Cross-origin browser request. |
| 404 | Unknown route/host; Caddy also rejects non-loopback peers and unknown hosts. |
| 405 | Unsupported method. |
| 413 | Body over its endpoint limit. |
| 415 | Missing/incorrect JSON content type. |
| 503 | Interpreter stopping or restarting, the Silicon still being restored, its webhook inbox full, or its storage failing. Sent with `Retry-After`. |

## Security and lifecycle boundaries

Silicon configuration is trusted executable input. Bash scripts and inference-provider tools run with your user permissions. The `access` map and ISI capabilities restrict the supported internal API; they do not isolate malicious code running under the same operating-system account. A process that can read the Silicon's configuration or private state is inside that trust boundary.

The interpreter listens on loopback, and the owned Caddy configuration only forwards loopback clients for known hosts. Unknown hostnames and non-loopback peers are rejected, including forwarded-header attempts to impersonate loopback. The event endpoint trusts that local boundary rather than a per-event Silicon bearer token. Publishing or forwarding that endpoint publicly changes the security model and is not a supported installation step here.

The runtime omits the Silicon token from CEL's `silicon` object and removes `SILICON_TOKEN` and `SILICON_INTERPRETER_TOKEN` from the provider environment. The ISI receives only its own `SI_TOKEN` capability for internal API access. IAM credential state is scoped to the Silicon home, and the application receives a short-lived, app-specific token instead of the long-lived Silicon credential. Protect the original YAML and all credential-bearing files accordingly.

Explicit `silicon stop`, SIGINT, SIGTERM, or SIGHUP stops the interpreter's owned workers, tools still running, and Caddy, removes the active daemon descriptor, and leaves saved connections and persistent state for restoration. The orderly stop is bounded so it finishes within the 90 seconds supervisors allow; a second SIGINT or SIGTERM (not the same signal repeated within two seconds) exits at once. This is a stop request, not a promise to let every model turn finish. App-owned relay daemons and saved authentication belong to those applications; disconnect and interpreter shutdown call Ting’s `unhook`, retaining the hook ID. Stopping preserves saved connection configuration so startup can restore and re-register the same hook.

On startup, the interpreter answers at once and restores each saved YAML path in the background: recompiled and reconnected, one Silicon at a time, each failure retried as described in [Running unattended](#running-unattended). Persistent session records are loaded when needed; provider processes are initialized lazily. A configuration file that was moved, removed, or made invalid keeps failing restoration, visibly in `silicon ls`, without being edited or forgotten by the interpreter.

Caddy updates use its dedicated private admin socket. Accepted routes are persisted; a failed update retains or restores the previous routes. Cleanup stops only the Caddy process owned by this interpreter, recorded in `caddy/owner.json`, and only while its command line still matches. Existing unrelated Caddy installations are not reconfigured or stopped; if another server answers port 80, routing reports that server's full response and stays down until it stops.

## Running unattended

### Autostart

The first `silicon connect` from an installed release, using the default interpreter directory, sets up autostart and says what it did:

| Platform | Mechanism | Starts | After a crash |
| --- | --- | --- | --- |
| macOS | LaunchAgent `~/Library/LaunchAgents/com.teamofsilicons.silicon.plist` | At login | launchd restarts it (at most every 30 seconds) |
| Linux with systemd | User unit `~/.config/systemd/user/silicon.service`, enabled, plus `loginctl enable-linger` | At boot (at login when lingering is not allowed) | systemd restarts it after 10 seconds |
| Linux without a systemd user manager | Crontab `@reboot` and five-minute lines running `silicon service ensure`, which starts the built-in supervisor `silicon service run` | At boot, when a cron daemon runs | The supervisor restarts it, waiting 5 seconds doubling to 5 minutes |
| Windows | Per-user logon task `Silicon Interpreter <your SID>` running `silicon-service.exe`, registered by `install.ps1` | At logon | The helper restarts it, and its `wsl.exe` session keeps the Silicon distribution running |

```sh
silicon service status
silicon service install
silicon service restart
silicon service uninstall
```

`status` shows the mechanism, its state, the last exit, and the logs to read. `uninstall` turns autostart off for good: `silicon connect` does not set it up again until `silicon service install`. `SILICON_NO_SERVICE=1` skips the automatic setup. Source and `cargo` builds, and a non-default `SILICON_INTERPRETER_HOME`, never get a service from `connect`; `silicon service install` still installs one for a non-default directory on request. On Windows, `install.ps1 -NoService` removes the logon task and remembers that choice; `install.ps1 -Service` turns it back on.

`silicon stop` stops the interpreter now; the service starts it at the next login or boot, or at the next `silicon connect`. An interpreter that an earlier `connect` started in the background hands over to launchd, systemd, or the logon task once no work is running: the service is started first and waits, then the old interpreter exits and the supervised one restores every saved Silicon. If the service cannot start (for example a Mac reached over SSH with nobody logged in), the running interpreter keeps running and says why.

Supervisors start programs with a minimal environment. `~/.silicon-interpreter/service.env` (mode 0600) holds `PATH`, `LANG`, `LC_*`, `SHELL`, and the `SILICON_`, `OMNI_`, `HONEYCOMB_`, `IAM_`, `TING_`, `SPACE_STATION_`, `ANTHROPIC_`, `OPENAI_`, `CLAUDE_`, `CODEX_`, and `GEMINI_` variables of the terminal that installed the service, refreshed whenever `silicon connect` starts the interpreter; the login shell's `PATH` is added when it starts. Edit it and run `silicon service restart` to change what the interpreter sees. The supervisor writes to `service.log`; the interpreter writes to `daemon.log`.

On macOS, a LaunchAgent runs only after someone logs in to the Mac's desktop: over SSH the agent loads at the next login, and a headless Mac needs automatic login, which FileVault prevents. macOS shows a "Background Items Added" notice, and the agent appears under System Settings › General › Login Items, where turning it off stops autostart. Programs launchd starts cannot read `~/Desktop`, `~/Documents`, `~/Downloads`, iCloud Drive, or `/Volumes` without Full Disk Access; keep YAML files and homes elsewhere, such as `~/silicon`. `connect` warns when one is there. On Linux without systemd, starting at boot needs a running cron daemon; on WSL without systemd, add `[boot] command=service cron start` to `/etc/wsl.conf`. Encrypted disks that must be unlocked at boot, and laptops that sleep, pause a Silicon until the machine is unlocked or awake.

### What recovers by itself

- **Restore.** Saved Silicons are restored in the background, each one attempted before any is retried. A failed restore (the network not up yet, an app service down, a volume not mounted) is retried after 5 seconds, doubling to 10 minutes, with ±20% jitter, forever; its error is logged in full when it first happens and when it changes. `silicon connect` of a waiting Silicon retries it at once, and `silicon disconnect` forgets it. Events for a Silicon still being restored get 503, so the sender can deliver them later.
- **Ting registration.** A Silicon whose Ting registration fails stays connected and keeps its inbox; registration alone is retried with the same backoff and re-asserted every six hours.
- **Routing.** A Caddy that cannot start does not stop the interpreter. Caddy is checked every five seconds (its process, its admin socket, and about once a minute that it alone answers port 80) and restarted with backoff from 1 second to 5 minutes. A Caddy left by an interpreter that crashed is stopped at the next start.
- **The interpreter itself.** An error accepting connections rebinds the same port; if that keeps failing for five minutes, the interpreter stops in order and exits non-zero so its supervisor restarts it. A panic in a background loop is logged and the loop continues. An Omni daemon left by an interpreter that crashed is stopped, never adopted. The soft open-file limit is raised to the hard limit (at most 65,536). A `connections.json` that cannot be read is left in place and the interpreter exits, so its supervisor retries.
- **Sessions.** Session-addressed workers idle for `SILICON_IDLE_SESSION_MINUTES` (default 60) stop their Omni daemon and provider; the next send, heartbeat, or reply resumes the same Omni session. Global ISIs are never retired. A turn that hears nothing from Omni for `SILICON_TURN_STALL_MINUTES` (default 360, counted in awake time) fails its session with the full picture, which also releases an update waiting for idle.
- **Flow delivery.** Outgoing messages are persisted and retried up to `silicon.max_retries` times after the initial attempt (default 10). Exhausted groups stay stashed; the next send to the same ISI/session triggers replay. Other destinations are isolated.
- **Disk.** Logs rotate (see [Files, storage, and logs](#files-storage-and-logs)), old releases are pruned, and each Silicon's webhook inbox is capped.

### Time limits

Every tool the interpreter runs has a time limit. At the limit its whole process group gets SIGTERM, then SIGKILL five seconds later, and the failure shows everything the tool printed, followed by a `silicon: stopped after …` line. Tools still running when the interpreter stops are ended too.

| Kind | Limit | Override |
| --- | --- | --- |
| `!` Bash expressions: DNA, heartbeats, credentials, flow values | 5 minutes | `SILICON_EXPRESSION_TIMEOUT_SECS` |
| `silicon.setup` scripts | 30 minutes | `SILICON_SETUP_TIMEOUT_SECS` |
| Honeycomb installs, updates, and removals | 20 minutes | `SILICON_INSTALL_TIMEOUT_SECS` |
| IAM and application CLIs | 2 minutes | `SILICON_APP_TIMEOUT_SECS` |
| Ting CLI | 60 seconds | `SILICON_TING_TIMEOUT_SECS` |
| Unattended update installs | 30 minutes | `SILICON_UPDATE_TIMEOUT_SECS` |

Expressions that CLI commands such as `silicon compile` evaluate in your terminal have no limit, so a credential command there can still prompt.

## Updates

Managed installations check GitHub's latest stable release while the interpreter is running: 5–10 minutes after it starts when the last check is over an hour old, then hourly with ±10 minutes of jitter. The schedule, ETag, and failure history persist in `update-state.json`; an unchanged answer (304) does not count against GitHub's rate limit, and rate-limit waits are honored. Drafts, prereleases, malformed version tags, and versions no newer than the installed release are not installed. When a newer release is already installed (after `silicon update`), the interpreter restarts into it without downloading it again.

The updater uses the same embedded installer and checksum-verified complete bundle as public installation. It clears source-build/release-mirror overrides before the update install. The installer separately downloads Honeycomb’s latest official release and verifies its checksum. Its logs are in the interpreter state directory's `updates.log`.

After successful automatic activation, the interpreter first runs the new release's `--version`; a release that fails this check is not started, the current one keeps running, and the check is repeated hourly. It then waits for active dispatches, nested activity, and pending provider work to finish, closes the admission gate, stops its owned children, and executes the newly installed interpreter, falling back to the release it was running if that cannot start. Connections and durable sessions are restored from disk. Incoming work is rejected once restart begins. Continuous active work can postpone the restart; a waiting restart is reported after an hour, then daily.

A failed install of a release is retried after 1 hour, 2, 4, and so on up to 7 days. Each distinct error is reported in full once, in `daemon.log` and every connected Silicon's log, and otherwise once a day; a missing network is reported only after it has lasted a day. Unattended installs are limited to 30 minutes and are stopped when the interpreter stops. `updates.log` rotates like the other logs.

`silicon update` performs a manual check and install and asks the running interpreter to restart into the new release as soon as no work is running. When the installed release is already current but the running interpreter is older, it asks for the same restart.

Set `SILICON_AUTO_UPDATE=0` in the interpreter's environment to disable periodic updates. Source builds remain under your control and cannot use the managed-prefix update path. The interpreter, its CLI, Omni, and Caddy can change together; Honeycomb apps update independently; YAML files, memories, workspaces, archives, and application state are never part of a release payload.

## Troubleshooting

Start with the complete error. It already contains the failing command's exit status and output, or the parser's position and the value it rejected; see [Reading errors](#reading-errors).

| Symptom | Check or action |
| --- | --- |
| `silicon` is not found | Add the installation prefix's `bin` to `PATH`; inspect the installer's printed path. |
| Release bundle unavailable | Check access to GitHub and the requested version's release assets. Use the versioned command above or the source-install path for development. |
| Checksum mismatch or missing binary | Keep the current installation; investigate/re-download the release. The installer fails before selecting incomplete payloads. |
| Port 80 cannot bind | Check for another server. On Linux the error lists `ip_unprivileged_port_start`, the `getcap` output, and whether Caddy's filesystem is mounted `nosuid`; install libcap tools and let the installer grant the bundled Caddy capability, or run `sudo sysctl net.ipv4.ip_unprivileged_port_start=80`. A service unit must not set `NoNewPrivileges=`. Use `serve --no-proxy` for direct development access. |
| Interpreter did not use 1823 | Read `silicon web --no-open`, startup output, or the private daemon descriptor; it selected a lower free port. |
| `.localhost` does not resolve in a client | Test with `curl --resolve HOST:80:127.0.0.1`; do not assume Caddy edits DNS. |
| Original template will not compile | It contains placeholders and invalid example expressions. Create your own corrected file; see the template notes below. |
| `SILICON_HOME must exist` | Create the intended directory and check resolution relative to the YAML file. |
| Missing/unknown configuration fields | Use the four required top-level mappings and the exact documented field names; canonical mode names are preferred. |
| Bare `!` inside an inline collection | Quote the complete expression, for example `["! pwd"]`. |
| CEL parse error | Check braces, string quotes, and CEL syntax. Use `null`, not Python's `None`; only registered helper functions are available. |
| No authenticated provider or unknown model/provider | Verify Omni's installed/authenticated providers and model keys with its own help/tools. Configuration compilation alone is not provider readiness. |
| Omni startup failed | The error quotes omnid's exit status and what it wrote during this run; the full log is `.silicon/omni/<UUID>/daemon.log`. Also check `PATH`, `OMNI_DAEMON`, and the provider's own installation/authentication. |
| Flow acknowledged but model is still working | HTTP 204 means durable acceptance before flow processing. Flow sends aggregate until flush; runtime delivery checks wait for provider receipts, not final inference output. Inspect progress/logs. |
| Event retry repeats an action | Canonical Ting IDs are deduplicated; generic JSON requests are distinct. Durable outgoing state prevents replay of recorded deliveries, but a crash before persisting a receipt may duplicate a send. App/shell actions have no flow transaction; use event IDs for idempotency. |
| Session send needs an ID | The target uses `primary_send_mode: session`. Supply `--id`; use `--new` for a missing persistent session. |
| Ephemeral session has disappeared | Expected after completion for a `global` + `ephemeral` ISI, which is the only combination that is discarded. Give the ISI `primary_send_mode: session`, or use a persistent ISI, when later recovery/querying is required. |
| Archive search seems empty | Bare `--archived` is only 72 hours. Supply explicit filters for older history and check the Silicon timezone. |
| `si` lacks context or capability | Run it inside an interpreter-created ISI. Setting only the `ISI` variable is insufficient. |
| An ISI cannot reach another ISI | Check its `access` list and the target spelling. Cross-Silicon sends are not supported by `si`. |
| App discovery/status fails | The error shows the app command, its exit status, and its stderr and stdout, including any JSON it returned. Fix what the app reports; update an app that lacks `APP iam --json` or a supported status form. |
| IAM cannot mint the SLT | Check the Silicon ID/token, app ID, permissions, and isolated IAM environment configuration. A personal Carbon login is not a substitute. |
| App rejected the SLT | Fix the app/test-plane issue and start authentication again; the old one-use SLT may already be consumed. |
| Removed app logs in again | Use `si app uninstall` or remove it from every ISI `apps` list (including Starter overrides), legacy `silicon.apps`, and legacy `silicon.login`, then reconnect. Ting remains implicitly managed. |
| Setup failed | Read the reported `silicon.setup` index, exit status, and stderr/stdout in the error. Each shell fallback needs `!`; completed shell effects remain even when connection fails. |
| Honeycomb cannot resolve a configured app | The error shows each candidate command passed over with its reason, and Honeycomb's exit status and output if Honeycomb itself failed. Check the bare app ID, CLI discovery, package availability for this platform, and the selected IAM organization. |
| Space Station asks for an organization | Install the CLI through Honeycomb and supply `--org ORG` or `SPACE_STATION_ORG` when invoking it directly; see the upstream issue ledger. |
| Disconnect reports cleanup errors | The Silicon/capabilities are removed; inspect Ting’s hook state and Caddy/interpreter logs. |
| A saved Silicon shows `waiting` in `silicon ls` | Read its error there: the interpreter retries it by itself. Fix what it names (a moved YAML, a failing setup script, an app that cannot install) and run `silicon connect` to retry at once. |
| The interpreter did not come back after a reboot | Run `silicon service status`. On macOS it starts only after a desktop login; on Linux without lingering it starts at login; without systemd it needs a cron daemon. Read `service.log` and `daemon.log` in the interpreter directory. |
| `silicon stop` did not stop it | `silicon stop --force` ends an orderly stop that is stuck. To keep it from starting at the next login or boot, run `silicon service uninstall`. |
| A tool failed with `silicon: stopped after …` | It reached its [time limit](#time-limits). Fix what hangs, or raise the limit with the matching `SILICON_*_TIMEOUT_SECS`. |
| Another server answers port 80 | Routing stays down until that server stops; the error quotes what answered. |
| Automatic update did not restart yet | Inspect update logs and active work. Restart waits until the interpreter can safely stop admitting work, and a failed check of the new release keeps the current one. |
| Automatic update needs Linux privileges | The installer exited 77. Run `silicon update` in a terminal as a user who can use `sudo` so a changed Caddy binary can receive port 80 capability (on Windows, rerun the Windows installer). |

Useful environment switches are `SILICON_INTERPRETER_HOME` for interpreter state, `SILICON_CADDY` for the Caddy executable, `OMNI_DAEMON` for the Omni daemon executable, `SILICON_AUTO_UPDATE=0` and `SILICON_NO_SERVICE=1` for development runs, the `SILICON_*_TIMEOUT_SECS` [time limits](#time-limits), `SILICON_IDLE_SESSION_MINUTES`, and `SILICON_TURN_STALL_MINUTES`. They affect the process in which they are set; an already-running daemon does not inherit new variables from a later terminal command.

## The repository's reference template

`stemcell/silicon`, `stemcell/memories`, and `stemcell/workspace` are reference/testing material. The installer does not distribute them into user homes, and they still require your own credentials and supporting scripts.

`stemcell/silicon/silicon.yaml` is the supplied source of intent, with its CEL expressions corrected and its helper functions supported. It is not a ready-to-connect configuration. Its current issues include:

- `silicon.id`, `silicon.org_id`, `silicon.token`, `SILICON_ORG`, and the optional Space Station fields are `...` placeholders.
- It refers to missing scripts/files including `install_python.sh`, `contacts.sh`, `tools.sh`, `team.sh`, `time_delay.sh`, and `learn.sh`. The repository has `CONTACTS.md`, `tools.md`, and `learn.md`; those names do not make the scripts exist automatically.
- Some suggestion messages still show old command forms. Current rollover uses `si session new --archive-current-session --id ... --title ... --description ...`.
- Its flow uses native `for` steps for ordered per-ting routing, keeps each item in `var.ting`, and aggregates messages by destination.

Correct a separate copy for your deployment. Compilation can report expression syntax before reaching the placeholder checks because syntax validation runs before any compile-time shell command. The absence of one particular placeholder error does not make the reference configuration valid.

## Development and compatibility

The repository contains the Rust interpreter and internal CLI under `src/`, a Windows launcher under `windows/`, integration checks under `tests/`, and this documentation under `docs/`. [The source on main](https://github.com/teamofsilicons/silicon-stemcell/tree/main), `silicon info`, and the command tree are the starting points for contributors. The `stemcell/` tree is reference material and is excluded from installation; tests create separate homes instead of rewriting it.

### Application integration checklist

An IAM app must expose `iam --json`, a short-lived-token login command, and a machine-readable authentication status. It owns refresh and access tokens, stores state beneath `SILICON_HOME`, treats `ISI` as optional context, and must not ask the interpreter or an ISI for a long-lived Carbon credential. Proactive apps publish to Ting; Ting alone manages interpreter webhook registration, batching, and retries. Apps with configuration expose `config set JSON`. Commands should explain their purpose and failure cause through a navigable `--help` tree.

Keep development and testing on the production authentication paths. An imported IAM test application must be paired with the app backend's test environment; a valid discovery response does not prove that login works. Add a consumer contract check that discovers the app, logs in with a real issued short-lived token, verifies authenticated status, exercises a protected read, removes authentication, and verifies the previous token can no longer be used. Never publish test secrets or local credential state.

### Contract versions

| Consumer or dependency | Silicon 6.1.0 contract |
| --- | --- |
| Existing 6.0.0 configurations | Compatible unchanged. Starter references and ISI `apps` are optional; legacy `silicon.apps` remains supported. |
| Existing 3.5 configurations | Migrate to `silicon.id: si:handle`, explicit `silicon.org_id`, and bare app IDs. `login` remains accepted. Remove `webhook` and `webhooks`; Ting is registered automatically. `sticky` and `archive_on_end` have been removed; replace them with `primary_send_mode` and `session_type` using the table above. |
| IAM application discovery | JSON bare `app_id`; additional public fields are permitted. Canonical IDs must match discovery before a command is trusted. |
| Authentication | Primary `login` / `login status --json`; legacy `auth token` / `auth status --json` remains supported. IAM is installed through Honeycomb without a version constraint; `--approve-scopes` is used only when its CLI exposes support. |
| Inference | Omni 0.9.1 Rust/client-daemon contract at `62c2adc57983be37c1de2064d169073bddf71291`. |
| Local interpreter API | Protocol `1`, reported by `silicon info`; protected `POST /control` and `POST /si`. |
| Honeycomb / Space Station | Latest standalone Honeycomb; Space Station CLI installed only when configured or explicitly requested. Interpreter telemetry uses its compiled Space Station Rust dependency. |

The Ting migration removes per-app webhook configuration. Existing `login` and legacy authentication spellings remain supported; no retirement date is set for those retained paths. A future removal or incompatible wire change must publish a migration and use a new contract version. The current checks negotiate CLI capabilities through command help and discovery; there is no general automatic upgrade negotiation between arbitrary client versions.

### Reporting a bug

Include the version from `silicon info`, the smallest configuration that reproduces the behavior, the exact command, expected and actual results with the complete error text, and sanitized logs. A fix PR is welcome but is optional:

```sh
silicon bug-report --title 'Short description' \
  --body 'Version, reproduction, expected result, actual result, and sanitized logs' \
  --pr https://github.com/teamofsilicons/silicon-stemcell/pull/123 \
  --dry-run
```

The dry run prints the report. Remove `--dry-run` to submit it to this repository using an authenticated `gh` CLI. `--pr` may be omitted. The command adds the Silicon version to the body. Do not include tokens, table keys, or credential files. Confirmed upstream defects are tracked in the [external dependency issue ledger](#external-dependency-issues) with their reproduction and impact.

### Telemetry development

Release builds provide the TOS interpreter and runtime table keys using `SILICON_INTERPRETER_TABLE_KEY` and `SILICON_RUNTIME_TABLE_KEY` at build time. Environment values with the same names override those defaults for a controlled deployment. Documentation ingestion has a separate destination. Table credentials are supplied through deployment secrets and are never placed in website JavaScript. The documentation sends to `/api/telemetry`; its server forwards permitted events to Space Station.

Use `SILICON_HONEYCOMB` to select a particular Honeycomb executable for an integration test. Use temporary homes and dedicated state directories for tests so personal IAM state, app credentials, and existing connections remain independent.

## Verification and requirement-to-evidence map

The historical 4.1.0 release has verified native bundles for macOS and Linux on ARM64 and x86-64, plus native Windows launchers. Windows x64 passes the complete WSL2 runtime and native-command suites. Windows ARM64 remains a preview because a physical ARM64 WSL2 run has not been completed. The release rows below distinguish platform, public installation, and dependency evidence; older rows retain historical checks.

The 3.6-series configuration and DNA regressions verify deferred setup, canonical app validation, optional telemetry settings, unchanged YAML bytes, legacy commands, and prompt source attribution. Version 3.6.1 carries the verified dependency corrections after the unpublished 3.6.0 candidate failed its release discovery gate; the original tag remains immutable. The release record below distinguishes candidate checks from completed publication and historical 3.5.0/3.5.1 evidence. Caddy-dependent integration tests run separately; `cargo test` alone does not verify them.

The recorded 3.6.0 local interpreter run passed 40 tests, with two Caddy integration tests explicitly ignored in that run; Clippy passed with warnings denied. The full protocol E2E passed with real Omni and Caddy, including the new 3.6 checks. Four credential-focused regressions also passed, including the two new compile-diagnostic tests. The documentation telemetry endpoint passed its Node regression. Installer regressions covered the new required binaries, Honeycomb failure retaining the existing release, and copying the native Space Station executable rather than a machine-specific launcher. Chrome layout checks covered desktop and a 390-pixel mobile viewport, including mobile navigation; all 60 internal documentation anchors resolved after the migration update.

The recorded 4.0.8–4.0.9 protocol E2E used the real pinned Omni daemon (0.8.0) and real Caddy, with a scripted Claude-compatible provider process for deterministic event behavior. It verifies the interpreter/Omni/Caddy protocol and lifecycle. Separately, a live run using the real authenticated `claude-code-cli` provider returned `SILICON_SMOKE_OK` and reached an idle session. That smoke test validates actual inference connectivity; it does not replace the deterministic concurrency/lifecycle assertions.

| Requirement | Evidence and scope |
| --- | --- |
| 6.1.0 local integration | 323 Rust tests passed (321 library tests and two integration tests), with nine helper tests ignored; formatting and Clippy passed. The complete real Omni/Caddy protocol E2E passed with a scripted provider. This verifies local integration; platform builds and public release evidence are recorded separately when complete. |
| 6.0.0 structured flow release | [Silicon 6.0.0](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v6.0.0) was published as latest stable on 29 September 2026 at 14:24 UTC from `5fd7140a7a68269e6e318704328fdc8345e9026c`. [All six platform jobs](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/36579624892) passed, including installed-bundle Omni/Caddy E2E on Unix and Windows x64 WSL2. All twelve release assets and eleven checksum entries matched the verified CI files. The native macOS ARM64 bundle reported Silicon 6.0.0, Omni 0.9.1, and Caddy 2.11.4; Windows ARM64 remains preview pending physical WSL2 verification. |
| 5.1.0 unattended release | [Silicon 5.1.0](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v5.1.0) was published as latest stable on 26 September 2026 at 14:12 UTC from `d760de06a8c7f1c6b8a60d3660158850ab8bd4de`. A [build-only run](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/36244680997) passed all six platform jobs on that commit before the tag existed. Its Windows x64 WSL2 suite first failed a three-second timing guard in the suggestion-cooldown check, because that runner handled provider events about ten times slower than a workstation; the rerun of the same artifacts passed, and the same test's event latency matched 5.0.2 locally. The [tagged run](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/36246696888) passed all six jobs at the first attempt, including each Unix installed-bundle Omni/Caddy E2E and the Windows x64 WSL2 and native-command checks with the new logon-task provisioning. All eleven published asset digests matched `SHA256SUMS`; `install.sh` and `install.ps1` (line endings aside) matched the tagged source. The macOS ARM64 bundle ran `silicon 5.1.0`, `omnid 0.9.1` and Caddy 2.11.4, and the public one-line installer installed 5.1.0 into a fresh prefix. Windows ARM64 retains its physical WSL2 testing boundary. |
| 5.0.2 full-error release | [Silicon 5.0.2](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v5.0.2) was published as latest stable on 25 September 2026 at 11:25 UTC from `733d9d689308cb47372373423844981d4a3a558b`. A [build-only run](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/36126396597) passed all six platform jobs on that commit before the tag existed, and the [tagged run](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/36127945769) passed them again, including each Unix installed-bundle Omni/Caddy E2E and Windows x64 WSL2/native-command checks. All eleven published asset digests matched `SHA256SUMS`; `install.sh`, `install.ps1` (line endings aside), both migration scripts and the migration procedure matched the tagged source. The macOS ARM64 bundle ran `silicon 5.0.2`, `omnid 0.9.0` and Caddy 2.11.4, and the public one-line installer installed 5.0.2 into a fresh prefix. Windows ARM64 retains its physical WSL2 testing boundary. |
| 5.0.0 public identifier release | [Version 5.0.0](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v5.0.0) was published on 23 September 2026 from `18d48112ae0285a8c5dc972fcec0f0296767218e`. It introduced the canonical identifier contract. Its existing assets remain unchanged; they are historical evidence, not verification of 5.0.1. |
| 4.1.0 Omni 0.9.0 and Ting delivery | [All six platform jobs](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/35743295086) passed on `66df3faa2be61543a336fa3f4e5bb632724a22b9`: four Unix installed-bundle Omni/Caddy suites, Windows x64 WSL2/native commands, Windows ARM64 launcher checks, Rust tests, formatting, and Clippy. Local validation passed 54 unit tests and the bundle-path integration test, including organization-switch grants, durable Ting batches, reconnects, and private app configuration. All nine release asset sizes/digests and eight checksum entries matched the verified CI files. IAM/Ting service behavior uses isolated CLI fixtures; Windows ARM64 physical WSL2 remains preview. |
| 4.0.3 release assets | [Version 4.0.3](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v4.0.3) contains six platform archives, two installers, and `SHA256SUMS`. All nine asset sizes and GitHub digests matched the verified CI files; all eight checksum entries and all six archive content/architecture checks passed. Every public asset URL returned HTTP 200 without authentication. |
| 4.0.3 public Unix installation | The exact public curl one-liner installed into a fresh macOS ARM64 prefix without source, version, or mirror overrides. All 34 installed payload files matched the verified native archive, including the intended compiled telemetry configuration; the installed interpreter reports 4.0.3. |
| 4.0.9 session retention and log attribution | [All six platform jobs](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/35516307701) passed on `468ff5d0842b0edb8e42b94a9f41dc09b14932b9`: native Unix installed-bundle Omni/Caddy E2E, Windows x64 WSL2/native commands, Windows ARM64 launcher checks, Rust tests, formatting, and Clippy. Local checks passed 49 unit tests plus the bundle-path integration test and the real Omni/Caddy protocol suite. All nine release asset sizes and GitHub SHA-256 digests matched the verified CI files; all eight checksum entries matched; every public URL answered with the expected size. A fresh macOS ARM64 public installation reported 4.0.9 and all 14 payload files matched the verified archive. Windows ARM64 WSL2 remains preview. |
| 4.0.8 Omni 0.8.0 | [All six builds and checks](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/35426793090) passed on `9d3c61cad1c05e1fb69f94a0b713b4a4edd9d10c` before any tag existed: native Unix installer/E2E with the Omni 0.8.0 daemon, Windows x64 WSL2/native commands, Rust tests, formatting, and Clippy. The unit test covers a provider removal with providers left, the last removal, and other configuration events staying quiet; the E2E resolves its scripted provider through a profile-free shell so the daemon's login-shell PATH probe cannot substitute a real CLI. Every archive reported `v4.0.8`, the seven-binary runtime inventory, and the Omni 0.8.0 notices; the macOS ARM64 bundle ran `silicon 4.0.8`, `omnid 0.8.0`, and Caddy 2.11.4 natively; all nine published assets matched the verified CI files by size and digest; and the local interpreter updated, restarted, and restored its Silicon on 4.0.8. |
| 4.0.7 package home repair | [All six builds and checks](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/35274873607) passed on `f87e7579d4bd600b3aa5bc0cb690dbd46340f9ec`: native Unix installer/E2E, Windows x64 WSL2/native commands, Rust tests, formatting, and Clippy. The unit test covers migration, repair, and unparsable configuration; the end-to-end connection starts from the package home 4.0.6 left behind, with the mock Honeycomb enforcing the real CLI’s required setting. The repair was also verified against an affected home using the real Honeycomb CLI, and the published release updated, restarted, and restored a live Silicon. |
| 4.0.6 independent applications | [All six builds and checks](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/35270308880) passed on `0116c15c31bb7564d55cff901bb008b4478fa9de`: native Unix installer/E2E, Windows x64 WSL2/native commands, Rust tests, formatting, and Clippy. The reconnect regression verifies an unversioned install on each connect with cached authentication. Local real-package checks verified latest app installation alongside existing PATH commands. |
| 4.0.3 source and Unix bundles | [Source checks](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/35197785502) and [all four native Unix jobs](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/35197918153) passed on `567894c23515396b14ef8d99354f9bb890cfa69b`: 40 unit tests, bundle-path integration, formatting, Clippy, complete bundle installation, real Omni/Caddy E2E, CLI execution, and live IAM discovery. Duplicate Windows jobs in the Unix workflow were cancelled after all four Unix jobs passed. Hook 0.6.1 is the latest verified Honeycomb package. |
| 4.0.3 Windows | [Windows x64 and ARM64 jobs](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/35198376479) passed on the same source. x64 includes actual WSL2 installation, full runtime E2E, and native-command checks. ARM64 includes native launcher checks and packaging; its WSL2 runtime remains preview. |
| Reference YAML and live connection progress | The shared evaluator supports `make_readable` and `convert_time`; the reference YAML regression executes every routing branch. Real webhook E2E verifies readable nested data through `var` and `send`. Gated authentication proves progress appears before authentication finishes, with success/failure markers, no captured credential output, and machine-readable JSON. Terminal rendering also has a unit check. |
| 4.0.0 public release | [Version 4.0.0](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v4.0.0) was published from `8fa368dda55c6a4982640595694fcb425cc84e37`. All nine uploaded files matched their GitHub sizes/digests, all eight checksum entries verified, and all six archives passed content and architecture checks. Every canonical public asset URL returned HTTP 200 with the expected size without authentication. |
| 4.0.0 public Windows installation | The exact public PowerShell `irm …/v4.0.0/install.ps1 \| iex` command passed on Windows x64 using PowerShell 5.1 in [the public installer run](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/35141926050). Download verification, WSL2 provisioning, real runtime E2E, browser launch, and native CLI checks passed. The ARM64 WSL2 job remains explicitly skipped until suitable hardware is available. |
| 4.0.0 native bundles | [All four Unix jobs](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/35139170050) passed on `83fbefe`, including interpreter tests, Clippy, real Omni/Caddy E2E, required CLI execution, and live IAM discovery. The workflow's duplicate Windows jobs were cancelled after the Unix jobs passed because Windows was tested separately. All 32 bundled application binaries match the latest verified Honeycomb packages exactly. |
| 4.0.0 Windows x64 | [Windows verification](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/35140947343) passed on `8fa368d` with the unchanged verified Linux runtime. Fresh PS5.1 installation, Ubuntu updates, missing-only WSL interoperability repair, full Omni/Caddy E2E, Unicode/quoted arguments, UNC paths, pipes, exit status, settings, browser launch, configuration redaction, and connection lifecycle passed. ARM64 native tests and packaging passed; ARM64 WSL2 remains unverified. |
| 4.0.0 public Unix installation | The exact public curl one-liner installed into a fresh macOS ARM64 prefix. All 34 installed files matched the verified archive. Same-version reinstall repaired missing Honeycomb/Space Station files and links while preserving a marker YAML, and the public installed bundle passed the complete real Omni/Caddy E2E. |
| Autostart on every platform (5.1.0) | `service::tests::launchd_install_writes_the_agent_and_bootstraps_it`, `launchd_without_a_gui_login_keeps_the_agent_for_the_next_login`, `systemd_install_enables_the_unit_and_explains_a_refused_linger`, `cron_install_merges_the_crontab_and_starts_the_supervisor`, `crontab_merge_is_idempotent_and_keeps_foreign_lines`, `plist_golden_and_valid_for_launchd` (checked with `plutil -lint`), and the portable supervisor tests `the_supervisor_restarts_a_crashing_serve_and_stops_after_a_clean_exit` and `supervisor_backoff_doubles_to_five_minutes_and_resets_after_a_healthy_run`. Every service command runs through a scripted runner; the real LaunchAgent was also exercised on macOS: after `kill -9` launchd restarted the interpreter in about 16 seconds with no orphaned Caddy, `silicon stop` stayed stopped, and uninstall removed the job. systemd, cron, and the Windows logon task are covered by unit tests and the Windows CI suite, not by a live boot. |
| Restore never forgets and never starves (5.1.0) | `server::tests::registry_changes_never_drop_saved_silicons_that_wait_for_restore`, `a_failed_restore_waits_its_backoff_and_reports_each_new_error_once`, `a_slow_failing_restore_does_not_keep_the_others_waiting`, `a_waiting_interactive_request_gets_the_lock_before_the_next_restore`, `a_restore_whose_ting_registration_fails_stays_connected_and_retries_ting_alone`, `a_corrupt_registry_is_moved_aside_whole_and_the_interpreter_starts_empty`, and `events_for_a_silicon_still_being_restored_ask_ting_to_retry`; the E2E waits for background restore after its restart. |
| Handover to a service starts it first (5.1.0) | `server::tests::a_handover_starts_the_service_first_and_stays_put_when_it_cannot` and `service::tests::installing_beside_an_unsupervised_interpreter_does_not_queue_a_second_one`. |
| Tools have time limits (5.1.0) | `process::tests::a_tool_past_its_limit_is_stopped_with_everything_it_said`, `a_tool_ignoring_sigterm_is_killed`, `a_background_child_holding_the_pipe_does_not_hold_the_answer`, and `a_stopping_interpreter_ends_the_tools_still_running`. |
| Caddy is supervised and its orphans stopped (5.1.0) | `server::tests::routing_that_cannot_start_is_retried_with_backoff_and_never_fails_a_caller`, `proxy::tests::an_earlier_caddy_that_accepts_stop_is_not_signalled`, `a_caddy_that_accepted_stop_is_waited_for_whatever_its_command_line_shows`, and the ignored real-Caddy tests. |
| Wall-clock heartbeats and idle retirement (5.1.0) | `runtime::tests::heartbeat_schedules_survive_restarts_and_a_clock_that_went_back`, `idle_session_workers_retire_and_resume_the_same_omni_session`, `an_ephemeral_reply_resumes_a_caller_retired_while_it_waited`, and `work_silent_past_the_stall_limit_is_reported_in_full`. |
| Updater backs off, reclaims locks, and prunes (5.1.0) | `update::tests::checks_are_scheduled_by_wall_clock_with_jitter_and_backoff`, `a_new_release_and_a_clock_set_back_reset_the_backoff`, `an_installer_past_its_limit_is_stopped_with_its_whole_group`, and `the_installer_reclaims_a_stale_lock_and_prunes_what_nothing_needs`, which runs the real `install.sh` functions in a temporary prefix. |
| Logs rotate and readers follow (5.1.0) | `tests::logs_rotate_at_their_cap_and_readers_follow_the_move` and `tests::a_silicon_log_past_its_cap_starts_a_new_file_and_keeps_the_old`. |
| Slow heartbeat cannot accumulate work | `runtime::tests::slow_heartbeat_coalesces_ticks_without_blocking_other_sessions` blocks a real protocol connection over several scheduler ticks, verifies independent-session progress, releases it, and verifies foreground delivery and no queued backlog. The final Windows WSL2 suite also passed heartbeat, suggestion, and busy-DNA checks. |
| Private bundle tools and Silicon home | `tests/bundle_path.rs` verifies actual native interpreter execution through a public symlink, bundled dependency discovery, home command priority, working directory, preservation of app update preferences, PATH deduplication, and refusal to trust an unmarked bundle directory. |
| 3.6.1 public release | [Version 3.6.1](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v3.6.1) was published as the latest stable release on 16 September at 17:19:29 UTC from `3f604bb1d99c2060b70377f6f8afe92693264475`. [All four native jobs](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/35124053286) and [source checks](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/35124052640) passed. All six asset sizes/digests and five checksum entries matched; each native archive contained the expected 34 payload files, architecture, installer, notices, and version. |
| 3.6.1 public installation and migration repair | The exact public curl installer passed in a fresh macOS ARM64 prefix. Reinstalling the same version repaired missing Honeycomb/Space Station executables, links, and notices while preserving a marker YAML. All 34 installed payload files matched the native CI archive, both dependency commands ran, and the public installed bundle passed the full real Omni/Caddy E2E. |
| 3.6.1 production documentation | [docs.teamofsilicons.com](https://docs.teamofsilicons.com) served the exact rendered 3.6.1 HTML, installer link, and migration instructions. Its production telemetry gateway returned HTTP 202 and accepted the verification event. |
| Real Space Station installation and auth | Honeycomb 0.2.0 anonymously installed public `tos>spacestation` 0.1.3 into a clean home. A separate freshly created Silicon home with no saved organization then passed real IAM SLT exchange, canonical-ID login, exact Silicon identity/org checks, logout, and false authenticated status against the retained production test identity. |
| DM's published replacement matches the live API | The native discovery gate rejected DM 0.3.0's old response-envelope parsing. Published DM 0.7.0 then passed fresh-home IAM issuance, interpreter login, exact identity/org, webhook registration/removal, logout, and final unauthenticated status; the dependency pin and notice were updated. |
| Briefcase's published replacement matches the live contract | Published Briefcase 1.1.0 reported matching client, server, and contract versions and discovered `tos>briefcase`. Real IAM SLT login returned the correct Silicon identity/org; authenticated status, logout, and final unauthenticated status passed without content writes. The pin replaces the incompatible 0.2.4 client. |
| Runnable documentation examples | The complete First Silicon example and a version combining all new fields both compiled in isolated homes. Setup stayed deferred and each YAML file remained unchanged; connecting requires the reader's real IAM and table credentials. |
| Required schema, canonical home, legacy mode mapping | `config::tests::load_resolves_home_and_validates_template_and_modes`; also checks full syntax before Bash and interval/message validation. |
| Deferred setup, canonical app IDs, telemetry schema, and unchanged source | `config::tests::setup_is_deferred_and_app_ids_and_telemetry_are_validated`; four configuration tests passed locally during 3.6.0 implementation. |
| DNA source attribution, computed contents, and failed-entry skipping | `runtime::tests::dna_includes_verbatim_sources_before_contents_and_skips_failed_entries` passed locally. |
| Preserve source flow order and shell notation | `config::tests::parser_keeps_shell_commands_and_order_without_rewriting_templates`; includes duplicate-key rejection, inline bang diagnostics, and multiline quoted preservation. |
| Deferred login/webhook command evaluation | `config::tests::load_expands_deferred_app_commands_without_invoking_them`; a real executable fixture remains uninvoked during compilation, including quoted paths/arguments. |
| CEL helpers, nested braces, JSON structures | `eval::tests::real_cel_nested_templates_helpers_and_failures`. |
| Bash, DNA, fallback ordering and context | `eval::tests::bash_fallbacks_and_dna_share_environment_without_losing_quoted_separators`. |
| Credential expressions show their failures but never their credentials | `config::tests::compilation_shows_credential_failures_but_never_credentials` and `eval::tests::credential_expressions_and_compile_diagnostics_do_not_disclose_secrets` exercise real Bash, stderr failures, malformed CEL, fallback errors, and interpolated credentials. Runtime command logging remains available. |
| Failures keep the tool's own words; only credential values are masked | `failure::tests::failures_keep_every_stream_verbatim_and_mask_only_credentials` and `failure::tests::short_settings_stay_readable_and_escaped_credentials_are_still_masked` cover the shared format and masking. Component checks cover Bash fallbacks (`eval::tests::failing_tools_report_status_stderr_stdout_and_every_candidate`), flow catches (`flow::tests::step_errors_reach_catch_and_log_in_the_tools_own_words`), IAM apps (`auth::tests::failing_apps_and_iam_show_exit_status_and_both_streams`), Honeycomb (`apps::tests::failing_honeycomb_and_apps_surface_status_stderr_and_stdout`), Ting (`ting::tests::failing_ting_calls_report_status_streams_and_answers`), HTTP answers (`server::tests::http_errors_carry_the_tools_words_and_mask_every_registered_credential`), and Omni (`runtime::tests::omnid_dying_at_startup_reaches_the_sender_with_its_status_and_output`). |
| A Silicon's Space Station destination works with TOS telemetry disabled | A live interpreter connection with vendor telemetry disabled wrote one runtime event to its configured user table; an actual table query verified the event and redacted token. |
| Documentation telemetry reaches its separate destination | The Vercel preview's real `/api/telemetry` accepted one event, and an actual `silicondocs` table query found the `release_smoke` row with version `3.6.0`. |
| TOS interpreter telemetry queues and drains | A standalone CLI queued its event durably; starting an isolated interpreter drained it and a daemon event. An actual `siliconinterpreter` table query found both records. |
| App command quotes, CEL, fallback scope, no compile-time invocation | `eval::tests::app_commands_preserve_argv_and_use_fallbacks_without_running_commands`. |
| Ordered matching conditions, catches, scoped errors | `flow::tests::flows_keep_json_variables_run_all_matching_ifs_scope_catches_and_continue`. |
| Syntax validation of unselected branches | `flow::tests::compile_rejects_invalid_unselected_branches_and_malformed_steps`. |
| Expression-generated flow | `flow::tests::flow_and_branch_expressions_produce_operations`. |
| CLI command shapes and intersecting archive filters | `cli::tests::command_shapes_and_archive_filters_preserve_scope`. |
| Log tail/follow boundary and prefix coloring | `cli::tests::tail_snapshot_follows_exact_read_boundary_and_colors_only_prefix`. |
| Flow delivery recovery | Runtime delivery failures are persisted/retried separately from flow catches; see the runtime and outbox tests. |
| Delivery acknowledgment precedes turn completion | `runtime::tests::receipts_ack_provider_delivery_before_end_and_native_next_turns_do_not_retire_early`; also `tests/e2e.py`. |
| Retry/late errors and listener recovery | `runtime::tests::retry_and_late_errors_preserve_receipts_and_failed_listeners_are_recreated`. |
| Retirement does not race a new send | `runtime::tests::retirement_rechecks_pending_work_after_waiting_for_send_lock`. |
| Restart admission gate respects nested active work | `runtime::tests::restart_gate_rejects_active_nested_work_then_rejects_new_dispatches`. |
| Restart waits for HTTP compilation and its response | `server::tests::http_compile_blocks_restart_until_its_shell_and_response_finish` runs an actual HTTP request and a blocked Bash expression. The same activity guard covers authentication requests and heartbeat preparation. |
| Suggestion thresholds/cooldown and archived worker retirement | `runtime::tests::suggestions_require_new_messages_and_cooldown_and_archived_workers_retire`; timed protocol E2E checks per-session heartbeats and suggestions. |
| Disconnect still removes capabilities after app cleanup failure | `runtime::tests::disconnect_ting_unhook_failure_still_removes_connection_and_capabilities`. |
| A stale connection cannot recreate workers after disconnect | `runtime::tests::stale_disconnected_connection_cannot_create_workers_or_capabilities`. |
| Delayed work cannot reach a replacement connection | `runtime::tests::reconnect_keeps_blocked_event_and_heartbeat_work_out_of_the_replacement` holds actual Bash work across reconnect, checks flow catches, and verifies a fresh heartbeat deadline. |
| Restored UUIDs cannot revive an old ISI capability | `runtime::tests::captured_caller_expires_when_its_worker_or_connection_is_replaced`; the shared request boundary checks the captured worker. |
| New ephemeral session titles and caller-relative disconnection | `runtime::tests::session_ephemeral_creation_requires_title_even_with_new_but_active_sends_do_not`; `tests/e2e.py` covers internal/control rejection, automatic flow creation, multiple working directories, missing files, and symlinks. |
| Archive timestamps and safe physical identity | `state::tests::archive_keeps_original_time_and_safe_disk_identity`; E2E verifies both persistent rollover modes and restart restoration. |
| IAM issuance/isolation/secret handling contract | `auth::tests::application_tokens_are_iam_issued_isolated_and_never_logged` uses controlled CLI fixtures. This is not evidence that all six real apps have authenticated successfully. |
| Caddy route constraints | `proxy::tests::only_local_dns_hosts_can_be_routed`. |
| Real Caddy routing, reload rollback, child cleanup | `proxy::tests::real_caddy_routes_reload_rollback_and_child_cleanup`, run separately with real Caddy; ordinary unit runs mark it ignored. |
| Port 80 denies non-loopback peers and spoofed forwarding headers | macOS integration test `proxy::tests::real_caddy_port_80_only_forwards_loopback_peers`, run with `SILICON_TEST_LAN_IP` and real Caddy; requires a free port 80. |
| Stable-version selection | `update::tests::only_newer_stable_releases_are_candidates`; isolated complete-bundle update checks cover rejection, activation, idle restart, busy restart, and resuming the same persistent UUID. Metadata transport and the hour-long wait are replaced only in that test copy. A separate 3.5.0 public-install check queried live GitHub HTTPS metadata and correctly reported that then-current version. |
| End-to-end protocol and lifecycle | `python3 tests/e2e.py`: port decrement, HTTP shape/errors, mid-turn injection, ISI environment/access, archives, ephemeral reply, heartbeat, suggestion limits, busy DNA refresh, session rollover, shutdown, restart restoration, disconnect, unchanged YAML bytes. |
| Actual inference | Separate live Claude smoke result: provider `claude-code-cli`, reply `SILICON_SMOKE_OK`, final status `idle`. Recorded delivery acknowledgment 27.82 s and total time 28.07 s in this run; these are observations, not latency guarantees. |
| 3.5.0 public release | [v3.5.0](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v3.5.0) was published from `3ac2922`. [All four native CI jobs](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/34344276712) passed interpreter checks, complete-bundle E2E, CLI execution, and IAM discovery. Downloaded archives matched the CI artifacts, exact payload inventory, tagged installer/notices, checksums, and GitHub asset digests. |
| 3.5.0 public one-line installation | The published installation command was run into a fresh macOS ARM64 prefix with all source/mirror overrides removed. All 30 payload files matched the verified CI bundle; the public installed interpreter passed the full protocol E2E. |
| 3.5.1 public release | [v3.5.1](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v3.5.1) is published from `1ad23cf`. The tagged runtime, installer, dependency inputs, tests, workflow, and notices match the four-platform build source below. Both [tagged-source checks](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/34459210895) passed, and all six uploaded assets match the verified local sizes and SHA-256 digests. |
| 3.5.1 public downloads and installation | All six canonical release URLs returned HTTP 200 without GitHub authentication; downloaded bytes, sizes, and SHA-256 digests matched the verified artifacts and GitHub metadata. The public installer also completed a fresh macOS ARM64 installation with source/version/mirror overrides removed and Cargo absent from `PATH`. All 30 installed payload files matched the CI archive; Silicon 3.5.1 and Commit logout help were verified. |
| 3.5.1 native bundles | [Build-only CI](https://github.com/teamofsilicons/silicon-stemcell/actions/runs/34453877850) built exact source `f3131299ea01613b8585ba9389fb1ad6abf8e140` on all four supported targets. Every job passed formatting, tests, Clippy, complete source installation, real Omni/Caddy E2E, CLI execution including Commit logout help, six IAM application-discovery checks, packaging, and artifact upload. The draft-release job was skipped. |
| 3.5.1 artifact contents | All four downloaded artifact ZIPs matched GitHub's SHA-256 digests. Each bundle contained the expected 30 regular payload files, required executable modes and native architectures, the 3.5.1 version marker, and exact source installer and notices. No Silicon YAML or runtime state was included. |
| 3.5.1 binary installation from local HTTPS | The actual macOS ARM64 CI archive was installed from a local HTTPS test endpoint into an isolated prefix with Cargo absent from `PATH`. All 30 payload files matched the archive byte for byte; Silicon 3.5.1 and Commit logout help were verified. |
| Documentation hosting for 3.5.0 | [docs.teamofsilicons.com](https://docs.teamofsilicons.com) served the static guide over verified HTTPS on Vercel; desktop/mobile navigation and layout were checked in Chrome. |
| Full real-app authentication | The released 3.5.0 interpreter passed all six production and hosted testing logins. Five released app CLIs passed logout in both environments. Commit's merged CLI fix also passed both cycles, including a hosted protected read and previous-token revocation after its backend fix went live and the retained sandbox was paired. The corrected CLI is included in the 3.5.1 bundle; it is absent from the 3.5.0 bundle. |

Reproducible development commands:

```sh
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings

# Supply the real executables; the E2E creates its own isolated work directory.
OMNI_DAEMON=/absolute/path/to/omnid \
SILICON_CADDY=/absolute/path/to/caddy \
python3 tests/e2e.py

SILICON_CADDY=/absolute/path/to/caddy \
cargo test --lib proxy::tests::real_caddy_routes_reload_rollback_and_child_cleanup -- --ignored
```

`SILICON_TEST_BIN_DIR` can select a built/installed interpreter directory for the E2E. It expects a real Omni daemon and real Caddy on `PATH` or through the overrides. It uses a free/available local test setup, creates its own YAML outside the template directories, and verifies that YAML's bytes remain unchanged. The LAN Caddy test additionally requires `SILICON_TEST_LAN_IP` set to this machine's non-loopback IPv4 address and port 80 available. Omni 0.8 starts CLIs on the `PATH` the user's login shell reports, ahead of its own, so the E2E points `SHELL` at a profile-free shell; the scripted provider is then resolved even when a real `claude` is on the developer's shell `PATH`.

Source references for maintainers: `src/apps.rs`, `src/failure.rs`, `src/settings.rs`, `src/telemetry.rs`, `src/config.rs`, `src/eval.rs`, `src/flow.rs`, `src/runtime.rs`, `src/auth.rs`, `src/state.rs`, `src/server.rs`, `src/proxy.rs`, `src/cli.rs`, `src/update.rs`, `src/dashboard.html`, `install.sh`, `.github/workflows/release.yml`, and `tests/e2e.py`. The specification is `UNDERSTANDING.md`; the preserved fixture is `stemcell/silicon/silicon.yaml`.

To verify complete bundles on all four native platforms before a release, dispatch the existing release workflow from a branch containing it, with an exact source commit:

```sh
gh workflow run release.yml --repo teamofsilicons/silicon-stemcell \
  --ref BRANCH -f tag=COMMIT_SHA -f build_only=true
```

Despite the historical input name `tag`, build-only mode accepts a source revision. It validates the installer against the source version, runs the same native builds and installed-bundle E2E checks, and uploads CI artifacts. It skips the draft-release job and does not create a Git tag. Normal release mode still requires an existing `vX.Y.Z` tag matching both the source and installer.

### Maintaining the documentation

The source is `docs/GUIDE.md`, `docs/DIARY.md`, `docs/EXTERNAL-BUGS.md`, and `docs/shell.html`. With Python Markdown installed, run `python3 docs/render.py` and commit the updated `docs/site/index.html`. An isolated alternative is `uv run --with markdown==3.8.2 python docs/render.py`. Vercel serves the committed static output and the telemetry API; it does not build or upload the interpreter, template homes, or local state.
