# Silicon 3.5

A local interpreter for connected Silicons, powered by Rust and Silicon Omni.

## What runs on your machine

Silicon is a local interpreter for a `silicon.yaml` file. One interpreter can connect several Silicons. Each Silicon has its own identity, home directory, internal Silicons (ISIs), access rules, event flow, sessions, and logs.

The `silicon` command manages the interpreter from your terminal. The `si` command is provided inside an ISI so it can communicate with permitted ISIs, inspect work, manage its session, and ask the interpreter to authenticate an application. The local dashboard exposes the same management operations through the interpreter's control API.

The interpreter listens on `127.0.0.1:1823`. If that port is occupied, it tries 1822, 1821, and so on, down through 1024. A dedicated Caddy process provides `http://silicon.localhost` for the dashboard and `http://<local-id>.<org-id>.localhost` for each connected Silicon. Caddy uses HTTP port 80; it does not search for an alternative proxy port.

Each active ISI session has an owned Omni daemon and uses Silicon Omni's Rust package. Omni starts and communicates with the selected inference provider. Silicon subscribes to Omni events, delivers new messages, assembles DNA, tracks sessions, and records progress. These are real provider processes; the interpreter does not simulate model output in normal use.

No terminal window is opened for each ISI. Each receives an independent process context, an Omni session, and the environment needed by its commands. Persistent session data survives stopping the interpreter. Global ephemeral work is discarded when it finishes.

## Installation

### Public binary installation

The public installer is designed to download a complete, versioned bundle. It does not require a Rust compiler. Once the `v3.5.0` release and its assets have been published and verified, the release-specific command is:

```sh
curl -fsSL https://github.com/teamofsilicons/silicon-stemcell/releases/download/v3.5.0/install.sh | sh
```

At this documentation snapshot, publication is pending. An unavailable bundle is a hard installation error; the installer does not substitute an older Stemcell release.

The default installation prefix is `~/.local/share/silicon`. Add its `bin` directory to your shell's `PATH`:

```sh
export PATH="$HOME/.local/share/silicon/bin:$PATH"
```

For the default prefix, the installer adds this path once to the startup file for your detected shell. Open a new terminal, or run the printed export command in your current terminal. Set `SILICON_NO_PATH=1` to leave shell startup files alone. Custom prefixes print the path without changing startup files.

To choose another dedicated prefix, set the variable on the `sh` side of the pipeline:

```sh
curl -fsSL https://github.com/teamofsilicons/silicon-stemcell/releases/download/v3.5.0/install.sh \
  | SILICON_PREFIX="$HOME/tools/silicon" sh
```

The target matrix covers macOS and Linux on ARM64 and x86-64. Windows is not a supported target. Release CI is configured to build each target; a local test on one architecture does not verify the other three.

The binary installation needs `curl`, `tar`, and a SHA-256 verifier (`sha256sum` or `shasum`). Downloads use HTTPS. Before activation, the installer checks the bundle checksum, required members, version marker, and whether the interpreter and Omni daemon can execute. A complete release includes:

| Command | Bundled component |
| --- | --- |
| `silicon`, `si` | Interpreter and internal CLI, 3.5.0 |
| `omnid`, `silicon-omni`, `omni`, `so` | Omni pinned to commit `d52f5416cd33b363554d2300b5603dc0b6c43545` |
| `caddy` | Caddy 2.11.4 |
| `iam` | `silicon-iam-cli` 1.4.1 |
| `dm` | `silicon-dm-cli` 0.3.0 |
| `briefcase` | `briefcase-cli` 0.2.4 |
| `waveform` | `waveform-cli` 0.1.0 |
| `commit` | `silicon-commit-cli` 0.1.0, through a managed wrapper |
| `remind` | `silicon-remind-cli` 0.1.2, through a managed wrapper |
| `hook` | `silicon-hook-cli` 0.2.0 |

The Commit and Remind wrappers suppress their independent update checks. Their underlying binaries are shipped as `commit-native` and `remind-native` inside the release. For a fresh Commit home with no explicit backend override or existing saved configuration, the wrapper selects `https://backend.commit.teamofsilicons.com`. It preserves an existing configuration or explicit `COMMIT_API_URL`.

The bundle supplies the listed client tools and local daemons. Accounts, permissions, remote service availability, and authenticated inference providers still have to be available. Installing a CLI does not create an IAM identity or grant provider access.

### Linux and port 80

On Linux, the installer checks whether unprivileged processes can bind port 80. If not, it grants only the bundled Caddy binary `cap_net_bind_service=ep` using `setcap`. This requires the system's libcap tools and suitable privileges; it can ask for `sudo` in an interactive terminal. Silicon itself does not need to run as root.

An automatic, noninteractive update cannot prompt for privileges. When the Caddy binary is unchanged, the installer can reuse the existing capability. If a new Caddy binary needs a capability and unattended privilege is unavailable, activation fails with an explanation and leaves the current release selected. Rerun the installation or update interactively.

### Build the complete installation from source

From this repository's root:

```sh
SILICON_SOURCE_DIR="$PWD" sh install.sh
```

Source installation requires Rust 1.98 or newer, Cargo, a C compiler, and dependencies needed by the pinned Rust crates. Release CI pins Rust 1.98.1. It builds the interpreter and every required application, and downloads verified Caddy. Caddy's upstream checksum file uses SHA-512.

`SILICON_GIT_REV` is an alternative to `SILICON_SOURCE_DIR`: supply an exact, lowercase, 40-character Git commit. The installer fetches that commit and verifies the checkout. The two source selectors cannot be used together.

For controlled builds, `SILICON_DEPENDENCY_BIN_DIR` can supply already-built, trusted dependency executables. This is a reuse mechanism, not independent verification of arbitrary local binaries. The interpreter is still built from the selected source. `CARGO_TARGET_DIR` can reuse a compilation directory.

For a developer build of only the interpreter and internal CLI:

