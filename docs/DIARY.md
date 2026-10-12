# Implementation diary

### 7.0.1 — Telemetry removed

Silicon no longer sends telemetry from the interpreter, CLI, runtime, dashboard, or documentation website. Space Station clients, delivery queues, ingestion credentials, and telemetry controls have been removed. Existing `silicon.space_station` configuration is ignored without evaluating its expressions. Local logs, error redaction, and session history remain available.

The former TOS telemetry tables (`siliconinterpreter`, `siliconruntime`, and `silicondocs`) are retired, and their old ingestion keys are rejected. Historical records are preserved. Older binaries can still attempt telemetry requests until updated, but those tables no longer accept them.

### 7.0 — Carbon and Silicon accounts

The interpreter now integrates with Silicon Accounts and Silicon Apps. Application identity and ownership use Carbon or Silicon accounts, with bare catalog app IDs. The Developer portal and CLI are the publishing interface.

Configuration removes organization fields and retired authentication environments. CLI app sessions are reused until expiry or explicit logout; logged-out apps stay logged out until explicit authentication. The local dashboard stores access across browser and interpreter restarts and offers a logout action that revokes its token.

The dashboard and documentation use the Silicon UI foundation distributed by [ui.teamofsilicons.com](https://ui.teamofsilicons.com/r/silicon-foundation.json). The vendored CSS keeps local controls usable offline. Documentation and examples describe the current account and app contract. Detailed records of earlier releases remain available in Git history and [GitHub Releases](https://github.com/teamofsilicons/silicon-stemcell/releases).
