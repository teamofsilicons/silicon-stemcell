# Silicon 4.0.0

A local Rust interpreter for `silicon.yaml`: connect Silicons, route events through CEL flows, run ISIs with Silicon Omni, and manage their sessions and IAM applications.

macOS and Linux:

```sh
curl -fsSL https://github.com/teamofsilicons/silicon-stemcell/releases/download/v4.0.0/install.sh | sh
```

Windows PowerShell:

```powershell
irm https://github.com/teamofsilicons/silicon-stemcell/releases/download/v4.0.0/install.ps1 | iex
```

[Release notes and checksums](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v4.0.0).

**Upgrading from 3.5.x:** rerun the installer above once into your existing installation prefix, even if an older update has already changed the reported version. The old updater cannot add Honeycomb and Space Station. Stop the running interpreter with `silicon stop` before reinstalling, then restart with `silicon serve`. For a custom prefix, set `SILICON_PREFIX` on the `sh` side of the pipeline; see the [migration instructions](https://docs.teamofsilicons.com/#upgrading-from-35x).

Version 4 focuses on the local interpreter. It retains setup scripts, Honeycomb app installation, canonical IAM IDs, DNA source attribution, Space Station telemetry, local ping, logs, and the dashboard. The hosted realtime service and remote login/watch commands have been removed.

The installer sets up Silicon, Caddy, IAM, Omni, Honeycomb, DM, Briefcase, Waveform, Commit, Remind, Hook, and Space Station. macOS, Linux, and Windows on ARM64 and x86-64. Windows uses WSL2; first setup may require administrator access and a restart. Windows ARM64 is a preview pending a full runtime test on ARM64 Windows hardware. The Unix default prefix is `~/.local/share/silicon`.

```sh
silicon compile /path/to/silicon.yaml
silicon connect /path/to/silicon.yaml
silicon ls
silicon web
```

Start with [installation and your first Silicon](https://docs.teamofsilicons.com/#start-here). The [complete guide](docs/GUIDE.md) covers usage, configuration, application contracts, and development. See the [implementation diary](docs/DIARY.md), [external dependency issues](docs/EXTERNAL-BUGS.md), [source specification](UNDERSTANDING.md), and [IAM application requirements](IAM.md). The preserved `stemcell/` directory is reference material, with placeholders and unfinished example expressions; create your own configuration using the guide.

```sh
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
OMNI_DAEMON=/path/to/omnid SILICON_CADDY=/path/to/caddy sh tests/e2e.sh
```

The E2E runs the real interpreter, Omni daemon and Caddy against a scripted provider. A separate live Claude smoke test verifies actual inference. See the guide for the scope of each check.

MIT · [dependency licenses](LICENSES/)