```sh
cargo build --locked
./target/debug/silicon --help
./target/debug/si --help
```

This does not install the required runtime dependencies. Put the intended `omnid`, Caddy, IAM, and application commands on `PATH`, or use the documented binary overrides. Automatic bundle updates do not replace an unmanaged `target/debug` or `target/release` executable.

### Activation and retained releases

Installed commands are symlinks through `<prefix>/lib/silicon/current/bin`. A new release is prepared under the same prefix, checked, moved into `<prefix>/lib/silicon/releases`, and selected by replacing the `current` symlink. Existing unmanaged files in `<prefix>/bin` are not overwritten; choose another prefix if there is a conflict.

Only one installation may modify a prefix at a time. A stale `.install-lock` after a forced kill requires checking that no installer is running before removing the lock. Old release directories are retained; there is no automatic release-pruning or public rollback command in this version.

The installer does not copy the repository's `stemcell` examples into a user's home. It never edits a connected `silicon.yaml`, memories, workspace, or session archive.

## First Silicon

Create your own directory and your own `silicon.yaml`. Keep it separate from the repository's reference fixture. The following is a complete schema example. Replace the identity and token with your values and ensure Omni has an authenticated provider that can satisfy the `fast` model key.

```yaml
silicon:
  id: assistant:my-org
  token: REPLACE_WITH_YOUR_SILICON_TOKEN
  timezone: Asia/Kolkata
  SILICON_HOME: ! pwd
  inference_providers:
    - all-available-providers

isi:
  assistant:
    model: fast
    primary_send_mode: global
    session_type: persistent
    dna:
      assemble:
        - '! printf "You are a helpful assistant. Keep useful notes in your workspace."'
      next_refresh: 30min

access:
  assistant: []

flow:
  - send:
      isi: assistant
      message: '{request.data.message}'
```

No application login is configured in this example. Add application commands when the corresponding Silicon identity and application permissions are ready. `token` remains required even if the first test does not use an IAM application.

```sh
silicon compile /absolute/path/to/silicon.yaml
silicon connect /absolute/path/to/silicon.yaml
silicon ls
silicon send assistant:my-org assistant "Hello"
silicon show assistant:my-org assistant
silicon web
```

`compile` validates configuration and expressions without connecting to the interpreter. It does execute compile-time Bash expressions, including `SILICON_HOME`, so it is not a side-effect-free shell dry run. All expression syntax is checked before those commands execute. Provider startup, application authentication, and runtime request values have their own checks later.

`connect` starts the interpreter if necessary, compiles the file, checks managed application authentication, adds routing, registers configured webhooks, and records the connection. Duplicate Silicon IDs, duplicate canonical YAML paths, or two connected Silicons sharing the same canonical `SILICON_HOME` are rejected.

The local identity is the canonical YAML path. The global identity is `silicon.id`. Moving a file changes its local identity. Editing a connected file does not live-reload it. Disconnect and reconnect to apply changes; restart restoration also recompiles the saved path. The interpreter never rewrites the file for you.

Send an event through the Silicon's host:

```sh
curl --fail-with-body \
  --header 'Content-Type: application/json' \
  --data '{"type":"new_message","data":{"message":"Hello from an event"},"metadata":{}}' \
  http://assistant.my-org.localhost/events
```

Caddy routes the hostname but does not edit DNS or `/etc/hosts`. If a particular client does not resolve `.localhost` names, the equivalent diagnostic command can add `--resolve assistant.my-org.localhost:80:127.0.0.1`.

## Configuration reference

The four required top-level keys are `silicon`, `isi`, `access`, and `flow`. Unknown fields are errors. Ordinary duplicate YAML keys are errors. Flow action order is preserved, including the reference dialect's repeated `if` keys, but the recommended form is an explicit list of steps.

### `silicon`

| Field | Meaning and validation |
| --- | --- |
| `id` | Required `local-id:org-id`. Each part must be a DNS label: 1–63 ASCII letters, digits, or hyphens, beginning and ending with a letter or digit. |
| `token` | Required Silicon credential for IAM. Empty strings, NUL bytes, and the exact placeholder `...` are rejected. It is not an ISI capability or a dashboard token. |
| `timezone` | Required IANA timezone, such as `UTC`, `Asia/Kolkata`, or `America/Los_Angeles`. |
| `SILICON_HOME` | Required existing directory, or an expression producing one. Relative paths resolve against the YAML file's directory. The final path is canonicalized. |
| `inference_providers` | Required nonempty list of provider names, `all-available-providers`, exclusions, or nested lists. |
| `login` | Optional list of deferred IAM application commands. |
| `webhook` | Optional list of application commands that support `webhook URL` and `unhook`. |

`SILICON_HOME: ! pwd` runs `pwd` with the YAML's parent directory as its working directory. Once home is resolved, later compile-time commands and runtime scripts use that home. Compile-time fields are evaluated once per compilation, not on every incoming event.

Provider selection walks the list from top to bottom. `all-available-providers` adds Omni's available providers, `except NAME` removes a provider, and a later explicit name adds it back. Nested lists follow the same order:

```yaml
inference_providers:
  - all-available-providers
  - except codex-app-server
  - [claude-code-cli]
```

A single `all-available-providers` entry uses Omni's native selection. Explicit names are checked against Omni's installed/authenticated providers when a session initializes. An empty final selection is an error. This version does not implement extra named groups such as `open-weight-models` unless Omni exposes that exact name as a provider.

`login` and `webhook` contain executable names and arguments, not shell programs to run while compiling:

```yaml
login:
  - ! dm
  - 'hook --profile work'
webhook:
  - ! dm
  - 'hook --profile work'
```

These fields expand CEL and `!>>` fallbacks, preserve argument quotes, and remove an original leading `!` as a legacy app-command marker. They do not execute Bash during evaluation. For example, `! dm` identifies the app to which Silicon later appends `iam --json`, a login command, or a webhook command; it does not launch bare `dm` during compilation. Command arguments are parsed with shell-style quoting and executed directly as argv. Shell pipelines, redirections, and environment assignments are not command wrappers; use a real executable wrapper when one is required.

