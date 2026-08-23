---
name: tokopedia-search
description: Search Tokopedia (Indonesian marketplace) for products with price filters via the tokopaedi Python library. Use when the user asks to find, compare, or price-check products on Tokopedia.
---

# Tokopedia Search

Search Tokopedia listings with price/rating filters using the `tokopaedi` library. No API key needed.

## Setup

`tokopaedi` runs via uvx (no install into the environment):

```bash
uvx --from tokopaedi python <script.py>
```

If `uvx` is missing, fall back to `pip install tokopaedi`.

## Usage

Write a script and run it through uvx:

```python
from tokopaedi import search, SearchFilters

filters = SearchFilters(pmin=4500000, pmax=7000000)
results = search("<product>", max_result=100, filters=filters)

for r in results:
    print(f"Rp{r.price:,} | sold={r.sold_count} | {r.product_name}")
    print(f"   {(r.url or '').split('?')[0]}")
```

`search()` paginates internally: it follows `additionalParams` (`next_param`) recursively until `max_result` is reached or pages run out. No manual pagination needed - just raise `max_result`. Dedupe still required (URLs carry `?extParam=...`).

Run:

```bash
uvx --from tokopaedi python script.py
```

### SearchFilters options

- `pmin`, `pmax`: price range in IDR
- `rt`: minimum rating
- Condition/shipping/cashback flags exist too; inspect with `help(SearchFilters)`

### Product fields

`product_id`, `product_name`, `url`, `price`, `price_original`,
`discount_percentage`, `sold_count`, `rating`, `review_count`,
`total_stock`, `shop`, `description`, `variants`, `reviews`

Useful methods on results:
- `results.enrich_details()` — fetch full details per item (slow)
- `results.enrich_reviews(max_result=50)` — fetch reviews per item (slow)

## Variant price verification

`get_product()` (detail API) is broken - Tokopedia rejects its mobile-app fingerprint with `curl: (92) HTTP/2 stream error` on every attempt. Only `search()` works. To get real per-variant prices, drive the CDP-attached Chrome from the shopee-search skill:

```bash
uv run --with requests --with websocket-client python .pi/skills/tokopedia-search/scripts/tp_variants.py <product-url> [more-urls]
```

`tp_variants.py` drives the same CDP Chrome as the shopee-search skill - on Wayland sessions it must run under XWayland (`--ozone-platform=x11`, see shopee-search SKILL.md) and the warm-up/wedge rules for that browser apply here too.

`tp_variants.py` skips network capture entirely and works on the rendered DOM: cache-bust the URL, wait for the buy box (`Subtotal` text), record the default selection under `Terpilih:`, then click each group pill (the higher-level variant family, e.g. a model/CPU tier) and each option pill (the spec axis, e.g. capacity/RAM), re-reading the price after every click. Non-obvious details learned in practice:

- **The GraphQL path is mostly dead**: PDPMainInfo arrives as an unparseable compact binary payload on nearly every listing now (~5/5 observed in one session; earlier estimate was ~1 in 3), even with cache-busting. Don't rely on it.
- **`[data-testid="lblPDPDetailProductPriceAmount"]` no longer exists**. Read the buy box from body text instead: match `/Subtotal[^R]*Rp\s*([\d.,]+)/`.
- **Out-of-stock combos show `-` as Subtotal** but the real price still renders lower on the page (take the LAST `Rp` match in body text) and "Stok varian ini habis" / "Stok: Habis" appears. Record these with an OOS flag instead of dropping them.
- **Pill classification trap**: spec pills that name a spec axis (e.g. capacity tokens like GB/TB or the word RAM) must never be classified as groups even when they also contain model-ish text. Classify a pill as a group only if it matches the tier/model keyword list AND contains no capacity/storage token; everything else is an option. Misclassifying cascades wrong clicks and reads stale states.
- **Clicks change state permanently** within a session: clicking a group then an option composes a selection. Enumerate group x option combos deliberately; re-clicking the default group resets.
- **Cache-bust the PDP URL** (append `?ck=<timestamp>`): the prefetch cache can serve a stale payload.
- One listing in ~10 is delisted between search and check (404 "Waduh, tujuanmu nggak ada") or never renders its buy box - treat as dead, don't retry.
- **Tab hygiene**: same policy as shopee-search - reuse the parked `about:blank` tab, never close the last page target (Chrome exits entirely).

## Gotchas

- **Multi-variant listings show the starting variant's price** as `price`. The headline config may cost more via the variant picker on the product page. Advise users to confirm exact config with the seller before buying.
- **Headline price can be a stripped config**: verified case - a listing advertised at Rp5.32jt whose default variant had NO storage installed; every real storage option cost more (Rp5.94jt / Rp6.54jt / Rp7.94jt). A price that looks too good for the titled specs usually maps to a stripped config.
- **Higher variants can leave your price band entirely**: verified case - a listing advertised at Rp5.4jt (its base tier) whose top-tier variant costs Rp7.75jt, well above the advertised figure. When a user filters by price range, only the cheapest combo is guaranteed inside it; verify every variant they might want.
- **Rate limit**: add `time.sleep(2)` between successive `search()` calls.
- **Bait pricing verified at scale**: in one session nearly every multi-variant headline was misleading - e.g. a listing advertising "high-RAM/large-storage @Rp5.79jt" where that price was actually the entry chip with minimal storage (the advertised config really started ~Rp8.4jt); another advertising the top CPU tier at Rp6.5jt whose top tier really costs Rp7.8jt. Always verify before quoting.
- **Dedupe across queries**: URLs carry a query-string (`?extParam=...`); strip it before deduping.
- **Field names**: it is `product_name`, not `name`; `ProductData` has no `.name`.
- `search()` returns `None` on any exception (bare `except` prints traceback and returns None) - check for it before iterating.
- `tp_variants.py` emits one JSON object per line (NDJSON, not a single document). Parse multi-line output with repeated `json.JSONDecoder().raw_decode()`, not `json.load()`. One object is printed per URL even on error, so partial results survive crashes.
- Result count may fall short of `max_result` even without blocking (e.g. 73 unique for a broad keyword at max_result=100) - that is the end of pagination, not an error.
- Plain `curl` against tokopedia.com gets blocked by TLS-fingerprint bot detection; do not hand-roll HTTP requests - always go through tokopaedi (it uses curl-cffi to impersonate Chrome).

## Interpreting results

- `sold_count > 100` signals a proven seller listing; near 0 means new or unproven.
