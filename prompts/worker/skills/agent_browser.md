# Browser Automation with silicon-browser

Your primary tool for browser automation. All `silicon-browser` commands are executed via Bash.

Your browser session is pre-configured via environment variables. Do NOT pass `--session` or `--profile` flags — they are already set for you. Just use `silicon-browser` commands directly.

Current v1 browser is Steel-backed. Profile state persists when you close the session. Always close before reporting done.

Remote browser shares are usually created by the manager tool, not by workers:

```bash
silicon-browser share --new --expiry 60
silicon-browser close
```

Use share only if the manager explicitly asks you to produce a share URL. Close after the carbon says login is done so cookies/storage are saved.


# silicon-browser

Stealth-first headless browser CLI for AI agents. Uses CloakBrowser's patched Chromium (33 C++ source-level patches) as its engine, combined with 18 JavaScript-level stealth evasions. Passes Cloudflare, DataDome, PerimeterX, and every major anti-bot system out of the box. Zero configuration needed for stealth.

All commands are executed via Bash. The browser persists between commands via a background daemon.

## Core Workflow

Every browser automation follows this loop:

1. **Navigate** to a URL
2. **Snapshot** to discover interactive elements (refs like `@e1`, `@e2`)
3. **Interact** using refs (click, fill, select)
4. **Re-snapshot** after any page change to get fresh refs

```bash
silicon-browser open https://example.com/form
silicon-browser snapshot -i
# Output: @e1 [input type="email"], @e2 [input type="password"], @e3 [button] "Submit"

silicon-browser fill @e1 "user@example.com"
silicon-browser fill @e2 "password123"
silicon-browser click @e3
silicon-browser wait --load networkidle
silicon-browser snapshot -i    # re-snapshot after navigation
```

Chain commands with `&&` when you do not need intermediate output:

```bash
silicon-browser open https://example.com && silicon-browser wait --load networkidle && silicon-browser snapshot -i
```

Run commands separately when you need to read output before proceeding (e.g., snapshot to discover refs, then interact).

## Stealth

Stealth is on by default. No flags, no config. Three layers:

1. **CloakBrowser binary** (primary) -- 33 Chromium C++ source patches. TLS fingerprints (JA3/JA4), canvas, WebGL, audio fingerprints, CDP input signals all match real Chrome at the binary level. `navigator.webdriver` removed in C++.
2. **JavaScript evasions** (defense-in-depth) -- 22 JS patches injected before any page code runs: Function.prototype.toString protection, platform-aware WebGL (Apple M1 on macOS, RTX 3060 on Windows), SpeechSynthesis voice mocking, CSS media query normalization, stack trace sanitization, Client Hints metadata, and more.
3. **Offscreen headed mode** -- instead of `--headless=new` (detectable), runs a real headed Chrome window at off-screen coordinates. Passes ALL headless detection because it IS a real browser. On headless Linux, auto-installs and starts Xvfb.

Bypasses Cloudflare, Akamai, PerimeterX, DataDome, and Newegg's custom detection out of the box. Scored 95% on BU Bench V1 (previous SOTA: 78%).

Disable stealth for debugging: `SILICON_BROWSER_NO_STEALTH=1`
Force traditional headless: `SILICON_BROWSER_HEADLESS_REAL=1`

## CAPTCHA Solving

Auto-detect and solve CAPTCHAs on the current page:

```bash
silicon-browser solve-captcha
```

Supported types:
- **Cloudflare Turnstile** -- detects "Just a moment..." pages, clicks checkbox via CDP
- **reCAPTCHA v2 checkbox** -- human-like mouse movement (based on real recorded data) to click "I'm not a robot"
- **Text CAPTCHAs** -- local OCR engine (classical CV, no external APIs)
- **hCaptcha** -- checkbox click with behavioral simulation

All solving is local. No external APIs, no LLM calls.

## Profiles

Every profile persists cookies, storage, and a **pinned fingerprint seed** -- the same profile always looks like the same person to websites.

```bash
# Default profile "silicon" is always there
silicon-browser open https://example.com

# Named profiles for different identities
silicon-browser --profile work open https://example.com
silicon-browser --profile personal open https://github.com

# Incognito -- temp dir, random fingerprint, no traces
silicon-browser --incognito open https://example.com
```