### `isi`

Define at least one ISI. Names may contain ASCII letters, digits, `.`, `_`, and `-`, up to 128 bytes; `.` and `..` alone are not allowed.

| Field | Meaning |
| --- | --- |
| `model` | Required Omni model key, passed using `Ask::key`. A key such as `fast` or `code` is resolved by Omni. |
| `primary_send_mode` | Required `global` or `session`. It controls addressing, not retention. |
| `session_type` | Required `persistent` or `ephemeral`. It controls retention and completion behavior. |
| `dna` | Required mapping with `assemble` and `next_refresh`. An empty assembly list is permitted; the internal instruction footer is still appended. |
| `heartbeat` | Optional mapping with `next` and a nonempty string `message`. Omit it for no heartbeat. |
| `new_session_suggestion` | Optional mapping with `cooldown_minutes`, `min_new_messages`, and a nonempty string `suggestion_message`. Omit it for no suggestions. |

Legacy fields are accepted with warnings:

| Legacy field | Canonical interpretation |
| --- | --- |
| `sticky: true` | `primary_send_mode: global` |
| `sticky: false` | `primary_send_mode: session` |
| `archive_on_end: true` | `session_type: ephemeral` |
| `archive_on_end: false` | `session_type: persistent` |

Conflicting legacy and canonical settings are rejected. In particular, `archive_on_end: false` does not mean ephemeral. Use canonical names in new configurations.

### Intervals and scheduled work

Bare numeric intervals are minutes. `30min`, `30m`, `10s`, and `2h` are also accepted. Values must be finite and positive, at most ten years. Timing expressions can use Bash, CEL, and fallbacks; the evaluated result must satisfy the same interval rules.

DNA is assembled at session initialization. `next_refresh` is reevaluated after assembly and after each refresh. Refresh work runs separately from the provider event listener, so a slow DNA command does not block receipt processing. A failed refresh is logged and retried after approximately 60 seconds.

Heartbeat `next` is evaluated when its schedule is created and when it becomes due. The first heartbeat waits for its configured interval. A global ISI can be started by its heartbeat. A session-addressed ISI gets independent heartbeat schedules for existing active session addresses; a heartbeat does not invent a missing session ID. App authentication is checked before delivery. Failed timing evaluations are logged and retried after approximately 60 seconds.

Timers are best effort and have scheduler granularity; they are not wall-clock cron jobs. Keep interval expressions quick and deterministic enough for the desired schedule. DNA, heartbeat, and suggestion expressions inside a session-addressed ISI receive `ISI=name:session-id`.

A new-session suggestion is a message, not an automatic archive. It is considered after a normal incoming message. At least `min_new_messages` new messages since the last suggestion are required, and subsequent suggestions must respect `cooldown_minutes`. Heartbeats and suggestion messages do not count toward that threshold. The first suggestion can occur as soon as the message threshold is reached; the cooldown is between suggestions. Counters belong to each session. Archived sessions do not receive these suggestions.

### DNA assembly

Each `dna.assemble` entry is one of:

- A path relative to `SILICON_HOME`, whose contents are read.
- A `!` Bash expression whose stdout becomes prompt text.
- A fallback chain combining files, commands, and explicitly quoted literal text.

```yaml
dna:
  assemble:
    - silicon.md
    - ! cat worker.md
    - ! ./contacts.sh !>> CONTACTS.md !>> "No contacts are available."
  next_refresh: 30min
```

`learn.sh` without `!` is a file to read. `! ./learn.sh` executes it. Quoted fallback text inside the scalar is literal prompt content; ordinary YAML quotes around a path are only YAML syntax.

An exhausted DNA fallback is logged and that assembly entry is skipped. Other entries continue. The interpreter appends a small instruction footer identifying the current ISI, its home, allowed ISI targets and their addressing modes, and the relevant `si --help` entry points. Names outside that ISI's access list are not included in this footer.

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
| `silicon` | Compiled Silicon settings, with the Silicon token removed at runtime. |
| `isi` | The configured ISI map. |
| `access` | The configured access map. |
| `var` | Variables created during this flow; starts empty for each event. |
| `error` | Present only while running a catch branch. |

Compile-time expressions do not have a live request; `request` and `var` start empty. Required configuration fields cannot depend on an event that has not arrived. Runtime DNA and scheduled expressions also have an empty request object.

Supported helper functions include:

| Expression | Result |
| --- | --- |
| `{tz_time('2026-01-01T00:00:00Z', 'Asia/Kolkata')}` | `05:30:00 01:01:26 Asia/Kolkata` |
| `{to_json(request.data.raw).name}` | Parse a JSON string, then select a field. |
| `{to_yaml(request.data)}` | Serialize JSON-shaped data as YAML using standard YAML indentation and newlines. |
| `{request.data.to.startswith('worker')}` | The supported Python-style spelling of CEL's string-prefix helper. |
| `{request.data.to.split('@')[0]}` | Split a string into a list. |

Use CEL syntax, including `null`, `&&`, and `!=`. Python expressions such as `is not None` are not valid CEL. `convert_time` and `make_readable` are not registered helpers.

Escape literal braces as `\{` and `\}`. YAML single-quoted strings are often convenient for CEL and backslashes. YAML double-quoted strings require their own escaping. Bash parameter forms such as `${NAME}` also contain braces; escape literal braces when they are intended for Bash rather than CEL. `$NAME` avoids that particular ambiguity.

For JSON variables, the interpreter recursively evaluates strings in YAML maps/lists and JSON container strings. Object keys are evaluated too; two keys that evaluate to the same name cause an error. A string result containing valid JSON is decoded, so `"123"` can become a number and `"{\"name\":\"Ada\"}"` can become an object. Subsequent CEL expressions can address that structure.

