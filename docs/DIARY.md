# Implementation diary

### 7.0.6 — Mid-turn Claude receipts and non-blocking delivery

The complete runtime bundles now include Omni 0.10.4. Claude starts a turn with a queued message and echoes it exactly, but a message it takes mid-turn is replayed from its rewritten queued command without the final newline that YAML messages end with. Omni matched exactly, so those messages never received INJECTED; the unmatched message stayed at the head of Omni's FIFO, and no later echo in that Claude session matched either. A busy runner therefore acknowledged nothing, Omni kept every delivered message pending for resend after failover, and each flow delivery timed out after 60 seconds. Omni now accepts an echo equal to the message or the message with surrounding whitespace trimmed, confirms the original text, and treats an echo of a later message as proof that earlier ones were read, marking those `inferred`.

The outbox no longer waits for START or INJECTED once Omni answers `accepted: true`. That answer means the message is in Omni's durable queue, and waiting held the Silicon's single delivery gate, so every Ting behind a long turn cost a full minute. Only a send whose acceptance answer was lost still waits for its receipt; receipts still attribute replies.

No configuration migration is required from 7.0.5. Sessions already stuck keep their Claude process until it restarts; their old pending Omni messages and uncertain outbox journals were delivered and need reconciliation rather than replay.

### 7.0.5 — Batched Claude receipts and quiet recovery checks

The complete runtime bundles now include Omni 0.10.3. Claude can acknowledge several queued inputs in one replay event; Omni now matches every text block against the pending FIFO prefix and records a separate durable receipt for each input. Exact whitespace, duplicate occurrence counts, and all-or-nothing validation are preserved, so an unknown block cannot acknowledge unrelated work.

Receipt-only outbox checks no longer increment send attempts or repeat an unchanged error on every trigger. New or changed failures are still logged, and uncertain deliveries remain protected against automatic resend.

No configuration migration is required from 7.0.4. Existing uncertain deliveries still need reconciliation before deliberate replay.

### 7.0.4 — Install apps with an existing sign-in

`si app install APP` now checks and reuses a current session belonging to the configured Silicon before adding the app to the ISI. Previously it unconditionally requested a fresh login, so apps such as Write rejected the request with “already signed in” and never reached the apps-list update. Signed-out apps still authenticate; foreign or unidentified sessions cannot be accepted as this Silicon. Explicit `si auth setup` retains its fresh-login behavior.

No configuration migration is required from 7.0.3. Restart the interpreter after updating and rerun the failed install.

### 7.0.3 — Outbox recovery and readable local hostnames

The runtime recovers missed delivery acknowledgements from Omni's durable history, matching event sequences as well as message text. An uncertain delivery is retained for receipt checks or manual reconciliation after a bounded wait, while newer messages proceed. Recognized legacy timeout records receive the same protection against automatic resend. Duplicate messages, delayed confirmations, and older END events cannot consume or complete newer work; delayed ephemeral replies retain their own turn's output.

Silicon handles with inner underscores now use readable local hostnames such as `assistant_one.si.localhost`. Their previous encoded hostnames remain aliases across reconnects and interpreter restarts, preserving existing webhook callers. Both names route to the same account, while hyphenated and underscored handles remain distinct.

No configuration migration is required from 7.0.2. Omni remains at 0.10.2. Uncertain deliveries must still be reconciled before deliberate replay.

### 7.0.2 — Error traces and Omni delivery acknowledgements

Failures show the original diagnostic first, followed by the source command or component, exit status, known project link, a supported explanation when available, and caller context. Both command streams remain visible with credentials masked. CLI, connection progress, HTTP responses, flow catches, retained delivery errors, and background logs preserve the trace. Cleanup failures remain alongside the primary error, and retry classifications survive formatting. Recovered app updates show the full diagnostic while continuing with a usable installed copy.

The complete runtime bundles now include Omni 0.10.2. Claude replay acknowledgements preserve original message whitespace, including the final newline from YAML, so accepted messages no longer leave later deliveries queued. Existing completed-but-unacknowledged deliveries need reconciliation before replay to avoid duplicate work. No configuration migration is required from 7.0.1.

### 7.0.1 — Telemetry removed

Silicon no longer sends telemetry from the interpreter, CLI, runtime, dashboard, or documentation website. Space Station clients, delivery queues, ingestion credentials, and telemetry controls have been removed. Existing `silicon.space_station` configuration is ignored without evaluating its expressions. Local logs, error redaction, and session history remain available.

The former TOS telemetry tables (`siliconinterpreter`, `siliconruntime`, and `silicondocs`) are retired, and their old ingestion keys are rejected. Historical records are preserved. Older binaries can still attempt telemetry requests until updated, but those tables no longer accept them.

### 7.0 — Carbon and Silicon accounts

The interpreter now integrates with Silicon Accounts and Silicon Apps. Application identity and ownership use Carbon or Silicon accounts, with bare catalog app IDs. The Developer portal and CLI are the publishing interface.

Configuration removes organization fields and retired authentication environments. CLI app sessions are reused until expiry or explicit logout; logged-out apps stay logged out until explicit authentication. The local dashboard stores access across browser and interpreter restarts and offers a logout action that revokes its token.

The dashboard and documentation use the Silicon UI foundation distributed by [ui.teamofsilicons.com](https://ui.teamofsilicons.com/r/silicon-foundation.json). The vendored CSS keeps local controls usable offline. Documentation and examples describe the current account and app contract. Detailed records of earlier releases remain available in Git history and [GitHub Releases](https://github.com/teamofsilicons/silicon-stemcell/releases).
