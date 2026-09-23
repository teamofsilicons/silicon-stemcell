# Interpreter identifier migration

Use the IAM-verified mapping after collision resolution. Stop/disconnect each interpreter before editing its YAML. Retain the YAML's canonical path, `SILICON_HOME`, token, ISI names and session files: those identify existing local work.

```yaml
silicon:
  id: si:assistant
  org_id: my-org
  token: ${EXISTING_TOKEN}
```

`silicon.id` is the full global `si:<handle>`; `silicon.org_id` selects IAM organisation authority and is no longer inferred from the ID. Change application selectors from `tos>dm` to `dm`, likewise `iam`, `briefcase`, etc. Preserve bundle selectors (`tos>interface`) and explicit executable commands. Update ID-returning shell expressions, flow actor comparisons, configured recipients (`worker@si:assistant`) and external webhook routing that intentionally uses the old public ID. Do not replace arbitrary task/message text or ISI session addresses.

Back up the YAML and `.silicon` state first. Migrate the Honeycomb local registry with its explicit mapping tool before reconnecting. Keep physical installation paths and existing home directories; the Honeycomb mapper handles aliases without moving package bytes. Update deployment-provided app/Silicon ID environment variables as well.

Run `silicon compile /absolute/path/silicon.yaml`, then reconnect that same path with the upgraded interpreter. Authentication sends both the new Silicon ID and explicit organisation. Its cached authentication checks are now keyed by ID, organisation and command. Restart clears the old in-memory checks. Existing Omni session UUIDs and on-disk session files remain unchanged. Do not mint replacement tokens or move session directories merely to change an ID.

For existing 4.1 Ting state, move the entire identity namespace with the stopped interpreter. Use a private JSON mapping containing exactly the IAM-verified old ID, new ID, and owning organization, for example `{"old_id":"assistant:my-org","new_id":"si:assistant","org_id":"my-org"}`. The selected app-grant organization may differ; all its subdirectories move together.

```sh
python3 scripts/migrate-ting-state.py --home /absolute/silicon-home --mapping /private/identity-map.json
python3 scripts/migrate-ting-state.py --home /absolute/silicon-home --mapping /private/identity-map.json --apply --stopped --backup-dir /private/new-ting-backup
```

The preview writes nothing. Apply requires a new backup directory, verifies every copied file by SHA-256, and atomically renames `.silicon/ting/<old-id>` to `.silicon/ting/<new-id>`. A destination collision, wrong owning organization, or symlink stops the move. Retained webhook IDs, delivery deduplication, pending batches, and selected-organization subdirectories remain byte-identical. A completed retry has a missing source: inspect the backup receipt and destination hashes instead of merging or deleting either namespace. Reconnecting refuses a different identity namespace in the same home.

Pending batches can contain historical event type names such as `tos>dm.sync.changed`. Preserve those bytes; either finish pending work before the cutover, or keep the old event topic explicitly recognized alongside the new topic until the backed-up inbox is empty. Do not reinterpret notification text or silently drop a pending batch.

In the backed-up `.silicon/auth-apps.json`, map only whole registered canonical application IDs through the same verified app map; preserve explicit executable commands. Reject duplicate destinations. Remove the old `.silicon/auth-checked.json` and `.silicon/auth-grants.json` from the active home after saving their copies, then explicitly authenticate each managed app with `si auth setup APP` within the connected Silicon. A locally reported authenticated session alone is not a proof that its cached subject/audience has migrated. This is cache invalidation, not deletion of app data; preserve provider credentials, tokens, and session files.

Compile rejects old IDs and missing organisations with actionable configuration errors. Reconnect rebuilds local hosts and webhook descriptors; update consumers of those addresses using the new connection descriptor. Restore the backed-up YAML/state and old binary only as part of the coordinated cross-service rollback.
