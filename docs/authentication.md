# Authentication

`overleaf-sync-now` needs an `overleaf_session2` session cookie to call Overleaf's project endpoints — `/project/<id>/updates` (the version-history probe) and `/project/<id>/download/zip` (the conditional zip pull). It walks an **auth chain** in priority order — first hit wins, and the cookie is cached for subsequent runs.

## Auth chain

| # | Source | Notes |
|---|---|---|
| 1 | **Cached cookie** (`~/.claude/overleaf-data/cookies.json`) | After first successful auth. Validated against Overleaf on every use. |
| 2 | **Browser via `login` profile** | Persistent Playwright-managed Chromium. Created by `overleaf-sync-now login`. **The proper fix for Chrome 130+ Windows.** |
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

`overleaf-sync-now login` sidesteps this entirely: it launches a managed browser (separate Playwright-controlled Chromium with its own profile), you log in there, and we read cookies via the Chrome DevTools Protocol — which returns plaintext because the request comes from inside the browser. The login persists in our profile for weeks.

## Manual paste fallback

If everything else fails (no browser available, locked-down environment), copy the cookie from another machine and pass it directly:

```bash
overleaf-sync-now save-cookie "<value of overleaf_session2>"
```

To get the value: open DevTools on overleaf.com → **Application → Cookies → `overleaf_session2`**. Copy the `Value` column.
