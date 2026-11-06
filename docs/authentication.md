# Authentication

`overleaf-sync-now` needs an `overleaf_session2` session cookie to call Overleaf's project endpoints — `/project/<id>/updates` (the version-history probe) and `/project/<id>/download/zip` (the conditional zip pull). It walks an **auth chain** in priority order — first hit wins, and the cookie is cached for subsequent runs.

## Auth chain

| # | Source | Used by | Notes |
|---|---|---|---|
| 1 | **Cached cookie** (`<DATA_DIR>/cookies.json` — default `~/.overleaf-sync/cookies.json`; legacy `~/.claude/overleaf-data/cookies.json`) | everything | After first successful auth. Re-validated against Overleaf at most once a minute (a successful check is cached for 60 s). Run `overleaf-sync-now status` to see the resolved data dir. |
| 2 | **Browser via `login` profile** | everything | Persistent browser profile created by `overleaf-sync-now login` (patchright preferred since 0.3.2; vanilla Playwright fallback). **The recommended path on every OS.** |
| 3 | **`rookiepy`** | setup, install, status, doctor; `sync` (then backs off for an hour after a failure) | Rust cookie reader for your installed browsers. Defeated by Chrome 130+ App-Bound Encryption on Windows. |
| 4 | **`browser_cookie3`** | same as 3 | Chrome / Edge / Firefox / Brave / Vivaldi / Opera / Chromium / LibreWolf / Arc / Safari. Defeated by Chrome 127+ on Windows without admin. |
| 5 | **Claude Code Playwright MCP profile** (`~/.claude/playwright-profile/`) | same as 3 | If you've used the [Playwright MCP](https://github.com/microsoft/playwright-mcp) and logged into overleaf.com there. |
| 6 | **Hidden prompt** | interactive `setup` only | You type the cookie yourself (input hidden). Last resort. |

The PreToolUse hook only uses steps 1 and 2. Steps 3-5 read your browsers' own cookie stores, which on macOS goes through the keychain (see below), so they never run on every AI read or edit.

If Overleaf can't be reached at all (offline, a DNS or proxy failure, an outage, or a sandbox without network access such as Codex CLI's default), the chain stops: the cookie can't be checked, and that is reported as **"could not reach Overleaf"**, never as an expired login. `status` then shows `Cookie auth: UNKNOWN`.

## When to use which