Fallback candidates are separated by unquoted `!>>` outside CEL. Each candidate is tried in order. A failure is logged; the next candidate does not receive an `error` variable from the failed candidate:

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

Every event requires this JSON shape:

```json
{
  "type": "new_message",
  "data": {"message": "Hello"},
  "metadata": {"app": "example>app"}
}
```

`type` must be a string; `data` and `metadata` must be objects. Applications choose event types and fields. There is no special built-in routing for `new_message`, `message_sent`, or `event`; your flow decides what those types mean.

A flow is an ordered list of steps, or an expression producing a step or list. Steps can also appear in branches. A generated flow is validated before execution.

| Action | Fields | Behavior |
| --- | --- | --- |
| `if` | `condition`, `then`, optional `else`, `catch` | Condition must evaluate to `true` or `false`. |
| `var` | `name`, `value`, optional `catch` | Evaluate the name and JSON-capable value, then store it under `var`. |
| `send` | `isi`, `message`, optional `session_id`, `catch` | Immediately dispatch to an ISI; a session-addressed target requires `session_id`. |
| `log` | `message`, optional `catch` | Append a runtime entry to the Silicon log. |
| standalone `else` | A branch | Run only if none of the immediately preceding consecutive `if` steps matched. |

Consecutive `if` steps are independent. More than one can run. A trailing standalone `else` belongs to that entire consecutive chain, not only the last `if`. Any non-`if` action ends the chain. Use an `else` inside one `if` when you want an ordinary two-way branch.

```yaml
flow:
  - var:
      name: incoming
      value: '{request.data}'

  - if:
      condition: '{request.type == "new_message"}'
      then:
        - send:
            isi: coordinator
            message: '{var.incoming.message}'
            catch:
              - log:
                  message: 'Delivery failed: {error}'
      catch:
        - log:
            message: 'Condition failed: {error}'

  - else:
      - log:
          message: 'Unhandled event type: {request.type}'
```

Catch branches receive a scoped string named `error`. Nested catches temporarily replace it and restore the outer error afterward. A catch does not leak `error` into later ordinary steps. Variables intentionally created inside a catch remain ordinary flow variables. Without a catch, an evaluation/action failure is logged and execution continues with the next step.

A flow send can create a missing session-addressed session automatically. An internal CLI send to a missing persistent session requires `--new`. This is a deliberate difference between flow routing and interactive session addressing.

### Acknowledgment and delivery

Each flow send waits for its provider delivery receipt (`START` or `INJECTED`) before the next step. The event response is sent after the entire flow, including its error handling, finishes. It does not wait for inference turns to finish. Successful responses contain `status: "ok"` and an `event_id`.

Silicon forwards new messages immediately, including while an addressed session is working. It does not keep an interpreter queue waiting for a turn to end. Provider initialization and actual provider delivery can still take time. Delivery receipts have a 60-second deadline. Omni's provider behavior determines when an injection or native follow-up is acknowledged.

The ordinary send CLI returns session and delivery information after dispatch acceptance. `show`, logs, and Omni events reveal subsequent work. A completed model answer is not returned in the event acknowledgment.

Expression, dispatch, and provider delivery-receipt failures run the send action's scoped catch. Without a catch, they are logged and the flow continues. A successful acknowledgment means the flow was processed; inspect logs and catch results to distinguish handled delivery failures. There is no general event deduplication or transaction rollback. If an upstream app retries an event after an uncertain response, already-completed actions can run again. Design side-effecting flows with that retry behavior in mind.

## CLI reference

Use `silicon --help`, `si --help`, and subcommand help as the primary command reference. `--json` is global on both commands.

### Interpreter commands

```sh
silicon compile PATH
silicon connect PATH
silicon disconnect ID_OR_PATH
silicon disconnect
silicon ls '*:my-org'
silicon logs show assistant:my-org
silicon logs show assistant:my-org --lines 200 --no-follow
silicon web
silicon web --no-open
silicon serve
silicon serve --port 1810 --no-proxy
silicon stop
silicon update
```

Without a command, `silicon` lists connections. `silicon list` is an alias for `silicon ls`. Quote globs so your shell does not expand them. `disconnect` without a target lists choices and prints the command to use; it does not disconnect everything.

`serve` stays in the foreground and is useful with a process manager. `connect` normally starts it automatically. `--no-proxy` is a development mode with direct localhost access and no Caddy aliases. This release does not install a login item, launch agent, or systemd service. After a reboot, use `silicon serve` or `silicon connect` to start the interpreter again.

Public work commands specify the Silicon identity first:

```sh
silicon send assistant:my-org coordinator "Please review this"
silicon send assistant:my-org worker.terminal "Build it" --id build-17 --new
silicon sessions assistant:my-org worker.terminal
silicon sessions assistant:my-org worker.terminal --archived '*release*'
silicon show assistant:my-org worker.terminal --id build-17
silicon end assistant:my-org worker.terminal --id build-17
```

### Internal commands