Profiles stored at `~/.silicon-browser/profiles/<name>/`. Each has a `fingerprint.seed` file generated once and reused forever.

```bash
# List all profiles
silicon-browser profile list

# Export a profile as encrypted .silicon file
silicon-browser profile pack <name>

# Import a profile
silicon-browser profile unpack <file>
```

## Push / Clone / Pull (Profile Sync)

Sync profiles between machines over HTTP with a 6-digit OTP. No SSH keys, no third-party services.

```bash
# Machine A: serve a profile
silicon-browser push shopify
# Output:
#   Local:  http://192.168.1.5:7291
#   Public: https://abc123.lhr.life    (auto-tunnel via localhost.run)
#   OTP:    483921

# Machine B: clone the profile
silicon-browser clone https://abc123.lhr.life
# Prompts for the 6-digit OTP, downloads and decrypts profile

# Future syncs: pull by name (remembers the URL)
silicon-browser pull shopify
```

Profile is AES-256-GCM encrypted with the OTP as the key. Server auto-shuts down after one successful transfer. Auto-tunnel gives a public HTTPS URL (disable with `SILICON_BROWSER_NO_TUNNEL=1`).

## Command Reference

### Navigation

```bash
silicon-browser open <url>              # Navigate (aliases: goto, navigate)
silicon-browser back                    # Go back
silicon-browser forward                 # Go forward
silicon-browser reload                  # Reload page
silicon-browser close                   # Close browser (aliases: quit, exit)
silicon-browser connect 9222            # Connect to browser via CDP port
```

### Snapshot

```bash
silicon-browser snapshot                # Full accessibility tree
silicon-browser snapshot -i             # Interactive elements only (recommended)
silicon-browser snapshot -i -C          # Include cursor-interactive (divs with onclick, cursor:pointer)
silicon-browser snapshot -c             # Compact output
silicon-browser snapshot -d 3           # Limit depth
silicon-browser snapshot -s "#selector" # Scope to CSS selector
silicon-browser snapshot -i --json      # JSON output
```

### Interaction (use @refs from snapshot)

```bash
silicon-browser click @e1               # Click
silicon-browser click @e1 --new-tab     # Click, open in new tab
silicon-browser dblclick @e1            # Double-click
silicon-browser fill @e2 "text"         # Clear field and type
silicon-browser type @e2 "text"         # Type without clearing
silicon-browser select @e1 "option"     # Select dropdown
silicon-browser select @e1 "a" "b"      # Select multiple
silicon-browser check @e1               # Check checkbox
silicon-browser uncheck @e1             # Uncheck checkbox
silicon-browser press Enter             # Press key
silicon-browser press Control+a         # Key combo
silicon-browser keyboard type "text"    # Type at current focus (no ref needed)
silicon-browser keyboard inserttext "t" # Insert without key events
silicon-browser hover @e1               # Hover
silicon-browser focus @e1               # Focus
silicon-browser scroll down 500         # Scroll page
silicon-browser scroll down 500 --selector "div.content"  # Scroll within container
silicon-browser scrollintoview @e1      # Scroll element into view
silicon-browser drag @e1 @e2            # Drag and drop
```

### File Upload

```bash
silicon-browser upload @e1 ./file.pdf                    # Upload to visible file input
silicon-browser upload 'input[type="file"]' ./image.png  # Hidden file input (common)
silicon-browser upload 'input[type="file"]' ./a.png ./b.png  # Multiple files
```

Many sites use hidden `<input type="file">` elements that do not appear in `snapshot -i`. Target them with a CSS selector.

### Get Information

```bash
silicon-browser get text @e1            # Element text
silicon-browser get html @e1            # innerHTML
silicon-browser get value @e1           # Input value
silicon-browser get attr @e1 href       # Attribute
silicon-browser get title               # Page title
silicon-browser get url                 # Current URL
silicon-browser get cdp-url             # CDP WebSocket URL
silicon-browser get count ".item"       # Count matching elements
silicon-browser get box @e1             # Bounding box
silicon-browser get styles @e1          # Computed styles
```

### Check State

