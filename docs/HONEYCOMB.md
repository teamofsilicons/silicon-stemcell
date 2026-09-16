# Preparing a Honeycomb release

Honeycomb publication is currently blocked by its IAM registration requirements for this local-only interpreter. [The external issue ledger](EXTERNAL-BUGS.md#local-interpreter-publication-requires-unsupported-iam-permissions) records the reproduced error. These steps prepare a reviewable package; they do not claim that `tos>silicon` exists in the catalog or is publicly installable.

Download the six verified GitHub release archives and their `SHA256SUMS` into one directory. With Python 3.12 or newer, prepare the package:

```sh
python3 scripts/prepare_honeycomb.py \
  --artifacts ./release-assets --version 4.0.0 --output ./honeycomb-package
honeycomb validate ./honeycomb-package
honeycomb pack ./honeycomb-package --output silicon-honeycomb-4.0.0.tar.gz
honeycomb validate silicon-honeycomb-4.0.0.tar.gz
```

The preparation script verifies every source checksum and version, rejects archive links and traversal, preserves all bundled dependencies and licenses, and generates `honeycomb.yaml`. It exports all 16 distribution commands so the interpreter can find its bundled dependencies on PATH. Unix executable paths are `bin/COMMAND`; Windows paths are `COMMAND.exe`. Each of the six target roots is self-contained because Honeycomb installs only the current platform's root. The Windows payload includes its matching Linux archive and requires WSL2; package validation alone does not prove Windows or WSL execution.

Honeycomb permits up to 512 MiB compressed, 2 GiB expanded, and 50,000 archive entries. Its validator is the authority for the final package format. Run the packaged commands and the complete interpreter smoke test on each actual supported platform before publication. Honeycomb rejects command names already on PATH, including the Honeycomb installer itself. Its documented aliases can resolve installation collisions, but dependency aliases must also preserve the interpreter's expected command resolution. Private installation and collision handling remain unverified while registration is blocked; do not treat a prepared archive as an installable published product.

Once upstream supports a local CLI registration without unnecessary permissions, register the actual interpreter as `tos>silicon` with an accurate WSL requirement and no realtime service. Read the current revision before uploading:

```sh
honeycomb apps get 'tos>silicon' --json
honeycomb --idempotency-key silicon-4.0.0-upload-001 \
  releases upload 'tos>silicon' silicon-honeycomb-4.0.0.tar.gz --revision REVISION --json
honeycomb releases list 'tos>silicon' --json
```

Wait for the returned operation to be accepted and IAM private activation to be effective. Verify private installation and execution before requesting public review, using the latest application revision:

```sh
honeycomb --idempotency-key silicon-4.0.0-review-001 \
  publication request 'tos>silicon' --revision REVISION \
  --message 'Local Silicon interpreter; Windows launchers require WSL2. No hosted realtime service or application data permissions.' --json
honeycomb publication get 'tos>silicon' --json
```

Keep the same idempotency key and bytes when retrying an uncertain upload. Changed package bytes require a new semantic version. Review acceptance, validator approval, IAM activation, and public archive access are distinct states; report publication only after the effective public state and anonymous installation both succeed.
