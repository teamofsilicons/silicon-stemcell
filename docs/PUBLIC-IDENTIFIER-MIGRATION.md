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

Compile rejects old IDs and missing organisations with actionable configuration errors. Reconnect rebuilds local hosts and webhook descriptors; update consumers of those addresses using the new connection descriptor. Restore the backed-up YAML/state and old binary only as part of the coordinated cross-service rollback.