```bash
silicon-browser is visible @e1
silicon-browser is enabled @e1
silicon-browser is checked @e1
```

### Wait

```bash
silicon-browser wait @e1                # Wait for element
silicon-browser wait --load networkidle # Wait for network idle
silicon-browser wait --url "**/page"    # Wait for URL pattern
silicon-browser wait --text "Welcome"   # Wait for text (substring)
silicon-browser wait --fn "window.ready" # Wait for JS condition
silicon-browser wait --fn "!document.body.innerText.includes('Loading...')"  # Wait for text to disappear
silicon-browser wait "#spinner" --state hidden  # Wait for element to disappear
silicon-browser wait --download ./out.zip       # Wait for download
silicon-browser wait 2000               # Wait milliseconds (last resort)
```

### Downloads

```bash
silicon-browser download @e1 ./file.pdf          # Click to trigger download
silicon-browser wait --download ./output.zip     # Wait for any download
silicon-browser --download-path ./dl open <url>  # Set default download dir
```

### Screenshots and PDF

```bash
silicon-browser screenshot              # Save to temp dir
silicon-browser screenshot path.png     # Save to specific path
silicon-browser screenshot --full       # Full page
silicon-browser screenshot --annotate   # Numbered labels on interactive elements
silicon-browser screenshot --screenshot-dir ./shots
silicon-browser screenshot --screenshot-format jpeg --screenshot-quality 80
silicon-browser pdf output.pdf          # Save as PDF
```

### Annotated Screenshots (Vision Mode)

`--annotate` overlays numbered labels on interactive elements. Each `[N]` maps to `@eN`. Caches refs, so you can interact immediately without a separate snapshot.

```bash
silicon-browser screenshot --annotate
# [1] @e1 button "Submit"
# [2] @e2 link "Home"
silicon-browser click @e2
```

Use when: unlabeled icon buttons, visual layout verification, canvas/chart elements, spatial reasoning needed.

### Viewport and Device Emulation

```bash
silicon-browser set viewport 1920 1080          # Set viewport (default: 1280x720)
silicon-browser set viewport 1920 1080 2        # 2x retina
silicon-browser set device "iPhone 14"          # Emulate device (viewport + user agent)
```

### Tabs and Windows

```bash
silicon-browser tab                     # List tabs
silicon-browser tab new [url]           # New tab
silicon-browser tab 2                   # Switch to tab by index
silicon-browser tab close               # Close current tab
silicon-browser window new              # New window
```

### Frames

```bash
silicon-browser frame "#iframe"         # Switch to iframe
silicon-browser frame main              # Back to main frame
```

### Dialogs

```bash
silicon-browser dialog accept [text]    # Accept dialog
silicon-browser dialog dismiss          # Dismiss dialog
```

### Cookies and Storage

```bash
silicon-browser cookies                     # Get all cookies
silicon-browser cookies set name value      # Set cookie
silicon-browser cookies clear               # Clear cookies
silicon-browser storage local               # Get all localStorage
silicon-browser storage local key           # Get specific key
silicon-browser storage local set k v       # Set value
silicon-browser storage local clear         # Clear all
```

### Clipboard

```bash
silicon-browser clipboard read              # Read text
silicon-browser clipboard write "text"      # Write text
silicon-browser clipboard copy              # Copy selection (Ctrl+C)
silicon-browser clipboard paste             # Paste (Ctrl+V)
```

### Network

```bash
silicon-browser network route <url>              # Intercept requests
silicon-browser network route <url> --abort      # Block requests
silicon-browser network route <url> --body '{}'  # Mock response
silicon-browser network unroute [url]            # Remove routes
silicon-browser network requests                 # View tracked requests
silicon-browser network requests --filter api    # Filter
```

### JavaScript Evaluation

Shell quoting corrupts complex JS. Use `--stdin` or `-b` for anything beyond simple expressions.

```bash
# Simple (single quotes, no nesting)
silicon-browser eval 'document.title'
silicon-browser eval 'document.querySelectorAll("img").length'

# Complex: heredoc (RECOMMENDED)
silicon-browser eval --stdin <<'EVALEOF'
JSON.stringify(
  Array.from(document.querySelectorAll("img"))
    .filter(i => !i.alt)
    .map(i => ({ src: i.src.split("/").pop(), width: i.width }))
)
EVALEOF

# Alternative: base64
silicon-browser eval -b "$(echo -n 'Array.from(document.querySelectorAll("a")).map(a => a.href)' | base64)"
```

