---
name: shopee-search
description: Search Shopee Indonesia for products with price filters via CDP attached to a logged-in Chrome profile. Use when the user asks to find or compare products on Shopee.
---

# Shopee Search

Search Shopee Indonesia using a real Chrome instance driven over raw CDP. Shopee's antifraud (`af-ac-enc-dat`) cannot be forged by curl-cffi; a real browser must execute it. Search additionally requires a logged-in session.

## One-time setup

Launch Chrome with the dedicated profile and log in to Shopee manually once:

```bash
google-chrome-stable --remote-debugging-port=9226 --no-first-run \
  --remote-allow-origins=* \
  --user-data-dir=$PWD/ignored/shopee-profile \
  https://shopee.co.id/login
```

The session persists in the profile. If Shopee shows a slider CAPTCHA ("Geser untuk menyelesaikan puzzle"), solve it in the window; this recurs occasionally after idle periods and is unavoidable.

## Usage

```bash
uvx --with requests --with websocket-client python scripts/shopee_search.py "thinkpad t14 second" \
  --pmin 3000000 --pmax 7000000 --limit 10 --pages 3
```

The script auto-launches Chrome with `ignored/shopee-profile` if not already running, attaches via CDP (`Page.navigate` + `Runtime.evaluate`), waits for client-side render, extracts product anchors matching `-i.<shopid>.<itemid>`, then filters by price. `--pages N` walks result pages `&page=0..N-1` (0-indexed) and dedupes by product URL.

Flags: `--pmin`/`--pmax` (IDR), `--limit`.

## Gotchas

- **Headless mode is detected** - redirects to `/verify/traffic/error`. Always run headed.
- **Cold navigation may trigger CAPTCHA** even when logged in; solve it in the visible window and rerun.
- **First paint is slow**: poll for product anchors up to ~25s instead of fixed sleeps.
- Product cards do NOT use `[data-sqe="link"]` in current DOM; match anchors by the `-i.<shopid>.<itemid>` URL pattern.
- **Grid hydration is gated on scroll input**: without scroll events the page stalls at ~13 anchors (an ads-only carousel) and the result grid never mounts. Poll loop must scroll incrementally (`window.scrollTo`) between checks.
- Early anchor counts are partial (ads only, 1-13); treat <30 anchors as not-yet-rendered and keep polling. Allow ~45s for cold starts.
- Sponsored/ad cards also match the `-i.<shopid>.<itemid>` pattern and can slip into results (e.g. unrelated products at odd prices).
- Price/name parsing from card text is noisy: names may embed trailing prices/ratings (e.g., `...BergaransiRp3.400`). Strip before presenting.
- Keep `PORT=9226` and profile path consistent between manual login and script runs, or you will re-login.
- Requires deps at call time: `uvx --with requests --with websocket-client`.

## Tokopedia alternative

For Tokopedia use the `tokopedia-search` skill instead - no browser needed (curl-cffi defeats its TLS-fingerprint check).
