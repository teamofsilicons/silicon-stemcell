# Silicon 3.5

A local Rust interpreter for `silicon.yaml`: connect Silicons, route events through CEL flows, run ISIs with Silicon Omni, and manage their sessions and IAM applications.

```sh
curl -fsSL https://github.com/teamofsilicons/silicon-stemcell/releases/download/v3.5.0/install.sh | sh
```

[Release notes and checksums](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v3.5.0).

This branch prepares unpublished 3.5.1 with reconnect/disconnect fixes and Commit logout. See the [source installation guide](docs/GUIDE.md#build-the-complete-installation-from-source) to test it; the command above installs public 3.5.0.

The bundle includes Silicon, Caddy, IAM, Omni, DM, Briefcase, Waveform, Commit, Remind, and Hook. macOS and Linux, ARM64 and x86-64. Default prefix: `~/.local/share/silicon`.

```sh
silicon compile /path/to/silicon.yaml
silicon connect /path/to/silicon.yaml
silicon ls
silicon web
```

Read [the live documentation](https://docs.teamofsilicons.com), the [complete guide](docs/GUIDE.md), [implementation diary](docs/DIARY.md), and [source specification](UNDERSTANDING.md). The preserved `stemcell/` directory is reference material, with placeholders and unfinished example expressions; create your own configuration using the guide.

```sh
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
OMNI_DAEMON=/path/to/omnid SILICON_CADDY=/path/to/caddy sh tests/e2e.sh
```

The E2E runs the real interpreter, Omni daemon and Caddy against a scripted provider. A separate live Claude smoke test verifies actual inference. See the guide for the scope of each check.

MIT · [dependency licenses](LICENSES/)
