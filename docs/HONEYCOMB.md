# Honeycomb supplies application dependencies

Silicon itself is distributed through [GitHub Releases](https://github.com/teamofsilicons/silicon-stemcell/releases) and the [Unix and Windows installers](https://docs.teamofsilicons.com/#installation). It does not need a Honeycomb listing, public-review request, or its own IAM app registration.

Honeycomb is installed independently from its checksum-verified latest release. Application CLIs are not copied into interpreter release bundles.

On every connection, Silicon runs `honeycomb install 'app' --json` for each configured or previously registered canonical app ID, plus `iam` for token issuance and `ting` for notification delivery. When an install fails but a working copy is already installed, the failure is logged and the installed copy is used, so reconnecting after a reboot does not depend on the network. Ting is installed, authenticated, and registered even when it is absent from `silicon.apps`. No version argument or persistent pin is written. Honeycomb selects the latest published package and handles verification and installation. Legacy explicit executable commands stay under the user's control.

The package registry is isolated beneath `<SILICON_HOME>/.silicon/packages`; app credentials remain under the original Silicon home. Interpreter updates do not disable app updates or wrap app commands to suppress updates. An old interpreter-imposed `auto_update: false` setting in the private package home is restored to Honeycomb's default once; later preferences are preserved. Honeycomb requires that setting, so a home left without it — as 4.0.6 did when it deleted the setting — is repaired before any package command runs. Authentication still uses the separate 48-hour check cache.

After authentication, `silicon.app_configs` objects are passed to the matching CLI with `config set JSON`. Commands receive `SILICON_HOME` and `SILICON_ORG`. Inside an ISI, `si app install 'app'` adds the canonical ID to the current YAML after installation and authentication; `si app uninstall 'app'` removes its package, app entry, and config. IAM and Ting remain required dependencies.

Application IDs are bare handles with explicit owning organizations in IAM and Honeycomb. Bundle IDs remain `org>bundle`; release selectors such as `briefcase>test@2.1.0` retain their channel/version meaning. Migrate existing registries with Honeycomb's explicit IAM mapping before reconnecting; see the [identifier migration](PUBLIC-IDENTIFIER-MIGRATION.md).

Only Ting is registered to the local Silicon URL. The interpreter retains and reuses its webhook ID, acknowledges durably stored `request.tings` batches with an empty 204, and runs `ting unhook ID --json` on disconnect. Applications publish through Ting instead of managing separate interpreter webhooks. See the [Ting registration contract](GUIDE.md#ting-registration-and-app-notifications).

Those applications keep their own IAM identities and authentication flows. The interpreter uses them on behalf of the configured Silicon identity; it does not need a separate application identity for distribution.

The earlier proposal to publish `tos>silicon` on Honeycomb was withdrawn. No application was registered, no release was uploaded, and no public-review request was submitted for that identity. The interpreter-specific Honeycomb packaging scripts and CI check were removed. The retired realtime app is separate; its remaining cleanup is recorded in the [external issue ledger](EXTERNAL-BUGS.md).
