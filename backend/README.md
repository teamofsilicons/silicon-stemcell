# Silicon Realtime

The read-only relay is live at **https://realtime.teamofsilicons.com**, registered in Honeycomb/IAM as `tos>silicon-realtime`. Its protocol is `v1`; `GET /health` reports readiness. Interpreter installation and user/API instructions are in [the main guide](../docs/GUIDE.md).

```sh
cargo test --locked --manifest-path backend/Cargo.toml
cargo clippy --locked --manifest-path backend/Cargo.toml --all-targets -- -D warnings
cargo zigbuild --locked --manifest-path backend/Cargo.toml --release --target aarch64-unknown-linux-gnu
python3 backend/deploy/release.py
```

`deploy/aws.json` manages a dedicated HTTPS host rule, target group, narrowly scoped secret/artifact access and port 1830 access from the load balancer. The production account's 32-vCPU quota prevented a new EC2 host, so the relay runs as a separate `silicon-realtime` systemd user/service on the existing Space Station API host. It has a 384 MiB memory limit, CPU weight 10 and 128-task limit. Deployment does not change Space Station's unit or configuration. The artifact bucket and Secrets Manager record are separate, retained resources referenced by stack parameters.

The deploy script uploads a content-addressed native ARM64 release to the private bucket, verifies its SHA-256 on the host, writes a root-only runtime environment from Secrets Manager, atomically selects the new release and checks local health. A failed health check restores the previous executable selection. `release.py` prints the SSM command ID so uncertain deployments can be inspected before retrying. The previous release directories remain available for rollback.

Runtime environment:

- `SILICON_REALTIME_APP_SECRET` and `SILICON_REALTIME_WEBHOOK_SECRET`: confidential IAM registration credentials; never ship them in the interpreter or commit them.
- `SILICON_IAM_URL`: defaults to `https://backend.iam.teamofsilicons.com`.
- `SILICON_REALTIME_BIND`: defaults to `127.0.0.1:1830`; production listens on its private interface through the ALB security group.
- `SILICON_BACKEND_TABLE_KEY`: optional Space Station ingestion key. Interpreter telemetry opt-out is carried on publisher messages and suppresses their backend telemetry too.
- `SILICON_HOME`: writable private service state, `/var/lib/silicon-realtime` in production.

One native interpreter connection multiplexes at most 256 Silicon identities. Each registration is introspected as that exact Silicon and organization. Subscribers receive only their IAM-selected organization's data; OBO verification binds the exact request bytes and consumes a proof once. Direct subscriptions are revalidated every 15 seconds. OBO subscriptions expire at the earlier of IAM's proof expiry or 60 seconds and must obtain another proof. Tickets are single-use, expire after 30 seconds, and never appear in URLs. Unexpected operations fail closed.

Snapshots and events are in memory only. No replay/history database exists; a slow subscriber receives `gap` and can fetch a new snapshot. A disconnected publisher loses its snapshot and broadcasts offline presence. A missing heartbeat becomes offline within 45 seconds plus current IAM-request time. The interpreter's generation-bound log queue discards entries from replaced connections. Known credentials are redacted before publication and credential field names are redacted again at the relay.

Testing contexts use the same endpoints with `testing_context: {app_id, app_secret, iam_test_key}` in the HTTPS JSON body or publisher registration. IAM authenticates the test credentials; the returned testing-environment UUID isolates all snapshots and subscriptions. A supplied secret never grants end-user authority on its own. No test request falls back to production.

`cargo test` runs an actual HTTP/WebSocket integration test with controlled IAM responses. A separately reproducible live test uses Node's built-in WebSocket and real IAM credentials:

```sh
node backend/tests/live.mjs /absolute/path/to/private-fixture.json
```

The fixture schema is documented at the top of that file. It is intentionally not committed. On 2026-09-16 this test passed against production IAM and the public WSS relay, then against an isolated hosted IAM environment with real delegated OBO configuration and subscription proofs. It checks identity/org denial, configuration redaction, single-use tickets and proofs, event delivery, ping/pong, read-only rejection and offline presence.

Honeycomb registration publishes the IAM/OBO identity; this repository distributes the Unix interpreter through GitHub releases. It does not claim an installable six-platform Honeycomb package or Windows interpreter support.
