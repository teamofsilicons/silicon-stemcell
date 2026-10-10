# Silicon 7.0.0

A local interpreter for connected Silicons, powered by Rust and Silicon Omni.

## Start here

Silicon 7 uses [Silicon Accounts](https://accounts.teamofsilicons.com/) for identity, [Silicon Apps](https://apps.teamofsilicons.com/) for application installation, and the [Developer portal](https://developers.teamofsilicons.com/) for publishing. Every action belongs to a Carbon (`c:handle`) or Silicon (`si:handle`).

1. [Install Silicon and its dependencies](#installation).
2. [Create your first Silicon](#first-silicon) with its Silicon Accounts identity and token.
3. Run `silicon compile PATH`, then `silicon connect PATH`.
4. Open the local dashboard with `silicon web`, or use `silicon --help` for the command tree.

Existing installations should follow the [7.0 migration](#upgrading-to-70) before reconnecting. The [configuration reference](#configuration-reference), [application contract](#silicon-accounts-application-contract), and [HTTP interface](#local-dashboard-and-http-interface) describe the supported interfaces.

## What runs on your machine

Silicon is a local interpreter for a `silicon.yaml` file. One interpreter can connect several Silicons. Each Silicon has its own identity, home directory, internal Silicons (ISIs), access rules, event flow, sessions, and logs.

The `silicon` command manages the interpreter from your terminal. The `si` command is provided inside an ISI so it can communicate with permitted ISIs, inspect work, manage its session, install or remove applications, and ask the interpreter to authenticate an application. The local dashboard exposes the same management operations through the interpreter's control API.

The interpreter listens on `127.0.0.1:1823`. If that port is occupied, it tries 1822, 1821, and so on, down through 1024. A dedicated Caddy process provides `http://silicon.localhost` for the dashboard and `http://<handle>.si.localhost` for each connected Silicon whose handle is a DNS label. Other valid handles use a collision-free encoded host; use the hostname returned by `connect`. Caddy uses HTTP port 80; it does not search for an alternative proxy port.

Each active ISI session has an owned Omni daemon and uses Silicon Omni's Rust package. Omni starts and communicates with the selected inference provider. Silicon subscribes to Omni events, delivers new messages, assembles DNA, tracks sessions, and records progress. These are real provider processes; the interpreter does not simulate model output in normal use.

No terminal window is opened for each ISI. Each receives an independent process context, an Omni session, and the environment needed by its commands. Persistent session data survives stopping the interpreter. Global ephemeral work is discarded when it finishes.

## Installation

### Public binary installation

On Linux with Silicon Apps installed, use the catalog:

```sh
silicon-apps install silicon --yes
```

For complete setup, the installer below delegates Linux installation to Silicon Apps and uses the complete GitHub bundle on macOS. Windows uses the PowerShell bundle installer. Native catalog installation for macOS and Windows is not available yet. No Rust compiler is required:

```sh
curl -fsSL https://github.com/teamofsilicons/silicon-stemcell/releases/download/v7.0.0/install.sh | sh
```

Find platform bundles and checksums on the [v7.0.0 release page](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v7.0.0). The installer sets up Silicon, Caddy, Omni, and the Silicon Apps CLI. Apps install the latest configured applications independently and retain their own automatic updates.

Silicon keeps reusable Starter genes, functions, and ISIs, per-ISI app declarations, native YAML flows, persistent sessions, and durable notification delivery. See the [7.0 migration](#upgrading-to-70) for the identity and configuration changes.

The default installation prefix is `~/.local/share/silicon`. Add its `bin` directory to your shell's `PATH`:

```sh
export PATH="$HOME/.local/share/silicon/bin:$PATH"
```

For the default prefix, the installer adds this path once to the startup file for your detected shell. Open a new terminal, or run the printed export command in your current terminal. Set `SILICON_NO_PATH=1` to leave shell startup files alone. Custom prefixes print the path without changing startup files.

To choose another dedicated prefix, set the variable on the `sh` side of the pipeline:

```sh
curl -fsSL https://github.com/teamofsilicons/silicon-stemcell/releases/download/v7.0.0/install.sh \
  | SILICON_PREFIX="$HOME/tools/silicon" sh
```

The target matrix covers macOS, Linux, and Windows on ARM64 and x86-64. Windows runs the Linux bundle through WSL2. Native Unix bundles target macOS 15 and Ubuntu 24.04; the Linux binaries require glibc 2.39 or newer. Each platform requires its own build and runtime verification.

The binary installation needs `curl`, `tar`, and a SHA-256 verifier (`sha256sum` or `shasum`). Downloads use HTTPS. Before activation, the installer checks the bundle checksum, required members, version marker, and whether the interpreter and Omni daemon can execute. A complete release includes:

| Command | Bundled component |
| --- | --- |
| `silicon`, `si` | Interpreter and internal CLI, 7.0.0 |
| `omnid`, `silicon-omni`, `omni`, `so` | Omni 0.9.1 pinned to commit `62c2adc57983be37c1de2064d169073bddf71291` |
| `caddy` | Caddy 2.11.4 |

Silicon Apps is installed separately through its official installer; existing update preferences and services are preserved. Each Silicon connection runs `silicon-apps install 'app' --yes --json` for configured and registered canonical app IDs, the Silicon Accounts issuer (`silicon-accounts`), and Ting (`ting`). Omitting `--version` selects the latest public package each time; no app version is pinned. Apps are not copied into the interpreter bundle, wrapped to suppress updates, or given environment variables that disable their automatic updates. Omni and Caddy remain interpreter runtime dependencies.

Accounts, permissions, remote service availability, and authenticated inference providers must still be available. Installing a CLI does not create a Silicon Accounts identity or grant provider access.

### Upgrading from 5.x

Prepare these edits in a separate copy of your configuration while 5.x is running. Do not replace its active flow with 6.0 syntax: 5.x does not support `aggregate`, native loops/functions, or `self.error`. Stop the old interpreter before installing 7.0.0 and applying the edited configuration. These changes also apply to older configurations after their account and webhook migrations. The installer preserves YAML and does not translate it.

1. Replace bare `{error}` in every catch with `{self.error}`. Result and error values are scoped to their `then` or `catch` branch; save a value with `var` when it is needed later.
2. Review each send that must happen immediately or remain a separate message. Add `aggregate: false` to it; otherwise messages to the same `(isi, session_id)` are joined in order and sent when the flow finishes. An immediate send also flushes earlier queued messages for that destination.
3. Move transport-failure handling out of `send.catch`. It now catches expression and target errors only. The runtime persists outgoing messages, retries delivery, logs failures, and stashes exhausted messages. A later send to the same ISI and session replays the stash first; no timer or alternate destination is used. `silicon.max_retries` defaults to ten retries after the initial attempt; set it explicitly if another limit is needed.
4. Keep existing Ting flows on `request.tings`. Only change their input handling if sending another JSON shape; generic webhook bodies now reach `request` unchanged. Existing CEL-generated steps and `!` Python or shell commands remain supported, so adoption of native loops and functions can be incremental.
5. Run `silicon stop`, then `silicon update` while the interpreter is stopped. Apply the prepared configuration edits, check `silicon --version`, and compile each configuration with the new `silicon compile /path/to/silicon.yaml`. Then reconnect with `silicon connect /path/to/silicon.yaml`. Prepare all saved configurations before the first reconnect, since it starts the interpreter and restores its other saved Silicons.

Keep the interpreter stopped while preparing the migration. Incoming and outgoing journals survive restarts, but crash recovery is at least once; application side effects should use event IDs for idempotency.

### Upgrading to 7.0

Stop the old interpreter and back up the configuration and Silicon home before editing. Use a Silicon Accounts identity `si:handle` and its token. Delete the removed `silicon.org_id` and `silicon.SILICON_ORG` fields, and remove retired environment selectors from wrapper commands. App IDs are bare catalog handles such as `dm`; the Accounts issuer is `silicon-accounts`.

Keep the same YAML path and `SILICON_HOME` so session history remains in place. Install 7.0, run `silicon compile /path/to/silicon.yaml`, and reconnect. Existing application logins are checked against the complete Carbon or Silicon account. If an application is signed out or its session has expired, explicitly run `si auth setup APP` inside an ISI to sign in again.

The local event hostname now follows the account, such as `assistant.si.localhost`. Use the URL returned by `silicon connect` and update local callers. See the [migration procedure](https://github.com/teamofsilicons/silicon-stemcell/blob/main/docs/PUBLIC-IDENTIFIER-MIGRATION.md) for the steps.

### Ting migration

For configurations from 4.0.x or earlier, before reconnecting on 7.0.0:

1. Remove `silicon.webhooks` and `silicon.webhook`. Keep application IDs under `isi.<name>.apps`; legacy `silicon.apps` remains supported. Applications publish notifications through Ting and no longer register separate interpreter webhooks.
2. Change the flow to read `request.tings`, a list of notification objects with `id`, `type`, `data`, and `metadata`. Other JSON shapes are also accepted, but Ting publishers use the batch envelope. Use native `for` steps or CEL list helpers to process a whole batch; the [first Silicon example](#first-silicon) shows a minimal flow.
3. Check that the Silicon identity can authenticate Silicon Accounts and Ting. Both are installed through Silicon Apps automatically, even with an empty `apps` list. The interpreter registers only Ting at `http://HANDLE.si.localhost/events`, retaining its hook ID for reconnects.
4. Run `silicon compile /path/to/silicon.yaml`, then reconnect or restart the interpreter. The updater and installer do not rewrite your YAML.

Ting receives an empty HTTP 204 after the complete batch is durably saved, before the flow runs. Follow logs or session progress to inspect processing. The interpreter loads the latest flow and function definitions for every request. Other manual configuration changes require reconnecting; `si app install` and `si app uninstall` update connected app settings immediately.

### Windows installation and project files

Existing Windows installations should rerun this release's PowerShell installer to add the native `ting.exe` entry point. `silicon update` updates the WSL2 runtime but does not add Windows launcher files.

In PowerShell:

```powershell
irm https://github.com/teamofsilicons/silicon-stemcell/releases/download/v7.0.0/install.ps1 | iex
```

Windows uses a native launcher and a dedicated WSL2 distribution named `Silicon`, with the same Linux runtime bundle and independent Silicon Apps installation used on Linux. First-time WSL2 setup can require administrator access, hardware virtualization, and a restart; rerun the installer after completing that setup. Ordinary interpreter commands run as the unprivileged Linux user `silicon`.

Keep each project, its YAML, and its `SILICON_HOME` inside the distribution's Linux filesystem. Open `\\wsl.localhost\Silicon\home\silicon` in File Explorer and copy or create your project folder there. For example:

```powershell
silicon compile '\\wsl.localhost\Silicon\home\silicon\assistant\silicon.yaml'
silicon connect '\\wsl.localhost\Silicon\home\silicon\assistant\silicon.yaml'
silicon web
```

`SILICON_HOME: ! pwd` continues to mean the YAML's directory. Setup commands, relative executables, DNA scripts, application CLIs, and ISI sessions execute from that home. The launcher translates explicit Windows paths into WSL paths, but it never rewrites or silently moves your YAML. A configuration or home on a Windows drive is rejected because private credential modes and Unix socket semantics require Linux storage.

Windows data remains accessible: `C:\Users\You\Documents` is `/mnt/c/Users/You/Documents`, and `D:\Projects` is `/mnt/d/Projects`. Reads and writes affect the actual Windows files and respect Windows permissions. Put explicit paths to that data in your scripts as needed. Portable Bash and installed CLI commands can use the same YAML on all platforms; macOS-specific tools and options still need alternatives inside the scripts.

The Linux runtime can update through Silicon Apps; rerun the Windows installer to update the Windows launchers and browser bridge. If a later update replaces Caddy and needs to renew its port-80 capability, rerun the Windows installer; it performs that provisioning inside the dedicated distribution.

Windows ARM64 remains a preview pending a complete run on physical ARM64 Windows hardware. This migration checks native launcher builds and script syntax; neither Windows x64 nor ARM64 WSL runtime behavior has been reverified.



### Linux and port 80

On Linux, the installer checks whether unprivileged processes can bind port 80. If not, it grants only the bundled Caddy binary `cap_net_bind_service=ep` using `setcap`. This requires the system's libcap tools and suitable privileges; it can ask for `sudo` in an interactive terminal. Silicon itself does not need to run as root.

An automatic, noninteractive update cannot prompt for privileges. When the Caddy binary is unchanged, the installer can reuse the existing capability. If a new Caddy binary needs a capability and unattended privilege is unavailable, the installer exits with status 77 and leaves the current release selected. The updater reports this once with the command to run and does not retry that release until the installed release changes or a newer release appears. Run `silicon update` in a terminal as a user who can use `sudo` (on Windows, rerun the Windows installer). The installer adds `/usr/sbin` and `/sbin` to `PATH` so `getcap` and `setcap` are found under service managers.

### Build the complete installation from source

From this repository's root:

```sh
SILICON_SOURCE_DIR="$PWD" sh install.sh
```

Source installation requires Rust 1.98 or newer, Cargo, a C compiler, and dependencies needed by the pinned Rust crates. Release CI pins Rust 1.98.1. It builds the interpreter and Omni, downloads verified Caddy, and separately installs the latest Silicon Apps. Application CLIs are installed when a Silicon connects. Caddy's upstream checksum file uses SHA-512.

`SILICON_GIT_REV` is an alternative to `SILICON_SOURCE_DIR`: supply an exact, lowercase, 40-character Git commit. The installer fetches that commit and verifies the checkout. The two source selectors cannot be used together.

For controlled builds, `SILICON_DEPENDENCY_BIN_DIR` can supply trusted Omni and Caddy executables. This is a reuse mechanism, not independent verification of arbitrary local binaries. The interpreter is still built from the selected source, and Silicon Apps is still installed independently at its latest release. `CARGO_TARGET_DIR` can reuse a compilation directory.

For a developer build of only the interpreter and internal CLI:

```sh
cargo build --locked
./target/debug/silicon --help
./target/debug/si --help
```

This does not install the required runtime dependencies. Put the intended `omnid`, Caddy, Silicon Accounts, and application commands on `PATH`, or use the documented binary overrides. Automatic bundle updates do not replace an unmanaged `target/debug` or `target/release` executable.

### Activation and retained releases

Standard Silicon Apps installations keep the interpreter under `<home>/.apps/installed/silicon`, with command links in `.apps/bin` and the internal `si` alias. Silicon Apps manages activation and retained package versions.

The standalone bundle installer remains available for platform setup and custom prefixes. In that layout, installed commands are symlinks through `<prefix>/lib/silicon/current/bin`. A new release is prepared under the same prefix, checked, moved into `<prefix>/lib/silicon/releases`, and selected by replacing the `current` symlink. Existing unmanaged files in `<prefix>/bin` are not overwritten; choose another prefix if there is a conflict.

Only one installation may modify a prefix at a time. `.install-lock` records its owner (`pid`, when that process started and, on Linux, the boot it ran in). A lock over a minute old is reclaimed automatically, saying why, when its process is gone, was started before the last boot, or is a different process that reused the number; a lock whose owner cannot be checked (one left by an older installer) is reclaimed after two hours. Installers take turns reclaiming through `.install-lock.reclaim`; if an error says another installer is reclaiming and none is running, remove that directory.

For standalone bundle installations, old releases are pruned. After activation the installer keeps the active release, the one it replaced, the release of the interpreter running the update, and any release a running interpreter recorded in `<prefix>/lib/silicon/running/`; it removes other releases and staging directories older than an hour. There is no public rollback command.

The installer does not copy the repository's `stemcell` examples into a user's home. It never edits a connected `silicon.yaml`, memories, workspace, or session archive.

## First Silicon

Create your own directory and your own `silicon.yaml`. Keep it separate from the repository's reference fixture. The following is a complete schema example. Replace the identity and token with your values and ensure Omni has an authenticated provider that can satisfy the `fast` model key.

```yaml
silicon:
  id: si:assistant
  token: REPLACE_WITH_YOUR_SILICON_TOKEN
  timezone: Asia/Kolkata
  SILICON_HOME: ! pwd
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

Setup creates a workspace directory on each connection; `mkdir -p` is safe to repeat. Silicon Accounts and Ting are implicit dependencies, even with empty ISI `apps` lists. Add other application IDs under `isi.assistant.apps` when the corresponding Silicon identity and application permissions are ready. Add the optional `space_station` fields shown below only after creating your table and obtaining its key. `token` remains required so the interpreter can authenticate the Silicon with Accounts.

```sh
silicon compile /absolute/path/to/silicon.yaml
silicon connect /absolute/path/to/silicon.yaml
silicon ls
silicon send si:assistant assistant "Hello"
silicon show si:assistant assistant
silicon web
```

`compile` validates configuration and expressions without connecting to the interpreter. It does execute compile-time Bash expressions, including `SILICON_HOME`, and downloads missing [Starter blocks](#reusable-starter-blocks), so it is not a side-effect-free shell dry run. Local configuration, flow, and imported function syntax are checked before Bash runs. Starter files require the resolved home first; their syntax is checked after downloading and before evaluating the remaining compile-time fields. `setup`, application commands, DNA scripts, and runtime flow actions remain deferred. Provider startup, application authentication, and runtime request values have their own checks later.

`connect` starts the interpreter if necessary, refreshes referenced Starter blocks while compiling the file, runs setup, installs and authenticates managed applications, adds routing, applies app configuration, registers the local route with Ting, and records the connection. Its text output streams local setup, application installation, authentication, and webhook progress while the operation runs. In a terminal, `… Authenticating dm` changes to `✓ Authenticated dm`; failures use `✗`. Redirected output uses separate lines without terminal control codes. `--json` returns the normal complete JSON response with its progress records. Setup, installation, authentication, and Ting commands run with the [time limits](#time-limits) the interpreter enforces. A failed automatic app authentication no longer fails the connection: it shows as `✗` with the app's complete answer, and the app is checked again later (see the [Silicon Accounts application contract](#silicon-accounts-application-contract)). Duplicate Silicon IDs, duplicate canonical YAML paths, or two connected Silicons sharing the same canonical `SILICON_HOME` are rejected.

The local identity is the canonical YAML path. The global identity is `silicon.id`, the complete Silicon Accounts identifier. Moving a file changes its local identity. The interpreter reads `flow` and optional `functions` definitions from disk for each incoming request, without rerunning compile-time shell expressions. Disconnect and reconnect to apply other manual configuration changes. `si app install` and `si app uninstall` update the file’s app entries and the connected app settings immediately; ordinary connection and interpreter updates do not rewrite it.

`silicon disconnect ./silicon.yaml` resolves the path in your terminal's working directory. Direct control-API requests must supply a Silicon ID or an absolute YAML path. Delayed flow sends, heartbeat work, session capabilities, and ephemeral replies stay bound to their original connection or worker; reconnecting the same ID does not transfer them to the replacement.

Send an event through the Silicon's host:

```sh
curl --fail-with-body \
  --header 'Content-Type: application/json' \
  --data '{"tings":[{"id":"local-event-1","type":"new_message","data":{"message":"Hello from an event"},"metadata":{}}]}' \
  http://assistant.si.localhost/events
```

Caddy routes the hostname but does not edit DNS or `/etc/hosts`. If a particular client does not resolve `.localhost` names, the equivalent diagnostic command can add `--resolve assistant.si.localhost:80:127.0.0.1`.

## Configuration reference

The four required top-level keys are `silicon`, `isi`, `access`, and `flow`. `functions` is optional and may appear anywhere at the top level. Unknown fields are errors. Ordinary duplicate YAML keys are errors. Flow action order is preserved, including the reference dialect's repeated `if` keys, but the recommended form is an explicit list of steps.

### `silicon`

| Field | Meaning and validation |
| --- | --- |
| `id` | Required complete Silicon public ID, `si:handle`. The handle is 3–50 lowercase ASCII letters, digits, underscores, or hyphens; the prefix does not count toward that limit. |
| `token` | Required Silicon credential for Silicon Accounts. Empty strings, NUL bytes, and the exact placeholder `...` are rejected. It is not an ISI capability or a dashboard token. |
| `timezone` | Required IANA timezone, such as `UTC`, `Asia/Kolkata`, or `America/Los_Angeles`. |
| `SILICON_HOME` | Required existing directory, or an expression producing one. Relative paths resolve against the YAML file's directory. The final path is canonicalized. |
| `inference_providers` | Required nonempty list of provider names, `all-available-providers`, exclusions, or nested lists. |
| `setup` | Optional ordered list of shell commands. Runs once during each connection attempt; compilation validates expressions without running these commands. |
| `apps` | Legacy list of Silicon Apps application IDs, retained for existing configurations. New files declare apps under `isi.<name>.apps`; both contribute to the Silicon's managed apps. |
| `app_configs` | Optional mapping from managed app IDs to configuration objects. Native YAML numbers, booleans, lists, maps, and null retain their types; string values support private compile-time expressions. Applied with `APP config set JSON` after authentication. |
| `space_station` | Optional object with nonempty `table_name` and `table_key` for a user-owned telemetry destination. This destination remains enabled when TOS telemetry is turned off. |
| `max_retries` | Optional nonnegative integer, default `10`; retries after the first outgoing flow delivery attempt before stashing for the next send to that destination. |
| `login` | Legacy list of deferred Silicon Accounts application commands; retained for existing configurations. |

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

Each application ID is a bare handle of 1–80 lowercase ASCII letters, digits, underscores, or hyphens, beginning with a letter. Every app is owned by a Carbon or Silicon account. Select the public app ID from the [Silicon Apps catalog](https://apps.teamofsilicons.com/). Duplicate IDs within one list are errors; the same app may appear under several ISIs. The interpreter installs and authenticates the union of all ISI app lists, including those from [Starter ISIs](#reusable-starter-blocks), plus legacy `silicon.apps`. Apps are available to the whole Silicon: these lists organize configuration and do not enforce access restrictions between ISIs. `ting` is always managed and registered, even when omitted from `apps`. The removed `webhooks` and `webhook` keys are configuration errors; delete them and retain application IDs in `silicon.apps` or ISI `apps`. App configuration keys must name a managed canonical ID, including implicit `ting`.

Setup commands run from `SILICON_HOME` with `ISI=interpreter`. A leading `!` is accepted; the first command may also be ordinary shell text. Each shell fallback must have its own `!`; a quoted fallback is literal text. CEL is evaluated before Bash. Command stdout and stderr are captured in the connection log after each command finishes. A failed step's error names `silicon.setup[N]` and carries the command's exit status and complete stderr and stdout. Setup is connection work, so reconnecting runs it again. Make commands safe to repeat. Setup does not run during `silicon compile`, before each message, or on every DNA refresh. An exhausted fallback chain aborts the connection; completed shell side effects are not rolled back.

`login` contains executable names and arguments, not shell programs to run while compiling:

```yaml
login:
  - ! dm
  - 'hook --profile work'
```

This field expands CEL and `!>>` fallbacks, preserves argument quotes, and retains an original leading `!` to distinguish an explicit executable command from a bare managed app ID. The marker is removed before execution; it does not execute Bash during evaluation. For example, `! dm` identifies the command to which Silicon later appends `accounts --json`, or a login command; it does not launch bare `dm` during compilation. Command arguments are parsed with shell-style quoting and executed directly as argv. Shell pipelines, redirections, and environment assignments are not command wrappers; use a real executable wrapper when one is required.

### `isi`

Define at least one ISI. Names may contain ASCII letters, digits, `.`, `_`, and `-`, up to 128 bytes; `.` and `..` alone are not allowed.

| Field | Meaning |
| --- | --- |
| `model` | Required Omni model key, passed using `Ask::key`. A key such as `fast` or `code` is resolved by Omni. |
| `primary_send_mode` | Required `global` or `session`. It controls addressing, not retention. |
| `session_type` | Required `persistent` or `ephemeral`. It controls completion behavior, and retention together with `primary_send_mode`: only `global` + `ephemeral` is discarded. |
| `dna` | Required mapping with `assemble` and `next_refresh`. An empty assembly list is permitted; the internal instruction footer is still appended. |
| `apps` | Optional list of bare Silicon Apps application IDs. Installed and authenticated for the whole Silicon; an omitted app list does not restrict an ISI's app access. |
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

DNA also includes the managed apps' Silicon Apps names and descriptions, with a help command for each app. This catalog covers the Silicon's shared apps, regardless of which ISI declared them:

```text
App Name: <app name from Silicon Apps>
App Id: dm
CLI: run `dm --help` to know about it

About: <app description from Silicon Apps>
```

The catalog is saved as `.silicon/apps.md`. When Silicon Apps metadata cannot be refreshed, the interpreter logs the failure and keeps previously fetched details; apps without cached details still receive an ID and help command.

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

The interpreter requires Starter 0.3.0 or newer and installs or upgrades it through Silicon Apps before downloading blocks. An explicit `SILICON_STARTER` executable override must already meet that version. Silicon Apps installation requires access to its Starter listing. If that listing is unavailable to you, install the [public Starter CLI](https://github.com/teamofsilicons/silicon-starter/releases/latest) first:

```sh
curl -fsSL https://starter.teamofsilicons.com/install.sh | sh
starter --version
```

The installer verifies the downloaded archive's SHA-256 checksum. On Windows, first open the interpreter's Linux environment with `wsl --distribution Silicon --user silicon`, then run those commands there. Restart the interpreter if installation changed its `PATH`.

Public blocks are readable anonymously; private blocks require an existing Starter login with access granted by the owning account. A gene is Markdown text. An ISI archive contains a root `isi.yaml` defining exactly one ISI named for its ID; a function archive contains a root `function.yaml` defining exactly one function and its parameters. Supporting files stay beside those definitions in the cache. File paths in a downloaded ISI's DNA assembly resolve from its block directory; shell commands still run from `SILICON_HOME`.

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

Bash runs as the current operating-system user, with the resolved home as its working directory. It receives `SILICON_HOME` and `ISI`. The runtime also gives Omni/provider sessions the configured `TZ`, `SI_URL`, and an ISI capability in `SI_TOKEN`.

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

Outgoing journals are stored under `.silicon/outbox/<silicon-id>/<request-id>.json`, separately from the incoming inbox. A stable request ID and delivery slot track outgoing state across processing retries. The persisted message remains the delivery plan even if a timestamp or other expression changes on replay; changing that slot's destination raises an error rather than rerouting it. On reconnect, completed delivery receipts whose incoming requests are already gone are cleaned up, while undelivered messages remain. Crash recovery is at least once: a crash between provider receipt and persisting that receipt can still duplicate delivery. Other completed app/shell side effects may also repeat when a flow retries, so use application event IDs for idempotency. There is no transactional rollback of actions or queued sends. Flow nesting is limited to 64 levels.

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

`install` and `uninstall` operate on Silicon Apps-managed applications using bare app IDs. `ping` checks the local interpreter's connection without prompting a model. `config` returns the connected configuration with credentials redacted. `info` returns version, protocol, source, documentation, and dependency details.

Application installation uses the current `SILICON_HOME`, or your ordinary home when unset. Set `SILICON_HOME=/path/to/home` when installing for a particular Silicon. Every connection installs configured or managed apps, plus Accounts and Ting. If a refresh fails but a working copy is installed, the interpreter logs the full failure and continues with that copy. Silicon Apps owns verification, installation, and updates under `<home>/.apps`; command links live in `.apps/bin`. The interpreter and direct CLI share that home and its account sessions. Unrelated global commands remain untouched. Legacy explicit executable commands are used as supplied. Authentication checks the current account session independently of package installation.

Commands are exposed through owned links under `<home>/.silicon/bin`; the interpreter adds those to its command environment. `uninstall` removes packages installed in this managed Silicon Apps home and matching interpreter links while preserving application credentials. Remove configured `apps` entries before reconnecting if you do not want an application installed again, or use `si app uninstall` from the running Silicon to update the YAML too.

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

`si app install` installs through Silicon Apps, authenticates the canonical app ID, and adds it to the calling ISI's `apps`. All ISIs can use the installed app. `si app uninstall` removes authentication, the managed package, app entries across ISIs and legacy `silicon.apps`/`login`, and its `app_configs` entry. Silicon Accounts and Ting cannot be uninstalled through this internal command. App edits validate and atomically update the affected configuration blocks, retaining source expressions and the live flow. Add new app configuration directly to the YAML; reconnect to apply manual settings changes.

`si session new` applies to the calling persistent session. `--id`, `--title`, `--description`, and `--archive-current-session` are required. `--summary` is an alias for `--description`. There is no `si deliberate start-new-session` command in this release.

### Reading errors

A failure carries the words of whatever failed: an app CLI, Silicon Apps, Silicon Accounts, Ting, a Bash expression, Omni and its provider, Caddy, GitHub, or the installer. Nothing is summarized or truncated, and there is no need to rerun a command to see its diagnostic. A command that ran and failed reads as the command, its exit status, then its complete stderr and stdout:

```text
`bash -c 'ledger --json'` failed: exit status: 4
stderr:
ledger: quota exceeded
  at line 2
stdout:
{"ok":false,"code":"quota"}
```

An empty stream reads `stderr: (empty)`. A command that exited successfully but answered wrongly states the problem first, as in `` `dm accounts --json` returned invalid JSON: expected value at line 1 column 1 ``, followed by the same status and streams. A command that could not start reads ``could not run `COMMAND`: `` and the operating system's reason. JSON an app prints is shown raw, exactly as printed. Parse errors keep the serde, YAML, or CEL text with its line and column, and configuration errors name the field and the value found. An HTTP failure carries the status and the response body. Each layer adds context before a colon, so the outermost words say what the interpreter was doing and the innermost are the tool's own. An exhausted fallback chain lists every candidate (`all 2 fallback candidates failed:` then `1.`, `2.`), and a second failure during reporting or cleanup follows on an `also:` line instead of replacing the first.

The same text reaches every reader:

- `silicon` and `si` exit with status 1 and print `Error:` and the complete text on stderr, including any `Caused by:` lines. `--json` changes successful output only.
- The control and internal APIs answer with `{"error": "..."}`. The dashboard shows that text whole, or the HTTP status and raw body when an answer is not JSON.
- A flow catch receives expression and validation failures as `{self.error}`; outgoing delivery failures appear in runtime logs and durable delivery state.
- `silicon.log` records it as an `[error]` entry. The file escapes newlines; `silicon logs show`, connection progress, and the dashboard unfold error entries so multi-line output reads as the tool wrote it, and `--json` returns the stored line. Errors logged during `connect` appear as they happen, even when the connection then succeeds.
- Failures with no caller, such as DNA refreshes, heartbeats, restoration, rejected Ting deliveries, and automatic updates, go to `silicon.log`. The last three also go to the interpreter's `daemon.log`.

Only literal credential values are replaced with `[redacted]`: the Silicon token, Silicon Accounts SLTs, ISI capabilities, Space Station table keys, the JSON argument of `APP config set`, `app_configs` values of eight or more characters, recognized token prefixes such as `stk-`, and the values of credential-named JSON fields such as `access_token`, `password`, or `api_key`. The surrounding message always survives. Configured values shorter than eight characters, such as `en` or `true`, are settings and stay readable; masking every occurrence of them garbles the errors people must read. A credential expression (`silicon.token`, `space_station.table_key`, or an `app_configs` string) reports its exit status, stderr, and parser or CEL text, but its source reads `[credential expression]` and its stdout reads `(withheld; it is the credential)`.

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

## Silicon Accounts application contract

Silicon Accounts authenticates Carbons (`c:handle`) and Silicons (`si:handle`). An app is owned and used by an account. The interpreter uses the configured Silicon's identity; it never borrows a personal Carbon login.

The supported discovery and authentication commands are:

```sh
APP accounts --json
APP login status --json
APP login --slt SHORT_LIVED_TOKEN
APP logout
```

Discovery returns a bare `app_id`, such as `{"app_id":"dm"}`. Status reports a boolean `authenticated` and the signed-in account identity, such as `id`, `kind`, and `uuid` (possibly under `account`). A successful status must match the configured Silicon. A malformed response or failed command is an error, not evidence of a usable session.

The interpreter asks Accounts to issue a token for the intended app:

```text
silicon-accounts login --silicon si:HANDLE --app APP_ID --json
```

It passes the Silicon credential privately through `ACCOUNTS_STK`, with `ACCOUNTS_HOME` set to the Silicon home. Accounts returns `app_id`, `slt`, and `expires_at`; only the matching application's `login --slt` command receives that one-use token. The interpreter verifies authenticated status after login. App discovery, authentication, and status failures preserve the command's complete diagnostic output with credentials redacted.

App sessions persist on disk beneath `SILICON_HOME`. Apps own their access and refresh tokens and reuse their session until it expires or the account logs out. The interpreter checks the current session at authentication boundaries. A valid session for the same account is reused across reconnects and restarts. A different signed-in account cannot be silently reused.

`si auth setup APP` explicitly signs in an app. `si auth remove APP` signs it out and remembers that choice across reconnects; automatic session checks do not undo it. A previously managed app that reports a signed-out or expired session stays signed out until explicit setup. Initial setup of a newly configured app remains automatic.

Commands receive `SILICON_HOME` and optional `ISI` context. Silicon Accounts state is isolated at `<SILICON_HOME>/.accounts/` using `ACCOUNTS_HOME`; app CLIs keep their own state beneath the same home. Credentials never enter the ISI's DNA or public configuration output.

### Ting registration and app notifications

Proactive Silicon Accounts apps publish notifications through [Ting](https://ting.teamofsilicons.com/). Ting handles app notification delivery and registration; application CLIs no longer need their own `webhook` or `unhook` commands. The local endpoint also accepts other JSON webhook callers.

The interpreter installs `ting` through Silicon Apps and authenticates its CLI just like other Silicon Accounts apps. After the Silicon route is ready, it inspects `ting webhook list --limit 100 --json` and registers with `ting webhook http://HANDLE.si.localhost/events --json`. A saved hook is reused with `--id WEBHOOK_ID`; the stable ID is retained across reconnects and restarts. Ting delivers `tings` batches to that endpoint with an empty HTTP 204 acknowledgment after durable acceptance.

Disconnect runs `ting unhook WEBHOOK_ID --json`. It still removes the local connection and ISI capabilities if unhooking fails, reporting the cleanup error. A failed registration rolls back the local connection and routing. Ting owns remote delivery, retries, and its local bridge; app-specific transport daemons are not registered with Silicon.

Apps that accept configuration expose `APP config set JSON`. The interpreter sends each configured object after authentication. If the app rejects it, the error names `silicon.app_configs.APP`, shows the command with its JSON argument as `'[redacted]'`, and includes the app's exit status and complete stderr and stdout, with configured values masked as described in [Reading errors](#reading-errors). Apps store credentials and configuration in their own state beneath `SILICON_HOME` (or their chosen remote configuration service), work when `ISI` is absent.

## Settings and telemetry

Settings apply to the interpreter and are stored in `settings.json` beneath its state directory. Telemetry defaults to enabled:

```sh
silicon settings get
silicon settings get telemetry
silicon settings set telemetry --off
silicon settings set telemetry --on
```

| Setting | Controls |
| --- | --- |
| `telemetry` | TOS-owned Space Station telemetry from this installation. |

`SILICON_TELEMETRY=0` also disables TOS telemetry. App CLIs may have their own telemetry settings. Silicon Apps owns application updates; inspect its current controls through `silicon-apps --help`.

TOS telemetry uses separate destinations for interpreter/CLI/daemon activity, Silicon runtime and session activity, and the documentation frontend. Events include the source, operation, timestamp, version, Silicon identity, ISI context where applicable, and diagnostic context. Silicon inputs, flow logs, provider/session events, tool or command information, and `silicon.log` error entries with their complete tool output can be included, masked as described in [Reading errors](#reading-errors). Telemetry records diagnostic activity; it does not replace durable session history or local logs. Space Station queues records durably. A short-lived CLI command waits at most 100 ms during flush and may exit with records still queued; the running interpreter's client drains that queue, so delivery need not be immediate.

To also send one Silicon's activity to your own Space Station table, add:

```yaml
silicon:
  # ...the other required fields...
  space_station:
    table_name: assistant-events
    table_key: REPLACE_WITH_YOUR_TABLE_KEY
```

`table_key` authorizes ingestion; `table_name` identifies the configured destination. `silicon settings set telemetry --off` disables TOS telemetry and keeps this explicitly configured destination active. Remove `space_station` and reconnect to stop the user destination. The Space Station Rust client handles buffering, its local spool, delivery, and retries. When Space Station is a managed app, its login uses the same Silicon Accounts identity as the interpreter connection.

The interpreter removes Silicon tokens, app configuration, and Space Station table keys from runtime CEL and public configuration responses. Known credential values, credential-shaped fields, and recognized token prefixes are redacted from diagnostic events, logs, and errors. Redaction is the only reduction: errors retain their complete text in the relevant CLI, `si`, dashboard, flow catch, or `silicon.log`, as described in [Reading errors](#reading-errors). Configured values shorter than eight characters are treated as settings and are not redacted. Compile-time Bash logs `running: [compile-time expression]` and its exit status, because runtime redaction is not yet registered; if it fails, the error shows the command with known credentials masked, its status, and both streams. A credential expression logs `running: [credential expression]`; its error shows its exit status, stderr, and parser or CEL text, but names the source `[credential expression]`, masks the source's words of eight or more characters wherever the parser quotes them, and never shows its stdout, which is the credential. Runtime Bash records its redacted command; setup additionally records stdout and stderr after each command finishes. Ordinary prompt and event content remains useful for diagnosis and can be included. Do not put credentials into ordinary message fields or command arguments intended for logs.

The documentation website has its own **Share documentation usage with TOS** checkbox in the footer. Its preference is saved in that browser's local storage and is independent of local interpreter settings. The page submits telemetry to its same-origin `/api/telemetry` endpoint; the destination credential stays on the server.

## Files, storage, and logs

The default interpreter state directory is `~/.silicon-interpreter`. `SILICON_INTERPRETER_HOME` selects another directory, useful for an independent installation.

| Location | Contents |
| --- | --- |
| Interpreter `daemon.json` | Running PID, chosen port, local management token, version, start time, and the supervisor that started it. Removed on orderly shutdown. |
| Interpreter `daemon.lock` | Held by the running interpreter; its first line is the holder's PID. |
| Interpreter `connections.json` | Every saved Silicon, whether connected or still waiting to be restored, until an explicit disconnect. One that does not parse is moved to `connections.json.corrupt-<UTC>`. |
| Interpreter `service.json`, `service.env`, `service.log`, `stopped` | The installed autostart service, the environment it gives the interpreter (0600), its supervisor's log, and a `silicon stop` marker for this boot. |
| Interpreter `daemon.log` | Background interpreter stdout/stderr, including restoration, update, and rejected webhook delivery failures. Those failures also go to the affected Silicon's `silicon.log`; automatic-update failures go to every connected Silicon's log. |
| Interpreter `settings.json` | Telemetry preferences. |
| Interpreter `control-session.json` | Private management token, retained across interpreter restarts until logout rotates it. |
| Interpreter `space-station/` | Space Station client state and durable telemetry spool. |
| Interpreter `caddy/` | Owned Caddy configuration, logs, data, storage, and `owner.json`/`caddy.pid` recording the running Caddy so a later start can stop one left behind. |
| `<SILICON_HOME>/.silicon/silicon.log` | Silicon event, flow, send, runtime, error, and provider log; rotated at 64 MiB. |
| `.silicon/heartbeats.json` | Wall-clock due times of this Silicon's heartbeats. |
| `.silicon/auth-apps.json` | Commands for configured/dynamically managed application authentication. |
| `.silicon/auth-checked.json`, `.silicon/auth-grants.json` | Account-bound application authentication state and remembered logout choices. |
| `.apps/bin/` | Command links managed by Silicon Apps. |
| `.silicon/bin/` | Interpreter-owned aliases for the connected Silicon's app commands. |
| `.apps/installed/` | Verified application packages managed by Silicon Apps. |
| `.silicon/apps.md`, `.silicon/app-descriptions.json` | Shared app DNA catalog and the last successful Silicon Apps metadata for each app. |
| `<SILICON_HOME>/.fromstarter/` | Downloaded Starter genes, ISI/function definitions, and their supporting files; refreshed when the Silicon connects. |
| `.silicon/sessions/active/<isi>/<UUID>.json` | Durable active persistent-session records. |
| `.silicon/sessions/archived/<isi>/<UUID>.json` | Archive metadata. |
| `.silicon/sessions/events/<UUID>.jsonl` | Persistent-session provider events. |
| `.silicon/omni/<UUID>/` | Omni's session data and its daemon log. |
| `<SILICON_HOME>/.silicon/ting/<silicon-id>/` | `pending/` (one file per accepted JSON request, run oldest first), `seen.json` (ting IDs processed in the last 100 days), and `hook.json` (the retained webhook ID). An earlier `inbox.json` is moved into these on first use. A damaged file is moved to `NAME.corrupt-<UTC>` and reported, and the inbox carries on. |
| `<SILICON_HOME>/.silicon/outbox/<silicon-id>/<request-id>.json` | Durable outgoing flow messages, delivery attempts, and undelivered groups retained for destination-triggered replay. |
| `<SILICON_HOME>/.accounts/` | Isolated Silicon Accounts CLI configuration and persisted session for this Silicon. |

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

Logs rotate at 64 MiB: the file moves to `NAME.1`, older copies shift up, and a new file starts. `silicon.log`, `daemon.log`, and `caddy.log` keep five copies; each session's Omni `daemon.log` and `.silicon/sessions/events/<UUID>.jsonl` keep two, and `si isi show` reads the event history across a rotation. A torn line in the event history (a full disk, a power cut) is shown beside the rest with the parser's reason instead of failing the command.

## Local dashboard and HTTP interface

`silicon web` opens the direct localhost dashboard with the current interpreter token in the URL fragment. `--no-open` prints that URL. The dashboard consumes the fragment, removes it from the displayed URL, and stores the token in browser local storage until you log out. Treat the printed URL and stored token as a management credential. Restarting the browser or interpreter preserves access. **Log out** revokes the current local management token and removes the saved token and visible data from open dashboard tabs. An HTTP 401 also clears the stale browser token; a temporary connection failure does not sign you out. Open `silicon web` again to obtain the current token.

The dashboard uses the [Silicon UI foundation](https://ui.teamofsilicons.com/r/silicon-foundation.json), vendored for offline use. It can list, compile, connect, disconnect, send events/messages, inspect/end sessions, search archives, start successor sessions, authenticate/remove apps, and read/follow logs. Its log following polls the latest tail; the CLI follower is the continuous file-oriented view.

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

The runtime omits the Silicon token from CEL's `silicon` object and removes `SILICON_TOKEN` and `SILICON_INTERPRETER_TOKEN` from the provider environment. The ISI receives only its own `SI_TOKEN` capability for internal API access. Silicon Accounts credential state is scoped to the Silicon home, and the application receives a short-lived, app-specific token instead of the long-lived Silicon credential. Protect the original YAML and all credential-bearing files accordingly.

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

Supervisors start programs with a minimal environment. `~/.silicon-interpreter/service.env` (mode 0600) holds `PATH`, `LANG`, `LC_*`, `SHELL`, and the `SILICON_`, `OMNI_`, `APPS_`, `ACCOUNTS_`, `TING_`, `SPACE_STATION_`, `ANTHROPIC_`, `OPENAI_`, `CLAUDE_`, `CODEX_`, and `GEMINI_` variables of the terminal that installed the service, refreshed whenever `silicon connect` starts the interpreter; the login shell's `PATH` is added when it starts. Edit it and run `silicon service restart` to change what the interpreter sees. The supervisor writes to `service.log`; the interpreter writes to `daemon.log`.

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
| Silicon Apps installs, updates, and removals | 20 minutes | `SILICON_INSTALL_TIMEOUT_SECS` |
| Silicon Accounts and application CLIs | 2 minutes | `SILICON_APP_TIMEOUT_SECS` |
| Ting CLI | 60 seconds | `SILICON_TING_TIMEOUT_SECS` |

Expressions that CLI commands such as `silicon compile` evaluate in your terminal have no limit, so a credential command there can still prompt.

## Updates

Silicon Apps owns package updates. `silicon update` delegates to `silicon-apps update silicon`, then requests an interpreter restart when the installed executable changes. The running interpreter also watches for an executable replacement made by Apps and waits until active work finishes before restarting.

The restart closes admission, stops owned children, and restores saved connections and durable sessions with the installed version. Continuous work can postpone that restart. YAML files, memories, workspaces, archives, and account sessions are outside the release payload and remain in place.

Use `silicon-apps --help` for installation, update, and developer publication controls. There is no separate interpreter update preference. Source builds remain under the developer's control.

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
| App discovery/status fails | The error shows the app command, its exit status, and its stderr and stdout, including any JSON it returned. Fix what the app reports; update an app that lacks `APP accounts --json` or a supported status form. |
| Silicon Accounts cannot mint the SLT | Check the Silicon ID/token, app ID, permissions, and Silicon Accounts session. A personal Carbon login is not a substitute. |
| App rejected the SLT | Fix the app login issue and start authentication again; the old one-use SLT may already be consumed. |
| Removed app logs in again | Use `si app uninstall` or remove it from every ISI `apps` list (including Starter overrides), legacy `silicon.apps`, and legacy `silicon.login`, then reconnect. Ting remains implicitly managed. |
| Setup failed | Read the reported `silicon.setup` index, exit status, and stderr/stdout in the error. Each shell fallback needs `!`; completed shell effects remain even when connection fails. |
| Silicon Apps cannot resolve a configured app | The error shows each candidate command passed over with its reason, and Silicon Apps's exit status and output if Silicon Apps itself failed. Check the bare app ID, CLI discovery, package availability for this platform, and the signed-in Silicon Accounts identity. |
| Disconnect reports cleanup errors | The Silicon/capabilities are removed; inspect Ting’s hook state and Caddy/interpreter logs. |
| A saved Silicon shows `waiting` in `silicon ls` | Read its error there: the interpreter retries it by itself. Fix what it names (a moved YAML, a failing setup script, an app that cannot install) and run `silicon connect` to retry at once. |
| The interpreter did not come back after a reboot | Run `silicon service status`. On macOS it starts only after a desktop login; on Linux without lingering it starts at login; without systemd it needs a cron daemon. Read `service.log` and `daemon.log` in the interpreter directory. |
| `silicon stop` did not stop it | `silicon stop --force` ends an orderly stop that is stuck. To keep it from starting at the next login or boot, run `silicon service uninstall`. |
| A tool failed with `silicon: stopped after …` | It reached its [time limit](#time-limits). Fix what hangs, or raise the limit with the matching `SILICON_*_TIMEOUT_SECS`. |
| Another server answers port 80 | Routing stays down until that server stops; the error quotes what answered. |
| Automatic update did not restart yet | Inspect update logs and active work. Restart waits until the interpreter can safely stop admitting work, and a failed check of the new release keeps the current one. |
| Automatic update needs Linux privileges | The installer exited 77. Run `silicon update` in a terminal as a user who can use `sudo` so a changed Caddy binary can receive port 80 capability (on Windows, rerun the Windows installer). |

Useful environment switches are `SILICON_INTERPRETER_HOME` for interpreter state, `SILICON_CADDY` for the Caddy executable, `OMNI_DAEMON` for the Omni daemon executable, `SILICON_NO_SERVICE=1` for development runs, the `SILICON_*_TIMEOUT_SECS` [time limits](#time-limits), `SILICON_IDLE_SESSION_MINUTES`, and `SILICON_TURN_STALL_MINUTES`. They affect the process in which they are set; an already-running daemon does not inherit new variables from a later terminal command.

## The repository's reference template

`stemcell/silicon`, `stemcell/memories`, and `stemcell/workspace` are reference material. The installer does not distribute them into user homes, and they still require your own credentials and supporting scripts.

`stemcell/silicon/silicon.yaml` is the supplied source of intent, with its CEL expressions corrected and its helper functions supported. It is not a ready-to-connect configuration. Its current issues include:

- `silicon.id`, `silicon.token`, and the optional Space Station fields are `...` placeholders.
- It refers to missing scripts/files including `install_python.sh`, `contacts.sh`, `tools.sh`, `team.sh`, `time_delay.sh`, and `learn.sh`. The repository has `CONTACTS.md`, `tools.md`, and `learn.md`; those names do not make the scripts exist automatically.
- Some suggestion messages still show old command forms. Current rollover uses `si session new --archive-current-session --id ... --title ... --description ...`.
- Its flow uses native `for` steps for ordered per-ting routing, keeps each item in `var.ting`, and aggregates messages by destination.

Correct a separate copy for your deployment. Compilation can report expression syntax before reaching the placeholder checks because syntax validation runs before any compile-time shell command. The absence of one particular placeholder error does not make the reference configuration valid.

## Development and compatibility

The repository contains the Rust interpreter and internal CLI under `src/`, the Windows launcher under `windows/`, and documentation under `docs/`. The `stemcell/` tree is reference material excluded from installation. Start with [the source on main](https://github.com/teamofsilicons/silicon-stemcell/tree/main), `silicon info`, and the CLI command tree.

### Application integration

Use the [Silicon Developer portal](https://developers.teamofsilicons.com/) and its `silicon-apps` CLI for application ownership, configuration, release upload, and publication. Authenticate as the Carbon or Silicon that owns the application; confirm that identity before publishing. The public [Silicon Apps catalog](https://apps.teamofsilicons.com/) supplies consumer installation and discovery.

Apps expose `accounts --json`, `login --slt`, `login status --json`, and `logout`, keep account sessions until expiry or logout, and store state beneath `SILICON_HOME`. `ISI` is optional context. Proactive apps publish notifications through Ting. Apps with configuration expose `config set JSON`. Every command should describe its purpose, supported flags, and concrete failure causes through `--help`.

### Contract versions

| Consumer or dependency | Silicon 7.0.0 contract |
| --- | --- |
| Configuration | Complete `si:handle` identity and bare app IDs; see [7.0 migration](#upgrading-to-70). |
| Application discovery | `APP accounts --json` with matching bare `app_id`. |
| Authentication | Accounts app token issuance, `APP login --slt`, account-bound persistent sessions, and explicit logout. |
| Distribution | Silicon Apps `silicon-apps` CLI; publish through `silicon-apps`. |
| Inference | Omni 0.9.1 Rust/client-daemon contract at `62c2adc57983be37c1de2064d169073bddf71291`. |
| Local interpreter API | Protocol `1`; protected `POST /control` and `POST /si`. |

Configuration and application contract changes require a major release and migration guidance. The current CLI help and Accounts discovery describe each application's supported contract.

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

Use `SILICON_APPS` to select a particular Apps executable. Use a dedicated Silicon home when validating an integration so existing connections and personal sessions remain independent.

## Build and release

```sh
cargo build --locked --release
cargo clippy --locked --all-targets -- -D warnings
```

Release archives include Silicon, Omni, Caddy, their notices, and version metadata. The release workflow builds the supported platforms and uploads checksum-verified assets. Publish the app catalog release through the signed-in owner's `silicon-apps` CLI. Deployment credentials and local account state must never be committed or included in release archives.

The Windows ARM64 runtime remains a preview pending verification on physical ARM64 Windows hardware.

### Maintaining the documentation

The source is `docs/GUIDE.md`, `docs/DIARY.md`, `docs/EXTERNAL-BUGS.md`, and `docs/shell.html`. With Python Markdown installed, run `python3 docs/render.py` and commit the updated `docs/site/index.html`. An isolated alternative is `uv run --with markdown==3.8.2 python docs/render.py`. Vercel serves the committed static output and the telemetry API; it does not build or upload the interpreter, template homes, or local state.