Rules: single-line no nested quotes = `eval 'expr'`. Nested quotes/multiline/arrow functions = `eval --stdin <<'EVALEOF'`. Programmatic = `eval -b`.

### Diff (Compare Page States)

```bash
silicon-browser diff snapshot                          # Current vs last snapshot
silicon-browser diff snapshot --baseline before.txt    # Current vs saved file
silicon-browser diff screenshot --baseline before.png  # Visual pixel diff
silicon-browser diff url <url1> <url2>                 # Compare two pages
silicon-browser diff url <url1> <url2> --screenshot    # Visual comparison
```

### Semantic Locators (Alternative to Refs)

When refs are unavailable or unreliable:

```bash
silicon-browser find text "Sign In" click
silicon-browser find label "Email" fill "user@test.com"
silicon-browser find role button click --name "Submit"
silicon-browser find placeholder "Search" type "query"
silicon-browser find testid "submit-btn" click
silicon-browser find first ".item" click
silicon-browser find nth 2 "a" hover
```

### Browser Settings

```bash
silicon-browser set geo 37.7749 -122.4194       # Geolocation
silicon-browser set offline on                  # Offline mode
silicon-browser set headers '{"X-Key":"v"}'     # Extra HTTP headers
silicon-browser set credentials user pass       # HTTP basic auth
silicon-browser set media dark                  # Dark mode
silicon-browser set media light reduced-motion  # Light + reduced motion
```

### State Management

```bash
silicon-browser state save auth.json    # Save cookies, storage, auth
silicon-browser state load auth.json    # Restore saved state
silicon-browser state list              # List saved states
silicon-browser state clear myapp       # Clear state
silicon-browser state clean --older-than 7  # Clean old states
```

### Sessions

```bash
silicon-browser --session site1 open https://site-a.com
silicon-browser --session site2 open https://site-b.com
silicon-browser session list
silicon-browser --session site1 close
```

### Debugging

```bash
silicon-browser --headed open https://example.com   # Show browser window
silicon-browser highlight @e1                       # Highlight element
silicon-browser inspect                             # Open Chrome DevTools
silicon-browser console                             # View console messages
silicon-browser errors                              # View page errors
silicon-browser record start demo.webm              # Record session
silicon-browser record stop                         # Stop recording
silicon-browser profiler start                      # Start profiling
silicon-browser profiler stop trace.json            # Stop and save
silicon-browser trace start                         # Start trace
silicon-browser trace stop trace.zip                # Stop and save
```

### Local Files

```bash
silicon-browser --allow-file-access open file:///path/to/document.pdf
silicon-browser --allow-file-access open file:///path/to/page.html
```

### iOS Simulator

```bash
silicon-browser device list
silicon-browser -p ios --device "iPhone 16 Pro" open https://example.com
silicon-browser -p ios snapshot -i
silicon-browser -p ios tap @e1
silicon-browser -p ios swipe up
silicon-browser -p ios screenshot mobile.png
silicon-browser -p ios close
```

Requires macOS + Xcode + Appium (`npm install -g appium && appium driver install xcuitest`).

## Authentication Patterns

### Option 1: Profile (best for recurring tasks)

Login once, reuse forever. Profile persists cookies and storage.

```bash
silicon-browser --profile myapp open https://app.example.com/login
# ... fill credentials, submit, wait for dashboard ...
silicon-browser close

# Future runs: already authenticated
silicon-browser --profile myapp open https://app.example.com/dashboard
```

### Option 2: State Save/Load

Save auth state as a portable JSON file.

```bash
# After logging in:
silicon-browser state save auth.json

# In a future session or different machine:
silicon-browser state load auth.json
silicon-browser open https://app.example.com/dashboard
```

State files contain session tokens in plaintext. Add to `.gitignore`. Set `SILICON_BROWSER_ENCRYPTION_KEY` for encryption at rest.

### Option 3: Auto-Connect (import from user's browser)

