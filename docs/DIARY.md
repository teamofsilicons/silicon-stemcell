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


### Publishing the guide

The static guide is live at [docs.teamofsilicons.com](https://docs.teamofsilicons.com), hosted by the `silicon-docs` Vercel project. DNS verification added a TXT challenge and the `docs` CNAME; a before/after comparison confirmed that all 55 existing DNS records remained unchanged. The domain returned HTTP 200 with verified TLS, and Chrome displayed the published page. Mobile navigation, the installation copy button, and desktop layout were checked. The deployment uploads public documentation assets; template homes, Rust build output, and local environment files are excluded.

GitHub's branch checks passed on both Ubuntu and macOS. The first complete implementation commit is `3bfd07e` on `stemcell-v3.5`; the supplied `stemcell/` files are unchanged. Publication of the versioned binary assets remains a separate gate.


A final failure-path review caught provider errors that can arrive before any `START` or `END`. Terminal blocked workers now fail their receipts and retire, so an update is not postponed forever by work that cannot begin. Flow sends now wait for the delivery receipt within the action, allowing `send.catch` to handle that failure and continue before the event is acknowledged. A regression using the real Omni Rust client and a controlled Unix transport checks both the failure case and successful acknowledgment before turn completion.

Two lifecycle races were closed before release. A connection obtained just before disconnect must recheck its enabled state while holding the worker lock, preventing it from creating a daemon and capability after cleanup. Restart admission also counts the entire HTTP operation and heartbeat preparation, including shell evaluation and authentication. A real HTTP compilation test holds a Bash expression open, verifies that restart is refused, releases it, and verifies that restart becomes possible after the response. The final ordinary suite passed 24 tests, with the two separately exercised Caddy integration tests marked ignored.

### Tracing the real application logins

Briefcase, DM, Hook, Remind, Waveform, and Commit all exposed their IAM IDs and received IAM-issued short-lived tokens, then rejected the test Silicons during hosted login. Direct exchange controls made the failure precise: a test Carbon succeeded, while two test Silicons in separate owned organizations failed with `invalid_grant`. IAM's issuer deliberately creates unscoped selected-organization login grants, but its shared OAuth authority function rejected the same shape for Silicon subjects. That function also serves refresh and introspection, so changing one app or retrying a consumed token would leave the root cause intact.

The prepared upstream change is one forward SQL migration plus a regression in IAM's existing protocol suite. It preserves the exact application, consent, parent session, active Silicon, organization, and membership checks while accepting the issuer's unscoped grant. On disposable PostgreSQL 16, the regression fails against the old helper and the full protocol suite passes with the fix. Another 339 ordinary IAM tests, formatting, Clippy, migration-security, and runtime-grant checks passed. The rollback function was also exercised transactionally. Hosted deployment remains pending: IAM verifies an exact migration ledger, so the API/worker image and both production/testing schemas must move together. A local CLI URL override cannot redirect token exchanges made inside already-published app servers. Hosted success will be recorded after that coordinated rollout and the six login checks actually pass.
