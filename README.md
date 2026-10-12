# Silicon 7.0.1

A local Rust interpreter for `silicon.yaml`: connect Silicons, route JSON events through YAML flows, run internal Silicons with Omni, and manage sessions and applications.

Silicon sends no telemetry, including to Space Station. Diagnostic logs and session history stay local.

Silicon uses [Silicon Accounts](https://accounts.teamofsilicons.com/) for Carbon and Silicon identities, [Silicon Apps](https://apps.teamofsilicons.com/) for installation, and the [Developer portal](https://developers.teamofsilicons.com/) for publication. Every action belongs to an account: `c:handle` or `si:handle`.

On Linux with Silicon Apps installed:

```sh
silicon-apps install silicon --yes
```

For complete setup on macOS and Linux (including Silicon Apps):

```sh
curl -fsSL https://github.com/teamofsilicons/silicon-stemcell/releases/download/v7.0.1/install.sh | sh
```

Windows PowerShell:

```powershell
irm https://github.com/teamofsilicons/silicon-stemcell/releases/download/v7.0.1/install.ps1 | iex
```

[Release notes and checksums](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v7.0.1). Native catalog installation currently targets Linux; macOS and Windows use the complete bundle installers. Windows uses WSL2; first setup may require administrator access and a restart. Windows ARM64 remains a preview.

**Upgrading to 7.0:** stop the interpreter, back up your Silicon home, remove the retired ownership fields from YAML, and use your Silicon Accounts identity and token. Keep the same YAML path and home to retain sessions. Follow the [migration procedure](docs/PUBLIC-IDENTIFIER-MIGRATION.md), then compile and reconnect. The installer does not rewrite your configuration.

```sh
silicon compile /path/to/silicon.yaml
silicon connect /path/to/silicon.yaml
silicon ls
silicon web
```

The installer supplies Silicon, Caddy, Omni 0.9.1, and Silicon Apps. Each connection installs Silicon Accounts, Ting, and the configured apps. App sessions persist until expiry or logout. The dashboard, built with the [Silicon UI foundation](https://ui.teamofsilicons.com/), also remembers local access across browser and interpreter restarts and provides explicit logout.

Declare apps under each ISI's `apps`; the interpreter installs and authenticates their union, including apps supplied by Starter ISIs. Reusable `starter:gene:ID`, `starter:function:ID`, and `starter:isi:ID` definitions are cached in `.fromstarter` and refreshed on connection. Native YAML flows support loops, functions, conditionals, aggregation, and durable delivery retries.

Start with [installation and your first Silicon](https://docs.teamofsilicons.com/#start-here). The [complete guide](docs/GUIDE.md) covers configuration, flows, sessions, application contracts, and development. See [Accounts integration](ACCOUNTS.md), [Apps installation and publication](docs/APPS.md), and the [source specification](UNDERSTANDING.md). The `stemcell/` directory contains reference material; create your own configuration using the guide.

```sh
cargo build --locked --release
cargo clippy --locked --all-targets -- -D warnings
```

MIT · [dependency licenses](LICENSES/)
