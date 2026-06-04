# Prod E2E Report

Date: 2026-06-04

Stemcell branch: `main`

Prod server: `silicon-chat`

Prod base URL: `https://glass.teamofsilicons.com`

Newest PEM found: `/Users/codanium/Downloads/silicon-web-chat.pem`

## Release State

- `main` was updated from `v1` as one release commit: `cae52c3 Release v1.0`.
- Git tag `v1.0` was pushed.
- GitHub Release `v1.0` was created.
- A follow-up stemcell fix was needed after prod testing: backup upload now uses the prod contract.

## Prod Fixes Applied Live

- Docker web healthcheck was fixed on the server.
  - Before: Docker showed `silicon-chat-web-1` as `unhealthy`.
  - Cause: healthcheck called `http://localhost:8000/healthz`; Django redirected to HTTPS; Python followed that redirect against the plain app port and failed with `SSL: WRONG_VERSION_NUMBER`.
  - Fix: healthcheck now sends `X-Forwarded-Proto: https`.
  - Result: `silicon-chat-web-1` is now `healthy`.

- Backend silicon-management authorization was fixed on the server.
  - Before: any authenticated carbon could create a silicon in any team.
  - Before: any authenticated carbon could mint an API key for any silicon.
  - Fix: silicon creation and key list/mint/revoke now require team head/admin access.
  - Result: unauthorized create/key-mint now returns `403`.

- Stemcell backup upload contract was fixed locally in this repo.
  - Before: stemcell sent `Authorization: Bearer <key>` and multipart field `archive`.
  - Prod expects: `X-Silicon-Key: <key>` and multipart field `file`.
  - Result: prod backup upload passes with the corrected contract.

## Full Flow Verified In Prod

Using disposable `codex-e2e-*` records, then cleanup:

- public health and readiness
- team creation
- founding team membership
- silicon creation
- silicon API-key minting
- silicon API-key authentication
- unauthorized silicon create blocked
- unauthorized API-key mint blocked
- carbon to silicon direct room and message
- silicon sees carbon message with `display_time`
- silicon to carbon reply
- silicon to silicon direct room and messaging
- read receipts
- progress `thinking` plus persisted `done`
- remote browser event with expiry
- take-back read gate and force override
- cron create/list/update/delete
- media upload-url presign
- backup upload
- Glass websocket connect, handshake, ping/pong
- Glass backup command delivery over websocket

The rerun after backend fixes ended with:

```text
SUMMARY websocket_failures=0
```

and the main API flow had no remaining failures before the websocket test script cleanup.

## Remaining Risks / Improvements

- The live backend server has many uncommitted/unpushed changes, and the local `glass` clone’s GitHub `origin/main` is behind/diverged from the server’s `origin/main`. Do not blindly pull/reset/deploy that repo until the live server history is reconciled.
- `backend.teamofsilicons.com` is stale and has no DNS. The working prod host is `glass.teamofsilicons.com`.
- `/api/v1/version` returns `{"version":"prod","commit":"local"}`. Build provenance should be wired to the deployed Git commit.
- The prod e2e tested backend/Glass API behavior directly. It did not run a real deployed stemcell manager loop through a live `si` CLI process.
- TTS/STT provider chains were not exercised in prod during this pass. Media presign passed; voice provider execution should be checked separately to avoid mixing provider-cost failures with core messaging.
