# Browser automation with `silicon-browser`

`silicon-browser` controls a remote Browserbase session through Playwright.
Your session and Glass-managed profile are already assigned. Run commands
directly; do not add `--session` or `--profile`.

## Required workflow

1. Open or navigate to the page.
2. Run `snapshot -i` to get current interactive `@refs`.
3. Interact through those refs.
4. Take a new snapshot after navigation or a meaningful DOM change; old refs
   may no longer be valid.
5. Extract or verify the result.
6. Run `silicon-browser close` when the task is finished. This releases the
   remote browser and preserves the managed profile.

```bash
silicon-browser open https://example.com
silicon-browser snapshot -i
silicon-browser fill @e1 "hello@example.com"
silicon-browser click @e2
silicon-browser snapshot -i
silicon-browser get text @e3
silicon-browser close
```

Use `silicon-browser --help` or `<command> --help` when a command is unclear.
Do not guess commands from an older browser tool.

## Current command surface

```bash
# Navigation and inspection
silicon-browser open <url> [--timeout <minutes>]
silicon-browser snapshot [-i] [--json]
silicon-browser get text|html|value @ref
silicon-browser screenshot [path] [--full-page]
silicon-browser evaluate "<javascript>"

# Interaction
silicon-browser click @ref
silicon-browser fill @ref "text"
silicon-browser type "text" [--delay <milliseconds>]
silicon-browser select @ref "value"
silicon-browser hover @ref
silicon-browser scroll down|up|top|bottom [--amount <pixels>]

# Tabs
silicon-browser tabs
silicon-browser tab new [url]
silicon-browser tab select <index>

# Session and human handoff
silicon-browser status
silicon-browser sessions
silicon-browser share [--expiry <minutes>] [--view-only] [--new]
silicon-browser close
```

`type` sends keyboard input to the currently focused element. Prefer `fill
@ref` for ordinary form fields.

## Files and uploads

Browserbase session files live under `/tmp/.uploads` in the remote browser VM.
Upload the local file into the session first, then bind it to a page file input:

```bash
silicon-browser snapshot -i
silicon-browser file session upload ./invoice.pdf
silicon-browser file input @e7 /tmp/.uploads/invoice.pdf
```

Manage downloads and session files with:

```bash
silicon-browser file session list
silicon-browser file session download report.csv ./report.csv
silicon-browser file session archive ./session-files.zip
silicon-browser file session delete <name-or-id>
silicon-browser file session clear
```

Provider-global file storage is not available on Browserbase; use
`file session`, not `file global`.

## Login and human intervention

Never ask for or copy a Carbon's password. If the site requires the Carbon to
authenticate, report that clearly to the manager. The manager can create a
controlled remote-browser handoff. A direct worker share link can be created
with:

```bash
silicon-browser share --expiry 30
```

Use `--view-only` when observation is enough. Use `--new` only when a separate
blank session is explicitly wanted.

Glass-managed profiles and sessions are authoritative. Do not work around a
profile/session ownership error by starting a competing session.

## Reliability rules

- Prefer refs from `snapshot -i`; re-snapshot instead of reusing stale refs.
- Verify consequential actions before and after clicking.
- Do not submit purchases, publish content, delete data, or send messages
  unless the manager's task authorizes that action.
- Use `evaluate`, not the retired `eval` spelling.
- Do not use retired local-browser commands such as `wait`, `find`, `state`,
  `network`, `cookies`, `pdf`, `device`, `push`, `pull`, or `clone`.
- If the CLI reports an unsupported operation, inspect `--help` and report a
  real blocker rather than inventing a workaround.
- Always close the browser in normal completion and best-effort cleanup paths.
