# Installing Silicon

## Install the Silicon CLI

macOS / Linux:

```bash
python3 -m pip install --upgrade silicon-cli
```

Windows:

```powershell
py -m pip install --upgrade silicon-cli
```

The PyPI package installs the `silicon` command. Pull a complete Glass team with:

```bash
silicon pull
```

Paste the team setup token generated in Glass when prompted. The command creates
one local instance per team Silicon, hydrates the current Stemcell, writes its
Glass configuration, installs its runtime dependencies, registers it, and
starts it.

## Requirements

- Python 3.9+
- A Glass team setup token
- Docker for the default isolated runtime

The default Docker runtime includes Node.js 22, Silicon Interface CLI, Silicon
Browser, Claude Code, Codex, Python, and Git. On supported Linux hosts,
`silicon pull` can install and configure Docker when it is missing.

To opt into the host-local runtime instead:

```bash
SILICON_RUNTIME=local silicon pull
```

Host-local installs require Node.js 22+, npm, Silicon Browser, and at least one
authenticated manager backend (Claude Code or Codex) on the host.

## Day-to-day commands

Use `silicon help` as the canonical command reference. The core lifecycle is:

```bash
silicon list
silicon start <name>
silicon update <name>
silicon update status <name>
```

`silicon stop` stops the main Silicon but deliberately leaves its Glass agent
online for remote status and restart commands. Use `silicon stop --full` or
`silicon agent stop` when the sidecar must also stop.

`silicon update <name>` is the complete instance-update operation. It resolves
the highest stable `vMAJOR.MINOR.PATCH` tag from the canonical Stemcell Git
repository, pins and verifies its exact commit and contents, and fully
pre-stages code and dependencies while the Silicon remains available. It then
announces maintenance to Glass, waits for active tasks to reach a safe
boundary, creates and verifies a recovery checkpoint, stops, activates,
restarts, and health-checks the Silicon. Do not manually stop it first: the
updater needs the running process to perform the task-safe drain. Upgrade the
machine-level CLI independently with
`python3 -m pip install --upgrade silicon-cli` or `silicon script update`.

Glass credentials pulled into an instance are stored in `.glass.json`. Silicon
Interface and Glass provide contact transport, media, STT/TTS, crons,
take-back, remote-browser events, backups, and control.
