# Authentication

`overleaf-sync-now` needs an `overleaf_session2` session cookie to call Overleaf's project endpoints — `/project/<id>/updates` (the version-history probe) and `/project/<id>/download/zip` (the conditional zip pull). It walks an **auth chain** in priority order — first hit wins, and the cookie is cached for subsequent runs.

## Auth chain

| # | Source | Notes |
|---|---|---|
| 1 | **Cached cookie** (`<DATA_DIR>/cookies.json` — default `~/.overleaf-sync/cookies.json`; legacy `~/.claude/overleaf-data/cookies.json`) | After first successful auth. Validated against Overleaf on every use. Run `overleaf-sync-now status` to see the resolved data dir. |
| 2 | **Browser via `login` profile** | Persistent patchright/Playwright-managed Chromium (patchright preferred since 0.3.2; vanilla Playwright fallback). Created by `overleaf-sync-now login`. **The proper fix for Chrome 130+ Windows.** |
| 3 | **`rookiepy`** | Rust cookie reader; handles Chrome 127–129 better than `browser_cookie3`. Still defeated by Chrome 130+ App-Bound Encryption. |
| 4 | **`browser_cookie3`** | Chrome / Edge / Firefox / Brave / Vivaldi / Opera / Chromium / LibreWolf via DPAPI. Defeated by Chrome 127+ on Windows without admin. |
| 5 | **Claude Code Playwright MCP profile** (`~/.claude/playwright-profile/`) | If you've used the [Playwright MCP](https://github.com/microsoft/playwright-mcp) and logged into overleaf.com there. |
| 6 | **Manual paste prompt** | Interactive only. Last resort. |

## When to use which

| Situation | Path |
|---|---|
| Most macOS / Linux users, or Windows + Chrome ≤126 | Auto-detect (chain steps 1–5) just works. |
| **Windows + Chrome 130 or later** | Run `overleaf-sync-now login` once. A managed browser opens, you log into Overleaf there, the session persists for weeks. |
| Server / no display | `overleaf-sync-now save-cookie "<value>"` after copying the cookie from another machine's DevTools. |

## Why Chrome 130+ needs the `login` path

Chrome 130 added **App-Bound Encryption (ABE)**: the cookie database AES key is wrapped a second time with a key bound to `Chrome.exe` itself, brokered through Windows COM elevation. Any process that isn't `Chrome.exe` (and isn't running as administrator) can read the encrypted bytes but can't decrypt them. So `browser_cookie3` and `rookiepy` both fail no matter how recently you logged in — the encryption layer is the blocker, not the cookie's freshness.

`overleaf-sync-now login` sidesteps this entirely: it launches a managed browser (separate patchright/Playwright-controlled Chromium with its own profile), you log in there, and we read cookies via the Chrome DevTools Protocol — which returns plaintext because the request comes from inside the browser. The login persists in our profile for weeks.

## What if Google blocks the sign-in?

If your Overleaf account uses **"Sign in with Google"**, you may see Google's *"This browser or app may not be secure"* page when you click the Google button inside the `login` window. This is Google's anti-automation gate (`disallowed_useragent`). It fires against any browser launched under remote-control protocols — not specifically against this tool.

Since 0.3.2 the `login` command uses [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) (a maintained Playwright fork that patches the CDP `Runtime.Enable` leak Google's gate detects) when it's installed, with vanilla Playwright as a fallback. patchright improves the odds Google's gate doesn't fire, but Google iterates against bypasses, so the supported escape hatch is:

1. Visit https://www.overleaf.com/user/password/reset
2. Enter your account email; Overleaf will email you a password-set link.
3. Set an Overleaf-specific password. Your Google sign-in still works in your normal browser; this just adds email+password as an alternate login path. *(This is the official Overleaf workaround for accounts originally registered via Google, ORCID, or Twitter.)*
4. Re-run `overleaf-sync-now login` and use **email + password** on the Overleaf form. Google never sees that flow.

If you can't change the account, fall back to [manual paste](#manual-paste-fallback).

## Manual paste fallback

If everything else fails (no browser available, locked-down environment), copy the cookie from another machine and pass it directly:

```bash
overleaf-sync-now save-cookie "<value of overleaf_session2>"
```

To get the value: open DevTools on overleaf.com → **Application → Cookies → `overleaf_session2`**. Copy the `Value` column.
