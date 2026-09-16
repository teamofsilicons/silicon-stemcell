# Silicon 3.6.0

A local Rust interpreter for `silicon.yaml`: connect Silicons, route events through CEL flows, run ISIs with Silicon Omni, and manage their sessions and IAM applications.

```sh
curl -fsSL https://github.com/teamofsilicons/silicon-stemcell/releases/download/v3.6.0/install.sh | sh
```

[Release notes and checksums](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v3.6.0).

Version 3.6.0 adds connection setup scripts, Honeycomb application installation, canonical IAM app IDs, DNA source attribution, Space Station telemetry, settings, and organization-scoped live updates.

The installer sets up Silicon, Caddy, IAM, Omni, Honeycomb, DM, Briefcase, Waveform, Commit, Remind, Hook, and Space Station. macOS and Linux, ARM64 and x86-64. Default prefix: `~/.local/share/silicon`.

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
