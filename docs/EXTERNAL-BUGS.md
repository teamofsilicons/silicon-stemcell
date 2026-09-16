# External dependency issues

This ledger records confirmed upstream issues, their impact on Silicon, and the upstream evidence. Publication requirements are listed separately below so they are not presented as runtime defects. New findings belong here with a reproducible command, affected version, expected and actual behavior, and a link to an upstream issue or fix when available.

| Component | Confirmed issue | Impact and status |
| --- | --- | --- |
| DM CLI 0.3.0 with the current DM API | `dm iam --json` exits 1 with `command_failed`: `invalid DM response: missing field app_id at line 1 column 243`. The CLI parses the API's response envelope as bare application data. Reproduced locally and on both Linux release targets. | Resolved in the distribution by pinning the already-published DM 0.7.0, from source `396df5a76f8bc9dea163d807638e8eac972ba8c0`. In a fresh home, real IAM issuance, interpreter login, exact Silicon identity/org, webhook registration/removal, logout, and final unauthenticated status passed. No upstream service or application source was changed. |
| Briefcase CLI 0.2.4 with the current service | In a fresh home, `BRIEFCASE_AUTO_UPDATE=0 briefcase iam --json` exits 1 because the service contract differs from the contract embedded in this client. It reports incompatible `listEntries`, `createFolder`, and `getEntry` endpoint versions before discovery completes. | Resolved in the distribution by pinning published Briefcase 1.1.0, source `951d6ba09e8cb14b5c79eab043aab0a2c70ba7bf`. Live version/contract metadata, `tos>briefcase` discovery, real IAM SLT login, exact Silicon identity/org, authenticated status, logout, and final unauthenticated status passed without content writes. No upstream service or application source was changed. |
| Space Station CLI 0.1.3 | In a fresh home, `spacestation login SLT` requires `--org ORG` or `SPACE_STATION_ORG`; it does not infer the selected organization from the short-lived token. | Handled in Silicon's login integration by deriving `SPACE_STATION_ORG` from the Silicon ID. A newly created home with no saved organization passed real canonical-ID login, authenticated identity/org, logout, and unauthenticated status. Direct CLI users still supply an organization. No upstream change was made. |
| Honeycomb CLI 0.2.0 | Automatic updates have no per-process environment opt-out. In a fresh Honeycomb home, directly running bundled `honeycomb iam --json` replaced the managed command link with a native 0.2.2 executable; a later Silicon installation correctly rejected that unmanaged path. | Silicon runs package operations with a private Honeycomb home under `<home>/.silicon/packages` and explicitly configures `auto_update=false`. Installer construction does the same in its scratch home. Personal Honeycomb state is preserved, and Silicon-driven operations retain pinned binaries. Before using the bundled Honeycomb directly, run `honeycomb config set auto_update false --json` for that Honeycomb home. The direct-CLI self-update defect remains upstream; no upstream patch was made. Managed-home and installer regressions passed. |
| IAM cross-organization SLT consent | An authenticated Carbon owning both organizations receives HTTP 500 `internal_error` when selecting a different organization for the verified `tos>silicon-realtime` app. Same-organization issuance succeeds. Reproduced in production and in a newly created isolated IAM testing environment. | Blocks that cross-organization realtime login before Silicon receives an SLT; Silicon fails closed. Reproduce with `iam --test TEST_ENV --json login --app-id 'tos>silicon-realtime' --grant-org OTHER_OWNED_ORG --approve-scopes`: scope consent is shown, then issuance fails. The isolated test request ID was `01a0aafb-30de-7882-b0e8-d821e52d666d`; production request ID was `01a0aaef-d558-74a0-a1e3-ce802cb0c064`. Same-organization production, testing, and OBO verification passed. No upstream fix was made. |
| IAM, during 3.5.0 verification | IAM issued an unscoped selected-organization Silicon grant that its shared subject-authority check rejected. | Resolved upstream by [IAM PR #19](https://github.com/teamofsilicons/silicon-iam/pull/19). The unchanged 3.5.0 interpreter subsequently completed the six production app logins. |
| Commit CLI 0.1.0 from crates.io | No logout command despite a backend revocation API. | Resolved by [Commit PR #1](https://github.com/teamofsilicons/silicon-commit/pull/1). The Silicon distribution pins the fixed revision `3fe18128282bf65c1f62595ed01e65ec467dba28`. |
| Commit hosted testing environments | The backend selected a testing environment while retaining the production app credential. | Resolved by [Commit PR #2](https://github.com/teamofsilicons/silicon-commit/pull/2). Existing unpaired testing environments still require pairing through the owner-authorized API. |

The [implementation diary](#implementation-diary) records the original reproductions and subsequent verification. The IAM and Commit fixes are historical compatibility context; they are not new modifications made for the 3.6.1 release.

### Honeycomb publication requirements

Honeycomb rejected this public-review request for `tos>silicon-realtime`:

```sh
honeycomb publication request 'tos>silicon-realtime' --revision 1 --message 'Request public review' --json
```

The expected result was a review request. The CLI exited 1 with HTTP 409 `revision_conflict`: “Upload a valid CLI release and wait for IAM private activation before requesting publication.” No request was accepted, and the publication queue remained empty. The app was active but private at revision 1, with IAM revision 8, effective revision 1, and no latest CLI release.

This was a publication compatibility constraint, not a confirmed runtime defect. Honeycomb requires six target payloads, including Windows ARM64 and x86-64; Silicon 3.6.1 shipped four Unix targets. The realtime service was subsequently retired during 4.0.0 preparation, as described below. No Honeycomb code or policy was changed.

### Local interpreter publication requires unsupported IAM permissions

The local interpreter does not need its own user login, IAM data permissions, delegated endpoints, or hosted authentication service. Its intended Honeycomb identity is `tos>silicon`; the live catalog returned 404 for that identifier during 4.0.0 preparation, and no placeholder application was registered.

Honeycomb 0.2.0's [registration contract](https://github.com/teamofsilicons/silicon-honeycomb/blob/eaf1b726675b26a9b9cca82c976b894e25006183/crates/core/src/model.rs) requires an HTTPS webhook receiver, a signing secret of at least 32 characters, and a nonempty webhook category. Its schema permits an empty IAM scope list, but the hosted IAM service rejects it. The following authorized reduction on the retiring app reproduced the incompatibility:

```sh
iam app update 'tos>silicon-realtime' \
  --app-scope '{"iam":[],"external":[]}' \
  --obo-endpoints '[]' --webhook-scope updates --json
```

Expected: remove all data permissions for an application that no longer needs them. Actual: `validation_failed`, `app_scope: must declare 1-100 unique permissions`; IAM request `01a0ab66-f98a-7ba2-be98-796ed5216097`. The equivalent Honeycomb update returned exit code 0 with a **pending**, rejected-by-IAM configuration, not a successful activation (operation `f7a3704d-486a-4b73-ae53-1919f137b6f9`, request `01a0ab66-baa1-75fb-9370-892aec9c1ac8`). Callers must inspect operation state, not only the CLI exit code.

Publishing this interpreter therefore requires upstream support for a standalone local CLI with zero IAM permissions and no authentication backend. No unnecessary scope, fake webhook, or replacement hosted authentication service was added. The six-target archive can be prepared independently; Windows entries are native launchers requiring WSL2, and do not claim a native Windows interpreter runtime. See [Honeycomb packaging instructions](https://github.com/teamofsilicons/silicon-stemcell/blob/main/docs/HONEYCOMB.md). An archive passing validation is not evidence of registration, public review, or publication.

### Retired realtime app still requires an IAM operator to delete its record

Honeycomb exposes configuration, release, publication, and reconciliation operations, but no app deletion or retirement operation. IAM's documented terminal deletion endpoint, `POST /api/v1/admin/applications/{app_id}/decisions` with `decision: "delete"`, requires platform-administrator authority, all application administration capabilities, and verified-channel step-up. The signed-in owning-organization administrator received HTTP 403 from the read-only platform inventory endpoint (request `01a0ab65-e9a2-7540-8cd2-93f6d25cee85`). Organization administration does not grant platform administration, and no access-control workaround was attempted.

The supported cleanup completed on 16 September 2026: `tos>silicon-realtime` was renamed **Silicon Realtime (retired)** at Honeycomb revision 3 / IAM revision 14; its OBO endpoints and external scopes were removed, membership access was removed, and its webhook subscription was reduced to updates only. The mandatory `self.identity.read` scope remains because IAM rejects removing the final scope. The record remains private and IAM-verified; this is a retired registration, not a deleted or platform-disabled app. All 23 retained access/refresh tokens received successful revocation responses and subsequently introspected as inactive. The two dedicated realtime test environments, `01a0aae6-4422-7543-a548-00dfaef436be` and `01a0aaf6-daa6-7c31-a124-12e0add582b7`, are deleted with their normal 30-day recovery window.

An authorized IAM platform operator must apply terminal deletion to `tos>silicon-realtime` (application UUID `01a0aae0-60b7-7462-9c06-854ccbd6baf7`) and then reconcile its Honeycomb record. The owning-organization session cannot perform that final operation.

One production test Silicon created for realtime verification remains to be removed. IAM requires a fresh email verification code for that action. During cleanup, Chrome reported that another extension interface was blocking automation, so the code could not be retrieved and the pending prompt was cancelled. Retry removal when browser access is available; this is a temporary cleanup blocker, not a permanent IAM limitation. The exact test identity and cleanup details remain in private task records.
