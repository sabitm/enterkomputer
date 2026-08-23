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
uvx --with requests --with websocket-client python scripts/shopee_search.py "laptop" \
  --pmin 3000000 --pmax 7000000 --limit 10 --pages 3
```

The script auto-launches Chrome with `ignored/shopee-profile` if not already running, attaches via CDP (`Page.navigate` + `Runtime.evaluate`), waits for client-side render, extracts product anchors matching `-i.<shopid>.<itemid>`, then filters by price. `--pages N` walks result pages `&page=0..N-1` (0-indexed) and dedupes by product URL.

Flags: `--pmin`/`--pmax` (IDR), `--limit`.

## Gotchas

- **Bait pricing is the norm, not the exception**: multi-variant listings advertise the *cheapest* variant under headline specs. Verified examples: "T14 G3 i5 @5jt" (real: 8jt), "T14 i7 Gen11 32GB/1TB @5.14jt" (real: 9.09jt; 5.14jt = i5 with NO SSD), "EliteBook 845 G8 R7/32GB/1TB @5.29jt" (real: ~10jt). Never quote a headline spec-price without checking `item.models` first.
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

## Variant price verification

The search page shows only the headline (cheapest-variant) price. Real per-model prices come from the PDP API: drive the same CDP Chrome to a product URL, capture `/api/v4/pdp/get_pc` via `Network.enable`, then read `data.item.models[]` - each model has `name`, `price` (divide by 100000 for IDR), `sold_count`. Non-obvious details:

- State is in `window.__STORE__`, not `__INITIAL_STATE__`, and `item.items` is often empty after hydration - network capture is the reliable path.
- Hydration takes ~10s; DOM probes before that return an empty shell (~1.8KB body).
- **Single model = the title config is exact** (no variant bait). Multiple models = assume the advertised price maps to the cheapest one until proven otherwise.

Use `scripts/variant_check.py <product-url> [more-urls]` which implements all of this (same script handles Tokopedia; see tokopedia-search skill).

## Tokopedia alternative

For Tokopedia use the `tokopedia-search` skill instead - no browser needed (curl-cffi defeats its TLS-fingerprint check).
