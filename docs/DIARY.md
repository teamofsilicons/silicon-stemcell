# Building Silicon 3.5

9 September 2026 · implementation diary


The 3.5 work establishes one Rust interpreter and a shared control surface for terminal and web operations. Configuration paths identify local connections; Silicon IDs identify their routed local hosts. Real Omni processes replace file-based send placeholders, and inference events drive delivery receipts, progress logs, and session lifecycle.

The parser preserves the reference dialect's ordered flow while rejecting ambiguous ordinary duplicate keys. Compatibility warnings make legacy addressing/retention fields visible. Expression validation covers unselected branches and fallback candidates before compile-time Bash can run. Login and webhook entries use a distinct deferred-command evaluation mode so configuration such as `! dm` retains its intended app-command meaning.

Session work separates logical archive names from immutable Omni UUIDs. Persistent sessions retain metadata, conversation data, and history. Ephemeral sessions route a completed result to an internal caller and remove their recoverable data. Rollover creates a fresh persistent successor, preserves the predecessor's identity/timestamps, and retains the live address for session-mode work. Delivery and retirement tests cover callbacks, late failures, retries, and messages arriving around the end of a turn.

Scheduling evaluates DNA and heartbeat intervals at their documented boundaries. DNA assembly was moved off the provider event listener so refresh scripts do not block inference receipts. Suggestion counters exclude control messages and operate independently per session. Restart admission is coordinated with active dispatch and nested runtime work.

Authentication uses the installed IAM CLI's Silicon login contract, an isolated IAM state home, app discovery/status checks, and application-owned sessions. Dynamic app authentication is remembered for later session/heartbeat checks. Credential-bearing subprocess output is kept out of normal logs. The full authentication contract has controlled tests; live multi-app integration remains a separate verification task.

Caddy is owned by the interpreter and controlled through a private Unix socket. Route updates have rollback behavior and do not reconfigure an unrelated Caddy daemon. The installer builds or downloads complete bundles, verifies checksums, checks every required member, and changes the selected release only after preparation. Managed wrappers prevent dependency CLIs from independently updating the bundle.

Verification combines Rust tests, real Caddy integration, a real Omni protocol E2E with a scripted provider, and a separate actual Claude inference smoke test. The latter returned `SILICON_SMOKE_OK`. Release publication and documentation hosting should be recorded here only after they are complete and checked. No deployment claim in this diary is a substitute for that final evidence.


### What the final integration runs caught

The real IAM 1.4.1 executable requires an explicit organization grant when minting an app token. Selecting `--org` only chooses context; it does not grant app access. The interpreter now passes `--grant-org` for the Silicon's own organization, and the contract check asserts it.

Restart testing exposed a saved-session edge case: `end` initially found only loaded workers. It now archives persistent sessions directly from disk without starting a provider, accepts the logical ID or Omni UUID, and serializes loaded-session ending with message acceptance. The E2E creates a session, restarts, and ends that unloaded session by UUID.

The updater was exercised against complete local release bundles with the real interpreter and Omni. Two test seams replaced GitHub metadata HTTPS with a loopback fixture and shortened the one-hour interval; those seams exist only in the test source copy. Equal/older versions, drafts, prereleases, bad checksums, and unmanaged builds were rejected appropriately. Successful activation preserved YAML and connections. During active work, version 3.5.0 kept serving after the new bundle was installed; only after provider completion did it execute 3.5.1 with the same PID and port, restore the session UUID, and resume work. This found and fixed ordinary shutdown incorrectly marking completed work as an error. GitHub's live transport and the literal hour-long wait are outside that accelerated check.

Browser verification covered configuration validation and connection, sending and injecting into a running turn, displaying provider completion, persistent rollover, and archive filtering. Mobile checks caught a connection-table overflow and a navigation handler that returned `false` while closing the menu, cancelling its link. The table now stacks at narrow widths and mobile documentation links navigate normally. The dashboard also retains the initial URL-fragment token for its browser session.

The complete source install and packaged install were tested in isolated prefixes. All 16 payload executables/wrappers were present, pinned Omni and Caddy versions matched, six application discovery commands returned IDs, and the installed bundle passed the protocol E2E. Package extraction and relocation preserved payload bytes, notices, wrapper behavior, and installation metadata. This is separate from live app authentication and from the four-platform release build.