Fastest for one-off tasks. Grabs cookies from the user's running Chrome.

```bash
silicon-browser --auto-connect state save ./auth.json
silicon-browser --state ./auth.json open https://app.example.com/dashboard
```

### Option 4: Auth Vault (credentials stored encrypted)

LLM never sees the password.

```bash
# Save once (pipe password to avoid shell history)
echo "$PASSWORD" | silicon-browser auth save github --url https://github.com/login --username user --password-stdin

# Login by name
silicon-browser auth login github

# Manage
silicon-browser auth list
silicon-browser auth show github
silicon-browser auth delete github
```

### Option 5: Session Persistence (auto-save/restore)

```bash
silicon-browser --session-name myapp open https://app.example.com/login
# ... login ...
silicon-browser close    # state auto-saved

# Next time: auto-restored
silicon-browser --session-name myapp open https://app.example.com/dashboard
```

## Common Patterns

### Form Filling

```bash
silicon-browser open https://example.com/signup
silicon-browser snapshot -i
silicon-browser fill @e1 "Jane Doe"
silicon-browser fill @e2 "jane@example.com"
silicon-browser select @e3 "California"
silicon-browser check @e4
silicon-browser click @e5
silicon-browser wait --load networkidle
silicon-browser snapshot -i    # verify result
```

### Data Extraction

```bash
silicon-browser open https://example.com/products
silicon-browser snapshot -i
silicon-browser get text @e5                    # specific element
silicon-browser get text body > page.txt        # all page text to file

# JSON output for parsing
silicon-browser snapshot -i --json
silicon-browser get text @e1 --json
```

### Scrape with JavaScript

```bash
silicon-browser open https://example.com/products
silicon-browser eval --stdin <<'EVALEOF'
JSON.stringify(
  Array.from(document.querySelectorAll('.product')).map(p => ({
    name: p.querySelector('h2')?.textContent?.trim(),
    price: p.querySelector('.price')?.textContent?.trim()
  }))
)
EVALEOF
```

### File Upload (Hidden Inputs)

```bash
# Most sites hide the file input
silicon-browser upload 'input[type="file"]' /path/to/image.png

# Multiple files
silicon-browser upload 'input[type="file"]' ./img1.png ./img2.png

# If visible in snapshot, use ref
silicon-browser upload @e7 /path/to/document.pdf
```

### Responsive Testing

```bash
silicon-browser set viewport 1920 1080 && silicon-browser screenshot desktop.png
silicon-browser set viewport 375 812 && silicon-browser screenshot mobile.png
silicon-browser set device "iPhone 14" && silicon-browser screenshot device.png
```

## Ref Lifecycle (Important)

Refs (`@e1`, `@e2`, etc.) are **invalidated** when the page changes. You MUST re-snapshot after:

- Clicking links or buttons that navigate
- Form submissions
- Dynamic content loading (dropdowns, modals, AJAX)

```bash
silicon-browser click @e5              # navigates to new page
silicon-browser snapshot -i            # MUST re-snapshot — old refs are dead
silicon-browser click @e1              # use new refs
```

Never cache refs across page changes. Always snapshot again.

## Tips

