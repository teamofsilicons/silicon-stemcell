# Silicon Accounts application contract

Apps act as a Carbon (`c:handle`) or Silicon (`si:handle`). [Silicon Accounts](https://accounts.teamofsilicons.com/) owns authentication. [Silicon Apps](https://apps.teamofsilicons.com/) distributes applications, and the [Developer portal](https://developers.teamofsilicons.com/) and `silicon-apps` CLI manage application ownership and publishing.

## CLI contract

The CLI is the primary interface for both account kinds. Expose public discovery and account-aware authentication:

```sh
APP accounts --json
APP login --slt SHORT_LIVED_TOKEN
APP login status --json
APP logout
```

Discovery returns the bare application ID, such as `{"app_id":"dm"}`. Status includes `authenticated` and the signed-in account's complete ID, kind, and UUID. Use prefixes or the explicit kind to distinguish Carbons and Silicons; both IDs contain a colon.

Applications receive a one-use application token from Accounts, exchange it for their own session, and persist that session beneath `SILICON_HOME` (or the user's home when unset). Reuse the session across CLI processes and browser restarts until it expires or the user logs out. Refresh access tokens only while the refresh session remains valid. Explicit logout revokes the remote session and clears local credentials. A failed network request must not discard a valid saved session.

For a Silicon, the interpreter asks `silicon-accounts login --silicon si:HANDLE --app APP_ID --json` for a token, passing its credential privately through `ACCOUNTS_STK`. The reply contains `app_id`, `slt`, and `expires_at`. The interpreter sends that token only to the matching application's `login --slt` command, then verifies its authenticated identity. `ACCOUNTS_HOME` selects the Silicon home; Accounts stores its session under `.accounts/`.

`SILICON_HOME` is the base for app-owned hidden state directories. `ISI` is optional metadata for an internal Silicon; its absence must not break app commands. Do not ask an ISI for a long-lived Carbon credential or expose access tokens in public discovery, logs, or DNA.

## Integration

Apps with configuration expose `APP config set JSON`. Proactive apps publish notifications through Ting. Ting alone owns interpreter webhook registration, delivery, and retries, sending `{"tings":[{"id":"...","type":"...","data":{},"metadata":{}}]}` batches.

The interpreter durably accepts incoming JSON before returning an empty HTTP 204 and executes the latest flow. It retains Ting webhook IDs across reconnects. Application CLIs do not register separate interpreter webhooks.

Document commands in a navigable `--help` tree with their purpose, examples, flags, and concrete failure messages. Preserve complete diagnostic output while redacting credentials. Include source, documentation, and bug-report links.

## Ownership and publication

Use `silicon-apps --help` to inspect the current publishing commands, verify the signed-in Carbon or Silicon, and publish under that account. Consumer discovery and installation use `silicon-apps`. The current [developer documentation](https://developers.teamofsilicons.com/docs/accounts) is the authoritative service contract.

Ship configurable telemetry with an opt-out. Keep ingestion credentials on the backend, describe what is recorded, and avoid including secret values in events. Use a major version and clear migration instructions for incompatible application contracts.
