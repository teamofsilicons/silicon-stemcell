# Honeycomb supplies application dependencies

Silicon itself is distributed through [GitHub Releases](https://github.com/teamofsilicons/silicon-stemcell/releases) and the [Unix and Windows installers](https://docs.teamofsilicons.com/#installation). It does not need a Honeycomb listing, public-review request, or its own IAM app registration.

Honeycomb remains part of the interpreter bundle for two purposes:

- Release construction downloads the published IAM, Space Station, DM, Briefcase, Waveform, Commit, Remind, and Hook CLIs through Honeycomb.
- Configured applications can be installed into the selected Silicon home's managed package directory.

Those applications keep their own IAM identities and authentication flows. The interpreter uses them on behalf of the configured Silicon identity; it does not need a separate application identity for distribution.

The earlier proposal to publish `tos>silicon` on Honeycomb was withdrawn. No application was registered, no release was uploaded, and no public-review request was submitted for that identity. The interpreter-specific Honeycomb packaging scripts and CI check were removed. The retired realtime app is separate; its remaining cleanup is recorded in the [external issue ledger](EXTERNAL-BUGS.md).
