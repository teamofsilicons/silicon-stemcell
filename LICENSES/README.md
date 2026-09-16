# Bundled third-party software

This bundle uses the exact Honeycomb application packages identified below. Their manifests do not identify a source build commit. The fixed upstream references document license provenance; they are not claims that a catalog binary was built from that exact revision. Catalog release versions and upstream Cargo versions can differ.

The IAM archive includes its own license, copied verbatim. The other seven application archives contain no LICENSE, COPYING, or NOTICE file. Where available, the corresponding upstream license text is retained here; otherwise a notice records the upstream metadata without adding license terms. Silicon's MIT license does not replace the terms of bundled software. Transitive dependencies retain their own licenses.

## iam (Honeycomb tos>iam 1.11.0)

- License: Proprietary (the published binary package)
- Upstream license reference: https://github.com/teamofsilicons/silicon-iam/blob/c79508255244f0d9390c765ce9f92b95228e0353/crates/cli/LICENSE
- Package SHA-256: `0e662f15d9b0d1b18346a15f65f349d12dc959e3115031bcae6ce9777cee374f`
- File: iam-LICENSE.txt
- The package license is proprietary and differs from the Apache-2.0 CLI source license at the linked reference. The package text is preserved; no Apache-only grant is asserted for this binary distribution.

## dm (Honeycomb tos>dm 0.7.0)

- License: LicenseRef-Proprietary (upstream CLI metadata)
- Upstream license reference: https://github.com/teamofsilicons/silicon-dm/blob/9908fec4d975e5fd8c41caf77480ff4e84a62dd9/crates/cli/Cargo.toml
- Package SHA-256: `59cb1f2fc3b961469c213b09dba939f534c14c761453cc66e1cdba190283183e`
- File: dm-NOTICE.txt

## briefcase (Honeycomb tos>briefcase 1.1.0)

- License: Apache-2.0 (upstream CLI license)
- Upstream license reference: https://github.com/teamofsilicons/silicon-briefcase/blob/abb3fb74aaf3d54ab7776a68a11f3d586cc71c13/clients/rust/crates/briefcase-cli/LICENSE
- Package SHA-256: `a2a1fce14171d3b03f0c8a5942f17c69079b15ebad9b783d680e0ce05b85ccb0`
- File: briefcase-LICENSE.txt

## waveform (Honeycomb tos>waveform 0.1.2)

- License: Proprietary. All rights reserved. (upstream CLI license)
- Upstream license reference: https://github.com/teamofsilicons/silicon-waveform/blob/88a76a0dd1bd39ce010b6e86c3c80ff4ebdcc9fe/cli/LICENSE
- Package SHA-256: `29c5c3fad9ac747ef6f1e2a1e0ace0a42c535a87f1cf95d722c1d4a516ec906d`
- File: waveform-LICENSE.txt

## commit (Honeycomb tos>commit 0.2.0)

- License: MIT (upstream CLI metadata)
- Upstream license reference: https://github.com/teamofsilicons/silicon-commit/blob/90531eaccafa8c96415949d28d0adec5d3ec4d25/cli/Cargo.toml
- Package SHA-256: `dbe42813a163a31be1cfe2c75cd0faf846a06e1116af45c5b0aa61b3478d5396`
- File: commit-NOTICE.txt

## remind (Honeycomb tos>remind 0.2.0)

- License: Apache-2.0 (upstream CLI license)
- Upstream license reference: https://github.com/teamofsilicons/silicon-remind/blob/4be216996a08d72de0b78ebdb1c5cddc0a1bd68a/crates/cli/LICENSE
- Package SHA-256: `0a026784d0d03a9d165bfe0cfd43783c883b775771cc1fd3d1bcd1d22e440615`
- File: remind-LICENSE.txt
- The reference CLI Cargo version is 0.1.2; the distributed Honeycomb package version is 0.2.0.

## hook (Honeycomb tos>hook 0.6.0)

- License: Apache-2.0 (upstream CLI metadata)
- Upstream license reference: https://github.com/teamofsilicons/silicon-hook/blob/d14360dc892db6bd4e09e90f530500117db215d2/crates/cli/Cargo.toml
- Package SHA-256: `b6bc254c5dc014c8ba3450fbc3f1e9f63721bce4ca43da2717d8db17089ac690`
- File: hook-NOTICE.txt
- The reference CLI Cargo version is 0.5.0; the distributed Honeycomb package version is 0.6.0.

## spacestation (Honeycomb tos>spacestation 0.1.4)

- License: MIT (upstream repository license)
- Upstream license reference: https://github.com/teamofsilicons/space-station/blob/c0f91ee8aae33462deae0adb23c1e6d1e93130f9/LICENSE
- Package SHA-256: `8fee12ad9f47e35868b30b85353ac8b64c5ac82642a5fc64a98747387aa6f4b8`
- File: spacestation-LICENSE.txt
- The reference CLI Cargo version is 0.1.2; the distributed Honeycomb package version is 0.1.4.

## Omni 0.7.2 (daemon and CLI aliases)

- Declared license: MIT
- Source: https://github.com/teamofsilicons/silicon-omni/tree/d52f5416cd33b363554d2300b5603dc0b6c43545
- File: omni-LICENSE.txt

## Caddy 2.11.4

- Declared license: Apache-2.0
- Source: https://github.com/caddyserver/caddy/tree/v2.11.4
- Files: caddy-LICENSE.txt, caddy-AUTHORS.txt
- The upstream release tree contains no NOTICE file.

## Honeycomb 0.2.3

- Declared license: MIT
- Source: https://github.com/teamofsilicons/silicon-honeycomb/tree/27f23c42d4130b4a50017dd4ea281fa94e2ec8e8
- License reference: https://github.com/teamofsilicons/silicon-honeycomb/blob/27f23c42d4130b4a50017dd4ea281fa94e2ec8e8/crates/cli/LICENSE
- Distribution: upstream GitHub release v0.2.3, verified using its per-archive SHA-256 files
- File: honeycomb-LICENSE.txt
