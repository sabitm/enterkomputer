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
  --remote-allow-origins=* --ozone-platform=x11 \
  --user-data-dir=$PWD/ignored/shopee-profile \
  https://shopee.co.id/login
```

The session persists in the profile. If Shopee shows a slider CAPTCHA ("Geser untuk menyelesaikan puzzle"), solve it in the window; this recurs occasionally after idle periods and is unavoidable.

## Usage

```bash
uv run --with requests --with websocket-client python .pi/skills/shopee-search/scripts/shopee_search.py "<product>" \
  --pmin 3000000 --pmax 7000000 --limit 10 --pages 3
```

The script auto-launches Chrome with `ignored/shopee-profile` if not already running, attaches via CDP (`Page.navigate` + `Runtime.evaluate`), waits for client-side render, extracts product anchors matching `-i.<shopid>.<itemid>`, then filters by price. `--pages N` walks result pages `&page=0..N-1` (0-indexed) and dedupes by product URL.

When `--pmin`/`--pmax` are given, the script also applies Shopee's server-side price filter so only in-band cards hydrate (fewer anchors per page, faster runs, less wedging risk). It appends `fe_filter_options=[{"group_name":"PRICE_RANGE","values":["<min>\u25b6\u25c0<max>"]}]` to the search URL, e.g. for 5-7mio:

```
https://shopee.co.id/search?keyword=%3Cproduct%3E&page=0&fe_filter_options=%5B%7B%22group_name%22%3A%22PRICE_RANGE%22%2C%22values%22%3A%5B%225000000%E2%96%B6%E2%97%807000000%22%5D%7D%5D
```

A client-side band check still runs afterwards as a safety net: sponsored/ad cards can bypass server filters and land out of band.

Flags: `--pmin`/`--pmax` (IDR), `--limit`.

## Gotchas

- **Bait pricing is the norm, not the exception**: multi-variant listings advertise the *cheapest* variant under headline specs. Verified examples: a listing advertising its premium CPU tier at Rp5jt (real: Rp8jt); one advertising a top-tier 32GB/1TB config at Rp5.14jt (real: Rp9.09jt - the cheap price was the entry CPU with NO storage); one advertising the flagship chip + 32GB/1TB at Rp5.29jt (real: ~Rp10jt). Never quote a headline spec-price without checking `item.models` first.
- **Headless mode is detected** - redirects to `/verify/traffic/error`. Always run headed.
- **On Wayland sessions, Chrome MUST run under XWayland (`--ozone-platform=x11`)**: native-Wayland Chrome accepts no trusted input over CDP - `Input.dispatchMouseEvent` and `Input.synthesizeScrollGesture` hang forever, so the result grid never hydrates and every search times out while `Runtime.evaluate` keeps working. Verified on NixOS + Wayland (Chrome 150). The scripts pass this flag when auto-launching; keep it in manual launches too.
- **After an unclean kill, remove stale `Singleton{Lock,Cookie,Socket}` from the profile dir** or a fresh launch hands off to the dead instance and exits silently (port never comes up).
- **Cold navigation may trigger CAPTCHA** even when logged in; solve it in the visible window and rerun.
- **First paint is slow**: poll for product anchors up to ~25s instead of fixed sleeps.
- Product cards do NOT use `[data-sqe="link"]` in current DOM; match anchors by the `-i.<shopid>.<itemid>` URL pattern.
- **Grid hydration is gated on trusted scroll input**: without it the page stalls at ~13 anchors (an ads-only carousel) and the result grid never mounts. See the hydration requirements below - synthetic `window.scrollTo` does NOT count as input.
- Early anchor counts are partial (ads only, 1-13); treat <30 anchors as not-yet-rendered and keep polling. Allow ~45s for cold starts.
- Sponsored/ad cards also match the `-i.<shopid>.<itemid>` pattern and can slip into results (e.g. unrelated products at odd prices).
- Price/name parsing from card text is noisy: names may embed trailing prices/ratings (e.g., `...BergaransiRp3.400`). Strip before presenting.
- **Grid hydration requires ALL of**: `Page.bringToFront` (background tabs never hydrate), full page load (`readyState == complete`) plus ~10s settle BEFORE any input, then *trusted* wheel input via `Input.dispatchMouseEvent` - synthetic `window.scrollTo` does not count and the grid stalls at ads-only anchors.
- **Scroll pacing matters**: wheel in small steps (~1000px, 0.6s apart) with a ~5s settle after reaching bottom; scrolling too fast skips lazy-mounted cards mid-hydration. One search page serves exactly 60 organic items (+ a few ad cards) - stop at >=62 anchors or after 3 stale bottom checks.
- **Price renders as two lines** in card text: a bare `Rp` line followed by a bare number line (`Rp\n2.800.000`). Parse that pair, not inline prices.
- **Tab hygiene**: scripts reuse an existing `about:blank` tab and park the shared tab there when done. NEVER close the last page target via `/json/close/<id>` - when no page targets remain, Chrome exits entirely and the profile must relaunch (slow, re-triggers anti-fraud).
- Keep `PORT=9226` and profile path consistent between manual login and script runs, or you will re-login.
- Requires deps at call time: `uv run --with requests --with websocket-client` (or `uvx`).

## Renderer wedging and recovery

- Long scraping sessions can wedge a renderer process: EVERY CDP call hangs (`Runtime.evaluate` times out even on a freshly attached live tab) while `/json/version` and `/json/list` still answer normally. The stuck tab survives restarts of your script - do not keep retrying.
- Recovery = kill Chrome and relaunch with the SAME profile dir and port; the Shopee login persists in the profile, no re-auth needed:
  ```bash
  pkill -f "[r]emote-debugging-port=9226"; sleep 3   # bracket trick: an unbracketed string self-matches THIS shell command and kills it mid-run
  rm -f <profile>/Singleton*                          # stale locks from unclean kills make new launches hand off and exit silently
  DISPLAY=:0 nohup google-chrome-stable --ozone-platform=x11 --remote-debugging-port=9226 ... # same flags as setup, about:blank
  ```
- Relaunch gotchas: pass `DISPLAY=:0` explicitly when launching from a non-interactive shell under nohup; verify readiness by polling `/json/version` in a loop (up to ~40s) instead of a fixed sleep; if the port never comes up while chrome processes linger, re-check that the kill actually landed before assuming a launch failure.

## Variant price verification

The search page shows only the headline (cheapest-variant) price. Real per-model prices come from the PDP API: drive the same CDP Chrome to a product URL, capture `/api/v4/pdp/get_pc` via `Network.enable`, then read `data.item.models[]` - each model has `name`, `price` (divide by 100000 for IDR), `sold_count`. Non-obvious details:

- **Cold-start PDP navigations are toxic**: if the tab's FIRST shopee page load is a PDP straight from `about:blank`, shopee either soft-redirects to the homepage (title becomes "Shopee Indonesia ...", zero `get_pc` requests) or wedges the renderer outright. Land on the homepage/search first (~8s), then hop PDP -> PDP in the SAME tab - chained hops capture `get_pc` reliably (~25-30s each, slower than search pages). `variant_check.py` does this warm-up automatically.
- State is in `window.__STORE__`, not `__INITIAL_STATE__`, and `item.items` is often empty after hydration - network capture is the reliable path.
- Hydration takes ~10s; DOM probes before that return an empty shell (~1.8KB body).
- **Single model = the title config is exact** (no variant bait). Multiple models = assume the advertised price maps to the cheapest one until proven otherwise.

Use `.pi/skills/shopee-search/scripts/variant_check.py <product-url> [more-urls]` which implements all of this (same script handles Tokopedia; see tokopedia-search skill).

## Tokopedia alternative

For Tokopedia use the `tokopedia-search` skill instead - no browser needed (curl-cffi defeats its TLS-fingerprint check).
