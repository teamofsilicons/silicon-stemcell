# Interpreter identifier migration

Use IAM's verified old-to-new mapping after resolving handle collisions. Silicon IDs are now `si:handle`, Carbon IDs are `c:handle`, and application IDs are bare handles such as `dm`. An organization's identity and authority remain explicit; bundle IDs retain `org>bundle` and Honeycomb release selectors retain forms such as `briefcase>test@2.1.0`.

For 5.0.1, download the [migration script](https://github.com/teamofsilicons/silicon-stemcell/releases/download/v5.0.1/migrate-identifiers.py) and [this procedure](https://github.com/teamofsilicons/silicon-stemcell/releases/download/v5.0.1/PUBLIC-IDENTIFIER-MIGRATION.md), and verify both against the release’s [SHA256SUMS](https://github.com/teamofsilicons/silicon-stemcell/releases/download/v5.0.1/SHA256SUMS) before use. The script is also available as `scripts/migrate-identifiers.py` in the matching source checkout.

This procedure updates local interpreter configuration and state. Coordinate it with IAM, Honeycomb, Ting, and the application's own session migration; a local code update does not prove that those services have completed their cutover.

1. Pause incoming work and reconcile pending external operations. Disconnect each migrating YAML with `silicon disconnect /absolute/path/silicon.yaml` using its original path (or old Silicon ID), then stop the interpreter with `silicon stop` before editing YAML or local state. Disconnect removes the saved connection descriptor while retaining the home's sessions, Ting webhook ID and inbox, so startup cannot restore the old connection before the explicit reconnect below. Stop Honeycomb's package maintenance worker while migrating its registry. Keep writers stopped until verification completes.
2. Back up the YAML, the existing Silicon home's `.silicon` and application credential directories, and the interpreter state directory (`SILICON_INTERPRETER_HOME`, or `~/.silicon-interpreter`). Keep the same canonical YAML path, `SILICON_HOME`, Silicon token, ISI names, Omni session UUIDs, and session files.
3. Update only identity-bearing configuration using the approved mapping:

   ```yaml
   silicon:
     id: si:assistant
     org_id: my-org
     token: EXISTING_SILICON_TOKEN
     SILICON_HOME: /existing/silicon/home
     apps:
       - dm
       - waveform
     app_configs:
       waveform:
         default_tts_provider: google
   ```

   This is a fragment; retain the rest of the YAML. `silicon.org_id` is the IAM owning organization; `SILICON_ORG` defaults to it and may still select an authorized application organization explicitly. Update ID-returning shell expressions, actor comparisons, configured recipients (`worker@si:assistant`, `@c:alice`), and deployment-provided identity variables. Preserve explicit executable commands; use `! command` in `login` for an intentional single-word executable so it is not interpreted as a managed app ID. Preserve bundle selectors, ISI/session names, opaque notification type names, message text, signed payloads, historical logs, and secrets. Do not infer authority by stripping a suffix from an unverified ID.
4. Migrate Honeycomb's installed registry with its approved mapping tool from the matching Honeycomb source checkout. Locate each context's `installed.json` beneath `<SILICON_HOME>/.silicon/packages`; production and test contexts must be handled separately. Preview first, then apply with a backup:

   ```sh
   python3 /path/to/silicon-honeycomb/scripts/migrate_identifier_schema.py \
     --installed /absolute/context/installed.json --map /private/iam-id-map.json
   python3 /path/to/silicon-honeycomb/scripts/migrate_identifier_schema.py \
     --installed /absolute/context/installed.json --map /private/iam-id-map.json \
     --apply --backup-dir /private/backups/honeycomb-identifiers
   ```

   Keep physical package paths, command links, aliases, and archive bytes unchanged. The Honeycomb mapper rewrites registry identities and refuses collisions; do not replace its checks with a text substitution. See [Honeycomb's migration procedure](https://github.com/teamofsilicons/silicon-honeycomb/blob/main/docs/IDENTIFIER-MIGRATION.md).
5. Migrate the interpreter's identity-bearing local state, using the same approved Honeycomb-format map:

   ```sh
   python3 /path/to/migrate-identifiers.py --home /existing/silicon/home \
     --map /private/iam-id-map.json --old-id assistant:my-org --org-id my-org
   python3 /path/to/migrate-identifiers.py --home /existing/silicon/home \
     --map /private/iam-id-map.json --old-id assistant:my-org --org-id my-org \
     --apply --backup-dir /private/backups/interpreter-identifiers
   ```

   `--org-id` is the selected application organization (`SILICON_ORG`), which may differ from the owning `silicon.org_id`; it must match the saved selection. The script previews by default. It maps exact application references in `.silicon/auth-apps.json`, renames the complete mapped Ting actor directory while preserving all organization subdirectories, the retained webhook ID, pending inbox and deduplication records, and invalidates cached authentication checks/grants after backing them up. It does not edit YAML, rotate tokens, move sessions, or rewrite saved delivery bodies. Run this Python script inside the Silicon WSL2 distribution on Windows. Supply `--interpreter-home` when using a nondefault interpreter state directory, and `--testing-environment-id` for an explicitly mapped test world. Inspect the old `.silicon/auth-apps.json`. For each saved bare executable that must remain a command, add `--command NAME` to both preview and apply; the script retains it as `! NAME`, even when its name also matches a canonical app ID. Bare entries must otherwise match an application's canonical ID in the approved map. Missing mappings, ambiguous commands and conflicting destinations must be resolved before proceeding.
6. With the upgraded interpreter and compatible application CLIs, compile and reconnect the same YAML path:

   ```sh
   silicon compile /absolute/path/silicon.yaml
   silicon connect /absolute/path/silicon.yaml
   silicon ping si:assistant
   ```

   Old Silicon/application identifiers and missing organizations fail configuration validation. Fresh IAM grants bind the complete Silicon ID, its explicit owning organization, and the selected application organization; old authentication cache entries are not authority. Application session stores follow each application's own cutover policy.
7. Read the returned connection host and update any external local-route consumers. When the handle and owning organization are DNS labels, the host remains `assistant.my-org.localhost`; underscores or boundary hyphens in either component use the interpreter's encoded host. Verify the retained Ting hook is registered at the new route, pending work and deduplication records remain present, application discovery returns bare IDs, authentication uses the expected actor and organization, and existing session history is still available before resuming delivery.

Before reopening writes, rollback means restoring the coordinated configuration/state snapshots and matching previous binaries alongside IAM/Honeycomb's rollback. After new writes, stop traffic and reconcile those writes before restoring anything; a local-only rollback can lose work or restore incompatible identities.