| Situation | Path |
|---|---|
| Any OS, `install` ended with `AUTH NOT YET CAPTURED` | Run `overleaf-sync-now login` once in a terminal. A browser opens, you log into Overleaf there, and the session persists for weeks. |
| **Windows + Chrome 130 or later** | `login` (see below for why reading Chrome's cookies fails). |
| **macOS + Safari** | `login`. macOS protects Safari's cookie file; a terminal can only read it with Full Disk Access, which there is no reason to grant for this. |
| macOS / Linux with Chrome, Edge, Firefox or Brave, already logged into Overleaf | `install` / `setup` usually pick that session up (on macOS after a keychain prompt). |
| Server / no display | Run `overleaf-sync-now save-cookie` yourself and type the cookie (copied from another machine's browser) at its hidden prompt. |

## What `login` opens and downloads

`login` opens your installed **Chrome**, or **Edge** if there is no Chrome, with a separate profile kept in `<DATA_DIR>/browser-profile/`. Only when neither can be started does it download Playwright's Chromium: about 300 MB (Chromium plus its headless shell and ffmpeg), stored in the Playwright cache (`~/Library/Caches/ms-playwright` on macOS, `~/.cache/ms-playwright` on Linux, `%LOCALAPPDATA%\ms-playwright` on Windows, or `$PLAYWRIGHT_BROWSERS_PATH`). That cache is shared with other Playwright tools; delete it only if nothing else uses it.

Later, the tool reopens the profile headless with the same browser that created it, to read the session.

## The macOS keychain prompt

Chromium-based browsers on macOS (Chrome, Edge, Brave, Vivaldi, Opera, Arc, Chromium) encrypt their cookies with a key stored in your login keychain as "<Browser> Safe Storage". When `install`, `setup`, `status` or `doctor` look for an existing Overleaf login in those browsers, macOS may show a dialog asking for your login password so that `security` can read that key, once per installed browser.

- **Allow**: the tool can use that browser's Overleaf session. You may be asked again next time.
- **Deny**: nothing is read; run `overleaf-sync-now login` instead.
- **Always Allow** is best avoided: it adds the `security` tool to that key's access list, so any program that runs `security` can read the browser's cookie key later without a prompt.

The PreToolUse hook never does this (it only uses the cached cookie and the `login` profile), and `sync` stops trying for an hour after the browsers failed once. The `login` browser itself does not use your keychain.

## Why Chrome 130+ on Windows needs the `login` path

Chrome 130 added **App-Bound Encryption (ABE)** on Windows: the cookie database AES key is wrapped a second time with a key bound to `Chrome.exe` itself, brokered through Windows COM elevation. Any process that isn't `Chrome.exe` (and isn't running as administrator) can read the encrypted bytes but can't decrypt them. So `browser_cookie3` and `rookiepy` both fail no matter how recently you logged in — the encryption layer is the blocker, not the cookie's freshness.

`overleaf-sync-now login` sidesteps this entirely: you log into the separate profile it opens, and the tool reads cookies via the Chrome DevTools Protocol — which returns plaintext because the request comes from inside the browser. The login persists in that profile for weeks.

## What if Google blocks the sign-in?

If your Overleaf account uses **"Sign in with Google"**, you may see Google's *"This browser or app may not be secure"* page when you click the Google button inside the `login` window. This is Google's anti-automation gate (`disallowed_useragent`). It fires against any browser launched under remote-control protocols — not specifically against this tool.

Since 0.3.2 the `login` command uses [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) (a maintained Playwright fork that patches the CDP `Runtime.Enable` leak Google's gate detects) when it's installed, with vanilla Playwright as a fallback. patchright improves the odds Google's gate doesn't fire, but Google iterates against bypasses, so the supported escape hatch is:

1. Visit https://www.overleaf.com/user/password/reset
2. Enter your account email; Overleaf will email you a password-set link.
3. Set an Overleaf-specific password. Your Google sign-in still works in your normal browser; this just adds email+password as an alternate login path. *(This is the official Overleaf workaround for accounts originally registered via Google, ORCID, or Twitter.)*
4. Re-run `overleaf-sync-now login` and use **email + password** on the Overleaf form. Google never sees that flow.

If you can't change the account, fall back to [manual paste](#manual-paste-fallback).

## Manual paste fallback

If everything else fails (no browser available, locked-down environment), copy the cookie from another machine and save it **yourself, in your own terminal**:

```bash
overleaf-sync-now save-cookie            # prompts; input is hidden
# or pipe it in (here: the macOS clipboard), so it never touches shell history:
pbpaste | overleaf-sync-now save-cookie -
```

To get the value, open overleaf.com (logged in) and the browser's developer tools:

| Browser | Open developer tools | Where the cookie is |
|---|---|---|
| Chrome, Edge | F12 or Ctrl+Shift+I (Windows, Linux); Cmd+Option+I (macOS) | **Application → Cookies → https://www.overleaf.com** |
| Firefox | F12 or Ctrl+Shift+I (Windows, Linux); Cmd+Option+I (macOS) | **Storage → Cookies → https://www.overleaf.com** |
| Safari | First turn on Safari → Settings → Advanced → "Show features for web developers", then Cmd+Option+I | **Storage → Cookies** |

Copy the `Value` of the `overleaf_session2` row.

**Never paste the cookie into an AI chat, and don't pass it as a command-line argument.** It grants full access to your Overleaf account. Text you give an agent is stored in the model provider's transcript and in local session logs; an argument is visible in the process list and your shell history. (`save-cookie <value>` still works for old scripts but prints a warning.) If a cookie has leaked, end the session on Overleaf's side: on overleaf.com open **Account → Account Settings → Sessions → Manage your sessions** and click **Clear Sessions** (or sign out in the browser it came from). Then run `overleaf-sync-now login` again.