- **Chain with `&&`**: `silicon-browser open url && silicon-browser wait --load networkidle && silicon-browser screenshot` -- faster than separate calls.
- **JSON output**: Add `--json` to any command for machine-parseable output.
- **Debug with `--headed`**: See the browser window. Also `SILICON_BROWSER_HEADED=1`.
- **Network idle**: Use `silicon-browser wait --load networkidle` after `open` for slow pages.
- **Default timeout**: 25 seconds. Override with `SILICON_BROWSER_DEFAULT_TIMEOUT` (milliseconds).
- **Always close**: Run `silicon-browser close` when done to avoid leaked processes.
- **Named sessions**: Use `--session <name>` for parallel agents to avoid conflicts.
- **Incognito for one-offs**: `--incognito` gives a throwaway session with random fingerprint.
- **Content boundaries**: Set `SILICON_BROWSER_CONTENT_BOUNDARIES=1` to wrap page output in markers that help LLMs distinguish tool output from untrusted page content.
- **Domain allowlist**: `SILICON_BROWSER_ALLOWED_DOMAINS="example.com,*.example.com"` restricts navigation.
- **Idle auto-shutdown**: `SILICON_BROWSER_IDLE_TIMEOUT_MS=60000` for ephemeral/CI environments.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `SILICON_BROWSER_NO_STEALTH` | `1` to disable stealth |
| `SILICON_BROWSER_HEADLESS_REAL` | `1` to force traditional `--headless=new` instead of offscreen headed |
| `SILICON_BROWSER_FINGERPRINT` | Pin fingerprint seed (consistent identity) |
| `SILICON_BROWSER_HEADED` | `1` to show browser window |
| `SILICON_BROWSER_SESSION` | Default session name |
| `SILICON_BROWSER_ENCRYPTION_KEY` | Encrypt state files at rest |
| `SILICON_BROWSER_DEFAULT_TIMEOUT` | Default timeout in ms (default: 25000) |
| `SILICON_BROWSER_IDLE_TIMEOUT_MS` | Auto-shutdown after inactivity |
| `SILICON_BROWSER_ALLOWED_DOMAINS` | Restrict navigation to these domains |
| `SILICON_BROWSER_CONTENT_BOUNDARIES` | `1` to wrap page output in markers |
| `SILICON_BROWSER_MAX_OUTPUT` | Limit output length (prevents context flooding) |
| `SILICON_BROWSER_COLOR_SCHEME` | `dark` or `light` |
| `SILICON_BROWSER_NO_TUNNEL` | `1` to disable auto-tunnel on push |
| `SILICON_BROWSER_DOWNLOAD_PATH` | Default download directory |
| `SILICON_BROWSER_PROXY` | Default proxy URL |
| `SILICON_BROWSER_ENGINE` | `chrome` (default) or `lightpanda` |
| `SILICON_BROWSER_SCREENSHOT_DIR` | Default screenshot directory |
| `SILICON_BROWSER_SCREENSHOT_FORMAT` | `png` or `jpeg` |
| `SILICON_BROWSER_SCREENSHOT_QUALITY` | JPEG quality (0-100) |



### Local Files (PDFs, HTML)

```bash
silicon-browser --allow-file-access open file:///path/to/document.pdf
silicon-browser --allow-file-access open file:///path/to/page.html
silicon-browser screenshot output.png
```

## Ref Lifecycle (Important)

Refs (`@e1`, `@e2`, etc.) are invalidated when the page changes. Always re-snapshot after:

- Clicking links or buttons that navigate
- Form submissions
- Dynamic content loading (dropdowns, modals)

```bash
silicon-browser click @e5              # Navigates to new page
silicon-browser snapshot -i            # MUST re-snapshot
silicon-browser click @e1              # Use new refs
```

## Semantic Locators (Alternative to Refs)

When refs are unavailable or unreliable, use semantic locators:

```bash
silicon-browser find text "Sign In" click
silicon-browser find label "Email" fill "user@test.com"
silicon-browser find role button click --name "Submit"
silicon-browser find placeholder "Search" type "query"
silicon-browser find testid "submit-btn" click
```

## JavaScript Evaluation (eval)

Use `eval` to run JavaScript in the browser context. **Shell quoting can corrupt complex expressions** -- use `--stdin` or `-b` to avoid issues.

```bash
# Simple expressions work with regular quoting
silicon-browser eval 'document.title'
silicon-browser eval 'document.querySelectorAll("img").length'

# Complex JS: use --stdin with heredoc (RECOMMENDED)
silicon-browser eval --stdin <<'EVALEOF'
JSON.stringify(
  Array.from(document.querySelectorAll("img"))
    .filter(i => !i.alt)
    .map(i => ({ src: i.src.split("/").pop(), width: i.width }))
)
EVALEOF

# Alternative: base64 encoding (avoids all shell escaping issues)
silicon-browser eval -b "$(echo -n 'Array.from(document.querySelectorAll("a")).map(a => a.href)' | base64)"
```

**Rules of thumb:**
- Single-line, no nested quotes -> regular `eval 'expression'` with single quotes is fine
- Nested quotes, arrow functions, template literals, or multiline -> use `eval --stdin <<'EVALEOF'`
- Programmatic/generated scripts -> use `eval -b` with base64
