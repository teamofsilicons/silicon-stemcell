
## Usage

```bash
silicon-browser open example.com         # start a 180-minute session and navigate
silicon-browser open example.com --timeout 60
silicon-browser snapshot -i              # list interactive elements with @refs
silicon-browser click @e1                # click by ref
silicon-browser fill @e2 "hello"         # fill an input
silicon-browser type "search query"      # keystroke-level typing
silicon-browser get text @e3             # extract text / html / value
silicon-browser screenshot               # saved under ~/.silicon-browser/screenshots
silicon-browser evaluate "document.title"
silicon-browser file session upload ./data.csv
silicon-browser file input @e4 /tmp/.uploads/data.csv
silicon-browser file session archive
silicon-browser tabs                     # list tabs
silicon-browser tab new example.org      # open / switch tabs
silicon-browser tab select 0
silicon-browser close                    # release the cloud browser
```

### Commands

| Command | Description |
|---|---|
| `open <url> [--timeout min]` | Navigate (starts a session if none active; default 180 min) |
| `snapshot [-i] [--json]` | Accessibility tree with `@refs`; `-i` = interactive only |
| `click @ref` | Click an element |
| `fill @ref "text"` | Fill an input |
| `type "text" [--delay ms]` | Type at the keyboard level |
| `select @ref "value"` | Select a dropdown option |
| `hover @ref` | Hover an element |
| `scroll [down\|up\|top\|bottom] [--amount px]` | Scroll the page |
| `get text\|html\|value @ref` | Extract from an element |
| `screenshot [path] [--full-page]` | Capture a screenshot |
| `evaluate "js"` | Run JavaScript, print the result |
| `file session list\|upload\|download\|archive\|delete\|clear` | Manage files in the active browser session |
| `file global list\|upload\|download\|delete` | Not supported on Browserbase |
| `file input @ref /tmp/.uploads/name` | Set a file input to one or more session files |
| `tabs` / `tab new [url]` / `tab select <n>` | Manage tabs |
| `share [--expiry min] [--view-only] [--new]` | **Remote access link** (see below) |
| `status` / `sessions` | Inspect sessions |
| `profile list\|save <name>\|delete <name>` | Persistent identities |
| `proxy current\|status` | Inspect provider-neutral network egress |
| `close` | Release the cloud browser |
| `login [--key ...]` | Enter & save your provider API key (prompts if no `--key`) |
| `install` | Verify credentials |

### Files

Uploaded session files live inside the remote Browserbase VM under
`/tmp/.uploads`. Upload a local file into the current session, then bind it to a
web page file input:

```bash
silicon-browser snapshot -i
silicon-browser file session upload ./invoice.pdf
silicon-browser file input @e7 /tmp/.uploads/invoice.pdf
```

Browserbase downloads can be listed, downloaded by filename/id, or bundled into
a local archive:

```bash
silicon-browser file session list
silicon-browser file session download report.csv ./report.csv
silicon-browser file session archive ./session-files.zip
```

Provider-global file storage is not available on Browserbase.

### Global options

`--session/-s <name>` (parallel sessions) · `--profile <name>` ·
`--incognito` · `--proxy <url>` ·
`--user-agent <ua>` · `--solve-captcha`

## Remote access links

Generate a shareable link to watch — or take control of — the live cloud browser:

```bash
silicon-browser share                      # current session link, expires in 120 min
silicon-browser share --expiry 30          # custom expiry (minutes)
silicon-browser share --view-only          # watch only, no control
silicon-browser share --new --expiry 120   # fresh blank session dedicated to the link
```

The link's lifetime is the remote session timeout, so the expiry is enforced
server-side: when it elapses, Browserbase tears the browser down and the link stops
working. The default is **120 minutes** (clamped to Browserbase's maximum if higher).

- **Interactive** (default): recipients can click and type in the live browser.
- **View-only** (`--view-only`): the Silicon Browser interface renders the
  Browserbase live view without pointer events.

Because the provider fixes a session's timeout at creation, `share` on an *existing*
session reports that session's real expiry; use `--new` to mint a fresh session
with exactly the expiry you ask for.

If there is no active local session, `share` fails instead of silently creating
an `about:blank` link. Open a page first, or pass `--new` when you explicitly
want a fresh blank shared browser.

Share links are pinned to the active leased CDP target. Cross-origin navigation
cannot detach the worker from that tab, and popups inherit ownership only when
their opener belongs to the same local session.

## Glass session archives

On a Glass-managed Silicon, Glass registers the canonical Browserbase session
when it creates it. Closing the final tab lease, timing out, or rolling over the
session queues an asynchronous archive: Glass copies every recorded tab and HLS
segment, provider metadata, browser logs, and downloaded files into private S3
storage. Team operators can watch live sessions and replay completed sessions
from the team's Browser page in Glass. Standalone installations continue to use
Browserbase directly.

## Profiles

A profile maps a local name to a Browserbase Context, so cookies, storage, and
login state persist across sessions:

```bash
silicon-browser --profile work open github.com   # reuse saved context
silicon-browser --profile work close             # context is saved on close
silicon-browser profile list
```

On Glass-managed silicons, the assigned Browserbase profile is used
automatically, including when `--profile` is omitted. A CLI profile name is
only needed for standalone installs or to select a locally named profile.

Glass is authoritative for managed contexts. If Glass or Browserbase cannot
verify the canonical session, Silicon Browser fails closed instead of creating
a second context writer. Tabs belonging to other Silicons are excluded from
listing, selection, snapshots, refs, and close operations.

Silicon Browser also detects common login walls (login URLs and password forms),
records the authentication state in Glass, and surfaces the live-view URL for
human reauthentication. No browser provider can guarantee that a site will
never revoke a session or apply bot/risk controls; this design keeps identity
stable and makes recovery explicit instead of attempting to bypass those controls.

## Network egress

Glass supports three provider-neutral modes for newly created canonical
sessions: direct provider networking, Browserbase-managed egress, or one
standard external proxy URL stored in Glass's encrypted provider-key store.
The mode is fixed for the provider session and never silently changes during a
tunnel failure.

```bash
silicon-browser proxy status   # Glass mode, region, context, and session usage
silicon-browser proxy current  # current session's egress source
```

Standalone sessions may still use an explicit `--proxy <url>`. Managed contexts
ignore per-command proxy overrides because Glass must keep one stable network
identity for every participant sharing the canonical session.
