---
name: web-browser
description: "Allows to interact with web pages by performing actions such as clicking buttons, filling out forms, and navigating links. It works by remote controlling Google Chrome or Chromium browsers using the Chrome DevTools Protocol (CDP). When Claude needs to browse the web, it can use this skill to do so."
license: Stolen from Mario
---

# Web Browser Skill

Minimal CDP tools for collaborative site exploration.

## Start Chrome

```bash
./scripts/start.js              # Fresh profile
./scripts/start.js --profile    # Copy your profile (cookies, logins)
```

Start Chrome on `:9222` with remote debugging.

`start.js` auto-detects Chrome/Chromium, including Playwright-managed binaries under `/opt/ms-playwright` and `~/.cache/ms-playwright`. Use `CHROME_BIN` to force a path.

## Navigate

```bash
./scripts/nav.js https://example.com
./scripts/nav.js https://example.com --new
./scripts/nav.js https://idp.example/auth --url-contains keycloak
```

Navigate current tab or open new tab.

Targeting flags (supported by nav/eval/screenshot/click/type/wait):
- `--url-contains <fragment>`: select tab whose URL contains fragment
- `--target-id <id>`: use specific CDP target id
- `--wait-new-tab`: wait for a newly opened tab/page (popup/new-tab auth flows)
- `--timeout-ms <ms>`: override timeout

## Evaluate JavaScript

```bash
./scripts/eval.js 'document.title'
./scripts/eval.js 'document.querySelectorAll("a").length'
./scripts/eval.js --url-contains keycloak 'location.href'
```

Execute JavaScript in selected tab (async context). Be careful with string escaping; single quotes are easiest.

## First-class click/type/wait helpers

```bash
./scripts/click.js '#kc-login'
./scripts/type.js '#username' 'nick'
./scripts/type.js '#password' 'ChangeMe1234!'
./scripts/wait.js --url-contains '/hub/home'
./scripts/wait.js --wait-new-tab --url-contains keycloak
```

These helpers reduce brittle one-off JS snippets and improve auth-flow reliability.

## Screenshot

```bash
./scripts/screenshot.js
./scripts/screenshot.js --url-contains '/hub/home'
```

Screenshot selected viewport; prints temp file path.

## Pick Elements

```bash
./scripts/pick.js "Click the submit button"
```

Interactive element picker. Click to select, Cmd/Ctrl+Click for multi-select, Enter to finish.

## Dismiss Cookie Dialogs

```bash
./scripts/dismiss-cookies.js          # Accept cookies
./scripts/dismiss-cookies.js --reject # Reject cookies (where possible)
```

Automatically dismisses EU cookie consent dialogs.

Run after navigating to a page:
```bash
./scripts/nav.js https://example.com && ./scripts/dismiss-cookies.js
```

## Background Logging (Console + Errors + Network)

Automatically started by `start.js` and writes JSONL logs to:

```
~/.cache/agent-web/logs/YYYY-MM-DD/<targetId>.jsonl
```

Manually start:
```bash
./scripts/watch.js
```

Tail latest log:
```bash
./scripts/logs-tail.js           # dump current log and exit
./scripts/logs-tail.js --follow  # keep following
```

Summarize network responses:
```bash
./scripts/net-summary.js
```
