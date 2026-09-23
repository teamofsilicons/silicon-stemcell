# Silicon 5.0.1

A local Rust interpreter for `silicon.yaml`: connect Silicons, route events through CEL flows, run ISIs with Silicon Omni, and manage their sessions and IAM applications.

macOS and Linux:

```sh
curl -fsSL https://github.com/teamofsilicons/silicon-stemcell/releases/download/v5.0.1/install.sh | sh
```

Windows PowerShell:

```powershell
irm https://github.com/teamofsilicons/silicon-stemcell/releases/download/v5.0.1/install.ps1 | iex
```

[Release notes and checksums](https://github.com/teamofsilicons/silicon-stemcell/releases/tag/v5.0.1).

**Upgrading from 4.x requires an identifier migration:** configurations now use `silicon.id: si:handle`, a separate `silicon.org_id`, and bare app IDs such as `dm`. Follow the [migration procedure](docs/PUBLIC-IDENTIFIER-MIGRATION.md) before upgrading or reconnecting an existing home. Disconnect and stop the old interpreter first; installers do not rewrite YAML or migrate local state.

**Upgrading from 4.0.x or earlier:** also remove `silicon.webhooks` and `silicon.webhook`, retain app IDs in `silicon.apps`, and update flows to read `request.tings` batches. Only Ting registers the local webhook; IAM and Ting are installed through Honeycomb even when omitted from `apps`. See the [Ting migration](https://docs.teamofsilicons.com/#ting-migration) before restarting.

**Upgrading from 4.0.6–4.0.8:** replace any `sticky` and `archive_on_end` fields with `primary_send_mode` and `session_type` using the [migration table](https://docs.teamofsilicons.com/#legacy-mode-fields-removed), then run `silicon update` and restart with `silicon stop` and `silicon serve`. 4.0.9 retains session-addressed ephemeral history and adds process, sender, and flow details to logs.

**Upgrading from 4.0.5 or earlier:** rerun the installer above once into your existing installation prefix, even if an older update has already changed the reported version. The old updater requires bundled app executables; current releases install apps independently through Honeycomb. Stop the running interpreter with `silicon stop` before reinstalling, then restart with `silicon serve`. For a custom prefix, set `SILICON_PREFIX` on the `sh` side of the pipeline; see the [migration instructions](https://docs.teamofsilicons.com/#upgrading-from-35x).

Silicon is distributed through GitHub Releases and the installers above. It does not need a Honeycomb listing or its own IAM app registration. Honeycomb supplies the application dependencies.

Version 5 uses the new IAM and Honeycomb identifier contract for the local interpreter. 5.0.1 strengthens migration, authentication-cache isolation, and retained delivery handling after 5.0.0. It retains setup scripts, Honeycomb app installation, canonical IAM IDs, DNA source attribution, Space Station telemetry, local ping, logs, and the dashboard. The hosted realtime service and remote login/watch commands have been removed.

The installer sets up Silicon, Caddy, Omni 0.9.0, and the latest standalone Honeycomb. Each connection installs IAM, Ting, and the latest configured apps through Honeycomb without version constraints; apps keep their own automatic updates. macOS, Linux, and Windows on ARM64 and x86-64. Windows uses WSL2; first setup may require administrator access and a restart. Windows ARM64 is a preview pending a full runtime test on ARM64 Windows hardware. The Unix default prefix is `~/.local/share/silicon`.

```sh
silicon compile /path/to/silicon.yaml
silicon connect /path/to/silicon.yaml
silicon ls
silicon web
```

Start with [installation and your first Silicon](https://docs.teamofsilicons.com/#start-here). The [complete guide](docs/GUIDE.md) covers usage, configuration, application contracts, and development. See the [implementation diary](docs/DIARY.md), [external dependency issues](docs/EXTERNAL-BUGS.md), [source specification](UNDERSTANDING.md), and [IAM application requirements](IAM.md). The `stemcell/` directory is reference material with a batch-aware flow, identity placeholders, and deployment-specific scripts; create your own configuration using the guide.

```sh
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
OMNI_DAEMON=/path/to/omnid SILICON_CADDY=/path/to/caddy sh tests/e2e.sh
```

The E2E runs the real interpreter, Omni daemon and Caddy against a scripted provider. A separate live Claude smoke test verifies actual inference. See the guide for the scope of each check.

MIT · [dependency licenses](LICENSES/)