```sh
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

`si session new` applies to the calling persistent session. `--id`, `--title`, `--description`, and `--archive-current-session` are required. `--summary` is an alias for `--description`. There is no `si deliberate start-new-session` command in this release.

## Session behavior

Addressing and retention are separate choices:

| Mode | Sending | End-of-turn behavior |
| --- | --- | --- |
| Global + persistent | `si isi send NAME MESSAGE` reuses the active session or creates it. | Retains the session and conversation for later messages. |
| Global + ephemeral | `si isi send NAME MESSAGE` creates independent disposable work. | Returns final output to an internal caller, then discards recoverable session data. |
| Session + persistent | Use `--id`; add `--new` to create a missing session. | Retains each addressed session separately. |
| Session + ephemeral | Use `--id --title` to create; `--id` sends to an existing running address. | Returns final output to an internal caller and retires the disposable session. |

For ephemeral work, an automatic reply goes back to the ISI session that invoked it through `si`, if that caller still exists. An external event or management send has no calling ISI to receive this reply; inspect logs or progress instead. A global ephemeral call always starts independent work, while a session-addressed ephemeral call can address its currently running ID.

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

Configured login apps are checked on connection, before a new ISI session initializes, and before heartbeats. Apps added with `si auth setup` are remembered in the Silicon's managed-app registry and join those checks. A currently authenticated app can be reused. Removing an app from the dynamic registry does not override an app still listed in `silicon.login`; that configured app can be authenticated again at the next boundary.

The supported discovery contract is:

```sh
APP iam --json
```

It must succeed and return a JSON object with a nonempty string `app_id`, for example `{"app_id":"tos>dm"}`. Extra public discovery fields are allowed. Secrets must not be returned as ordinary discovery data.

The interpreter probes status in this order:

```sh
APP auth status --json
APP login status --json
```

One must provide a boolean `authenticated`. A false state can be reported with a nonzero exit code. A true state must also have a successful exit status. Invalid JSON, an absent boolean, or a transport error must not masquerade as successful authentication.

When authentication is needed, Silicon invokes IAM with its own identity:

```text
iam --output json --org ORG silicon-login --sid LOCAL:ORG --stk SILICON_TOKEN --app-id APP_ID --grant-org ORG
```

IAM must return `slt` and a positive `expires_in` of at most 120 seconds. Only IAM receives the long-lived Silicon credential. Depending on the working status contract, the interpreter then invokes exactly one of:

```text
APP auth token SLT
APP login SLT
```

It checks that the app reports `authenticated: true` afterward. It does not retry a different login spelling with the same potentially consumed one-use token. Captured token-bearing stdout/stderr is not forwarded to the Silicon log or exposed in the CLI result.

Removal supports `auth remove`, `auth logout`, or `logout`, selected through command help, followed by a false-status check. `si auth setup` returns the public app ID; it does not return the SLT or application tokens.

Application commands receive the Silicon home and the common isolated IAM home. App state should live under `SILICON_HOME` in an app-owned hidden directory. The interpreter sets `SILICON_IAM_HOME` to `<SILICON_HOME>/.silicon-iam` so it does not borrow the user's personal Carbon IAM session. A symlink used to redirect that credential directory is rejected.

For test worlds, the selected IAM environment and each application's paired test environment must agree. Merely importing an application into IAM does not create the app backend's own test plane. The real app test-plane creation APIs may require an authorized production control-plane identity; do not infer test success from `iam --json` alone.

### Webhook expectations

An app listed in `webhook` must support:

```text
APP webhook http://LOCAL.ORG.localhost
APP unhook
```

Registration happens after the Silicon route is added. A failed registration rolls back the connection and route as far as cleanup permits. Disconnect attempts every configured unhook and removes the Silicon and ISI capabilities even if one app reports an unhook error; it returns the cleanup error so the remaining app state can be investigated.

Apps should deliver the required event shape as JSON and consider the request acknowledged only on a successful response. They own upstream signature verification, session renewal, transport/retry policy, and any app-local relay. The local interpreter does not independently verify each app's remote webhook signature or require the Silicon token on the loopback event endpoint.

## Files, storage, and logs

The default interpreter state directory is `~/.silicon-interpreter`. `SILICON_INTERPRETER_HOME` selects another directory, useful for an independent test installation.

| Location | Contents |
| --- | --- |
| Interpreter `daemon.json` | Running PID, chosen port, and local management token. Removed on orderly shutdown. |
| Interpreter `connections.json` | Saved connection identities, YAML paths, home paths, and hosts. |
| Interpreter `daemon.log` | Background interpreter stdout/stderr, including restoration/update failures. |
| Interpreter `updates.log` | Bundle installer output for updates. |
| Interpreter `caddy/` | Owned Caddy configuration, logs, data, and storage. |
| `<SILICON_HOME>/.silicon/silicon.log` | Append-only Silicon event, flow, send, runtime, error, and provider log. |
| `.silicon/auth-apps.json` | Commands for configured/dynamically managed application authentication. |
| `.silicon/sessions/active/<isi>/<UUID>.json` | Durable active persistent-session records. |
| `.silicon/sessions/archived/<isi>/<UUID>.json` | Archive metadata. |
| `.silicon/sessions/events/<UUID>.jsonl` | Persistent-session provider events. |
| `.silicon/omni/<UUID>/` | Omni's session data and its daemon log. |
| `<SILICON_HOME>/.silicon-iam/` | Isolated IAM CLI configuration/state for this Silicon. |

Session metadata includes the logical ID, Omni UUID, ISI, title, description, first/last timestamps, archive timestamp, status, normal-message count, and suggestion counters. Persistent data is saved atomically through a private temporary file and rename, with filesystem synchronization. State directories use mode 0700 and newly written state/log files use mode 0600 on Unix.

Short private aliases under `/tmp/silicon-<uid>/` keep Omni socket paths within macOS's Unix-socket limit; session data remains under the Silicon home. Caddy gets a separate private temporary admin-socket directory. The interpreter never contacts the machine's default Caddy admin API.

Ephemeral work does not leave recoverable session records, event-history files, or its Omni session directory after retirement. Its operational activity can still appear in the append-only Silicon log. “Ephemeral” is a retention choice, not a promise that no log of the work exists.

Log entries have this shape:

```text
[type] [origin] [UTC timestamp] [message]
```

Embedded message newlines are escaped so an entry stays on one line. `silicon logs show ID` prints the latest 100 entries and follows new ones. On a terminal, the type/origin prefix is colored, and the Silicon ID and log location remain in a footer. Ctrl-C restores the terminal and exits the viewer. `--no-follow` prints the tail and exits.

With `--json --no-follow`, the result is an object with `silicon`, `path`, and `lines`. With `--json` while following, each line is emitted as an independent JSON object containing `silicon`, `path`, and `line`. The follower handles appends, file replacement/truncation, and partial lines. There is no automatic Silicon log rotation or retention policy in this release.

## Local dashboard and HTTP interface

`silicon web` opens the direct localhost dashboard with the current interpreter token in the URL fragment. `--no-open` prints that URL. The dashboard consumes the fragment, removes it from the displayed URL, and stores the token in browser session storage. Treat the printed URL and stored token as a management credential.

The dashboard can list, compile, connect, disconnect, send events/messages, inspect/end sessions, search archives, start successor sessions, authenticate/remove apps, and read/follow logs. Its log following polls the latest tail; the CLI follower is the continuous file-oriented view.

The underlying control API is `POST /control` with `Authorization: Bearer <interpreter-token>` and a JSON body of `{ "action": "...", "args": {...} }`. The internal API is `POST /si` with the corresponding ISI capability. Events use `POST /` or `POST /events` on a connected Silicon hostname.

Requests require `Content-Type: application/json` and are limited to 16 MiB. Cross-origin browser requests are rejected. During shutdown/restart, requests are rejected with 503 so callers can retry after the interpreter resumes.

| HTTP status | Typical cause |
| --- | --- |
| 200 | Flow/control operation completed to its documented acknowledgment boundary. |
| 400 | Invalid JSON, invalid event shape, configuration error, or failed control/runtime operation. |
| 401 | Invalid management token or ISI capability. |
| 403 | Cross-origin browser request. |
| 404 | Unknown route/host; Caddy also rejects non-loopback peers and unknown hosts. |
| 405 | Unsupported method. |
| 413 | Body over 16 MiB or a failed body read. |
| 415 | Missing/incorrect JSON content type. |
| 503 | Interpreter stopping or restarting. |

## Security and lifecycle boundaries

Silicon configuration is trusted executable input. Bash scripts and inference-provider tools run with your user permissions. The `access` map and ISI capabilities restrict the supported internal API; they do not isolate malicious code running under the same operating-system account. A process that can read the Silicon's configuration or private state is inside that trust boundary.

The interpreter listens on loopback, and the owned Caddy configuration only forwards loopback clients for known hosts. Unknown hostnames and non-loopback peers are rejected, including forwarded-header attempts to impersonate loopback. The event endpoint trusts that local boundary rather than a per-event Silicon bearer token. Publishing or forwarding that endpoint publicly changes the security model and is not a supported installation step here.

The runtime omits the Silicon token from CEL's `silicon` object and removes `SILICON_TOKEN` and `SILICON_INTERPRETER_TOKEN` from the provider environment. The ISI receives only its own `SI_TOKEN` capability for internal API access. IAM credential state is scoped to the Silicon home, and the application receives a short-lived, app-specific token instead of the long-lived Silicon credential. Protect the original YAML and all credential-bearing files accordingly.

Explicit `silicon stop`, SIGINT, or SIGTERM stops the interpreter's owned workers and Caddy, removes the active daemon descriptor, and leaves saved connections and persistent state for restoration. This is a stop request, not a promise to let every model turn finish. App-owned relay daemons and saved authentication belong to those applications; disconnect calls their `unhook`, while stopping the interpreter preserves the saved connection configuration for later startup.

On startup, each saved YAML path is recompiled and reconnected. Failed restorations are logged and skipped rather than preventing every other Silicon from starting. Persistent session records are loaded when needed; provider processes are initialized lazily. A configuration file that was moved, removed, or made invalid can therefore fail restoration without being edited by the interpreter.

Caddy updates use its dedicated private admin socket. Accepted routes are persisted; a failed update retains or restores the previous routes. Cleanup stops only the Caddy process owned by this interpreter. Existing unrelated Caddy installations are not reconfigured or stopped.

## Updates

Managed installations check GitHub's latest stable release once per hour while the interpreter is running. The first periodic check occurs after an hour. Drafts, prereleases, malformed version tags, and versions no newer than the running interpreter are not installed.

The updater uses the same embedded installer and checksum-verified complete bundle as public installation. It does not fetch a new shell script and execute it blindly, and it clears source-build/release-mirror overrides before the update install. Its logs are in the interpreter state directory's `updates.log`.

After successful automatic activation, the interpreter waits for active dispatches, nested activity, and pending provider work to finish. It then closes the admission gate, stops its owned children, and executes the newly installed interpreter. Connections and durable sessions are restored from disk. Incoming work is rejected once restart begins. Continuous active work can postpone the restart.

`silicon update` performs a manual check/install and reports whether a restart is required. After a manual update, use `silicon stop` followed by `silicon serve` when ready. The command reports the currently running version if there is no newer eligible release.

Set `SILICON_AUTO_UPDATE=0` in the interpreter's environment to disable periodic updates. Source builds remain under your control and cannot use the managed-prefix update path. The interpreter, its CLI, and bundled dependencies can change together; YAML files, memories, workspaces, archives, and application state are never part of a release payload.

## Troubleshooting

| Symptom | Check or action |
| --- | --- |
| `silicon` is not found | Add the installation prefix's `bin` to `PATH`; inspect the installer's printed path. |
| Release bundle unavailable | Confirm that the requested release was actually published with complete assets. At this snapshot public publication is pending; use the source-install path for development. |
| Checksum mismatch or missing binary | Keep the current installation; investigate/re-download the release. The installer fails before selecting incomplete payloads. |
| Port 80 cannot bind | Check for another server. On Linux, install libcap tools and let the installer grant the bundled Caddy capability. Use `serve --no-proxy` for direct development access. |
| Interpreter did not use 1823 | Read `silicon web --no-open`, startup output, or the private daemon descriptor; it selected a lower free port. |
| `.localhost` does not resolve in a client | Test with `curl --resolve HOST:80:127.0.0.1`; do not assume Caddy edits DNS. |
| Original template will not compile | It contains placeholders and invalid example expressions. Create your own corrected file; see the template notes below. |
| `SILICON_HOME must exist` | Create the intended directory and check resolution relative to the YAML file. |
| Missing/unknown configuration fields | Use the four required top-level mappings and the exact documented field names; canonical mode names are preferred. |
| Bare `!` inside an inline collection | Quote the complete expression, for example `["! pwd"]`. |
| CEL parse error | Check braces, string quotes, and CEL syntax. Use `null`, not Python's `None`; only registered helper functions are available. |
| No authenticated provider or unknown model/provider | Verify Omni's installed/authenticated providers and model keys with its own help/tools. Configuration compilation alone is not provider readiness. |
| Omni startup failed | Check `.silicon/omni/<UUID>/daemon.log`, `PATH`, `OMNI_DAEMON`, and the provider's own installation/authentication. |
| Flow acknowledged but model is still working | Expected: the acknowledgment waits for flow completion and provider delivery, not final inference output. Inspect progress/logs. |
| Event retry repeats an action | There is no automatic flow transaction or event deduplication. Use app/domain identifiers when your own side effects need idempotency. |
| Session send needs an ID | The target uses `primary_send_mode: session`. Supply `--id`; use `--new` for a missing persistent session. |
| Ephemeral session has disappeared | Expected after completion. Use a persistent ISI when later recovery/querying is required. |
| Archive search seems empty | Bare `--archived` is only 72 hours. Supply explicit filters for older history and check the Silicon timezone. |
| `si` lacks context or capability | Run it inside an interpreter-created ISI. Setting only the `ISI` variable is insufficient. |
| An ISI cannot reach another ISI | Check its `access` list and the target spelling. Cross-Silicon sends are not supported by `si`. |
| App discovery/status fails | Verify `APP iam --json` and one supported status form in the same Silicon home. Update an incompatible app. |
| IAM cannot mint the SLT | Check the Silicon ID/token, app ID, permissions, and isolated IAM environment configuration. A personal Carbon login is not a substitute. |
| App rejected the SLT | Fix the app/test-plane issue and start authentication again; the old one-use SLT may already be consumed. |
| Removed app logs in again | Remove its entry from your own `silicon.login` configuration if it should no longer be managed, then reconnect. |
| Disconnect reports cleanup errors | The Silicon/capabilities are removed; inspect the named app's unhook state and Caddy/interpreter logs. |
| Startup skipped a saved connection | Confirm the saved YAML path still exists, compiles, and can authenticate its managed apps. |
| Automatic update did not restart yet | Inspect update logs and active work. Restart waits until the interpreter can safely stop admitting work. |
| Automatic update needs Linux privileges | Rerun the installer/update interactively so a changed Caddy binary can receive port 80 capability. |

Useful environment switches are `SILICON_INTERPRETER_HOME` for interpreter state, `SILICON_CADDY` for the Caddy executable, `OMNI_DAEMON` for the Omni daemon executable, and `SILICON_AUTO_UPDATE=0` for development runs. They affect the process in which they are set; an already-running daemon does not inherit new variables from a later terminal command.

## The repository's reference template

`stemcell/silicon`, `stemcell/memories`, and `stemcell/workspace` are reference/testing material. The installer does not distribute them into user homes, and this implementation does not modify them.

`stemcell/silicon/silicon.yaml` is deliberately preserved as the supplied source of intent. It is not a ready-to-connect configuration. Its current issues include:

- `silicon.id` and `silicon.token` are `...` placeholders.
- Several CEL interpolations contain text such as `your time` inside the expression without valid CEL syntax.
- It contains Python-style `is not None`, which must be expressed in CEL in a user's own configuration.
- It refers to `convert_time` and `make_readable`, which are not registered helpers.
- It refers to missing scripts/files including `contacts.sh`, `tools.sh`, `team.sh`, `time_delay.sh`, and `learn.sh`. The repository has `CONTACTS.md`, `tools.md`, and `learn.md`; those names do not make the scripts exist automatically.
- Some suggestion messages still show old command forms. Current rollover uses `si session new --archive-current-session --id ... --title ... --description ...`.
- It mixes canonical settings with `sticky` and `archive_on_end`. The compatibility mappings above describe their actual meaning.
- Its repeated flow `if` keys and one unambiguously misplaced `var` field block are normalized by the dialect parser with warnings. Prefer an explicit step list and correct indentation in new files.

Correct a separate copy for your deployment. Compilation can report expression syntax before reaching the placeholder checks because syntax validation runs before any compile-time shell command. The absence of one particular placeholder error does not make the reference configuration valid.

## Verification and requirement-to-evidence map

The current library suite completed with 24 passing tests and two Caddy-dependent tests excluded from the ordinary test run. `cargo clippy --all-targets -- -D warnings` passed. The ignored Caddy tests are explicit integration checks, not automatically verified by `cargo test` alone.

The protocol E2E uses the real pinned Omni daemon (0.7.2) and real Caddy, with a scripted Claude-compatible provider process for deterministic event behavior. It verifies the interpreter/Omni/Caddy protocol and lifecycle. Separately, a live run using the real authenticated `claude-code-cli` provider returned `SILICON_SMOKE_OK` and reached an idle session. That smoke test validates actual inference connectivity; it does not replace the deterministic concurrency/lifecycle assertions.

| Requirement | Evidence and scope |
| --- | --- |
| Required schema, canonical home, legacy mode mapping | `config::tests::load_resolves_home_and_validates_template_and_modes`; also checks full syntax before Bash and interval/message validation. |
| Preserve source flow order and shell notation | `config::tests::parser_keeps_shell_commands_and_order_without_rewriting_templates`; includes duplicate-key rejection, inline bang diagnostics, and multiline quoted preservation. |
| Deferred login/webhook command evaluation | `config::tests::load_expands_deferred_app_commands_without_invoking_them`; a real executable fixture remains uninvoked during compilation, including quoted paths/arguments. |
| CEL helpers, nested braces, JSON structures | `eval::tests::real_cel_nested_templates_helpers_and_failures`. |
| Bash, DNA, fallback ordering and context | `eval::tests::bash_fallbacks_and_dna_share_environment_without_losing_quoted_separators`. |
| App command quotes, CEL, fallback scope, no compile-time invocation | `eval::tests::app_commands_preserve_argv_and_use_fallbacks_without_running_commands`. |
| Ordered matching conditions, catches, scoped errors | `flow::tests::flows_keep_json_variables_run_all_matching_ifs_scope_catches_and_continue`. |
| Syntax validation of unselected branches | `flow::tests::compile_rejects_invalid_unselected_branches_and_malformed_steps`. |
| Expression-generated flow | `flow::tests::flow_and_branch_expressions_produce_operations`. |
| CLI command shapes and intersecting archive filters | `cli::tests::command_shapes_and_archive_filters_preserve_scope`. |
| Log tail/follow boundary and prefix coloring | `cli::tests::tail_snapshot_follows_exact_read_boundary_and_colors_only_prefix`. |
| Delivery receipts reach flow catches before acknowledgment | `runtime::tests::webhook_ack_waits_for_delivery_and_runs_send_catch_before_continuing` uses the real Omni Rust client over a controlled Unix transport. |
| Delivery acknowledgment precedes turn completion | `runtime::tests::receipts_ack_provider_delivery_before_end_and_native_next_turns_do_not_retire_early`; also `tests/e2e.py`. |
| Retry/late errors and listener recovery | `runtime::tests::retry_and_late_errors_preserve_receipts_and_failed_listeners_are_recreated`. |
| Retirement does not race a new send | `runtime::tests::retirement_rechecks_pending_work_after_waiting_for_send_lock`. |
| Restart admission gate respects nested active work | `runtime::tests::restart_gate_rejects_active_nested_work_then_rejects_new_dispatches`. |
| Restart waits for HTTP compilation and its response | `server::tests::http_compile_blocks_restart_until_its_shell_and_response_finish` runs an actual HTTP request and a blocked Bash expression. The same activity guard covers authentication requests and heartbeat preparation. |
| Suggestion thresholds/cooldown and archived worker retirement | `runtime::tests::suggestions_require_new_messages_and_cooldown_and_archived_workers_retire`; timed protocol E2E checks per-session heartbeats and suggestions. |
| Disconnect still removes capabilities after app cleanup failure | `runtime::tests::disconnect_unhook_failure_still_removes_connection_and_capabilities`. |
| A stale connection cannot recreate workers after disconnect | `runtime::tests::stale_disconnected_connection_cannot_create_workers_or_capabilities`. |
| Archive timestamps and safe physical identity | `state::tests::archive_keeps_original_time_and_safe_disk_identity`; E2E verifies both persistent rollover modes and restart restoration. |
| IAM issuance/isolation/secret handling contract | `auth::tests::application_tokens_are_iam_issued_isolated_and_never_logged` uses controlled CLI fixtures. This is not evidence that all six real apps have authenticated successfully. |
| Caddy route constraints | `proxy::tests::only_local_dns_hosts_can_be_routed`. |
| Real Caddy routing, reload rollback, child cleanup | `proxy::tests::real_caddy_routes_reload_rollback_and_child_cleanup`, run separately with real Caddy; ordinary unit runs mark it ignored. |
| Port 80 denies non-loopback peers and spoofed forwarding headers | macOS integration test `proxy::tests::real_caddy_port_80_only_forwards_loopback_peers`, run with `SILICON_TEST_LAN_IP` and real Caddy; requires a free port 80. |
| Stable-version selection | `update::tests::only_newer_stable_releases_are_candidates`; isolated complete-bundle update checks cover rejection, activation, idle restart, busy restart, and resuming the same persistent UUID. Metadata transport and the hour-long wait are replaced only in the test copy. |
| End-to-end protocol and lifecycle | `python3 tests/e2e.py`: port decrement, HTTP shape/errors, mid-turn injection, ISI environment/access, archives, ephemeral reply, heartbeat, suggestion limits, busy DNA refresh, session rollover, shutdown, restart restoration, disconnect, unchanged YAML bytes. |
| Actual inference | Separate live Claude smoke result: provider `claude-code-cli`, reply `SILICON_SMOKE_OK`, final status `idle`. Recorded delivery acknowledgment 27.82 s and total time 28.07 s in this run; these are observations, not latency guarantees. |
| Complete public release | Complete macOS ARM64 source/package installations and installed-bundle E2E passed. GitHub checks passed on Linux and macOS. The four-platform release workflow and public asset publication remain pending. |
| Live documentation domain | [docs.teamofsilicons.com](https://docs.teamofsilicons.com) serves the static guide over verified HTTPS on Vercel; desktop/mobile navigation and layout were checked in Chrome. |
| Full real-app authentication | Separate isolated integration work is pending. Successful `iam --json` discovery is only one prerequisite. |

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

`SILICON_TEST_BIN_DIR` can select a built/installed interpreter directory for the E2E. It expects a real Omni daemon and real Caddy on `PATH` or through the overrides. It uses a free/available local test setup, creates its own YAML outside the template directories, and verifies that YAML's bytes remain unchanged. The LAN Caddy test additionally requires `SILICON_TEST_LAN_IP` set to this machine's non-loopback IPv4 address and port 80 available.

Source references for maintainers: `src/config.rs`, `src/eval.rs`, `src/flow.rs`, `src/runtime.rs`, `src/auth.rs`, `src/state.rs`, `src/server.rs`, `src/proxy.rs`, `src/cli.rs`, `src/update.rs`, `src/dashboard.html`, `install.sh`, `.github/workflows/release.yml`, and `tests/e2e.py`. The specification is `UNDERSTANDING.md`; the preserved fixture is `stemcell/silicon/silicon.yaml`.

### Maintaining the documentation

The source is `docs/GUIDE.md`, `docs/DIARY.md`, and `docs/shell.html`. With Python Markdown installed, run `python3 docs/render.py` and commit the updated `docs/site/index.html`. Vercel serves that committed static output; it does not build or upload the interpreter, template homes, or local state.
