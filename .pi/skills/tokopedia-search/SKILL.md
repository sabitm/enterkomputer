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
results = search("laptop", max_result=100, filters=filters)

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

`get_product()` (detail API) is broken - Tokopedia rejects its mobile-app fingerprint with `curl: (92) HTTP/2 stream error` on every attempt. Only `search()` works. To get real per-variant prices, drive the CDP-attached Chrome from the shopee-search skill with `scripts/variant_check.py`:

```bash
uv run --with requests --with websocket-client python scripts/variant_check.py <product-url> [more-urls]
```

It captures the `PDPMainInfo` GraphQL response via `Network.enable`; all RAM/SSD combo prices live in `components[name=new_variant_options].data[0].children[]` (`price`, `priceFmt`, `optionName`). Non-obvious details:

- **Cache-bust the PDP URL** (append `?ck=<timestamp>`): the prefetch cache serves a compact payload with empty `basicInfo.name` and no variant data.
- **Capture every matching response id**, not just the first/last: retries and replays can fire multiple times, and some are empty shells. Pick the one where `basicInfo.name` is non-null.
- **Single-config listings have no `new_variant_options` component**; fall back to reading rendered DOM prices (`RpX.XXX.XXX` leaf nodes) after ~6s hydration.
- One listing in ~10 is delisted between search and check (404 "Waduh, tujuanmu nggak ada") - treat as dead, don't retry.

## Gotchas

- **Multi-variant listings show the starting variant's price** as `price`. The headline config may cost more via the variant picker on the product page. Advise users to confirm exact config with the seller before buying.
- **Rate limit**: add `time.sleep(2)` between successive `search()` calls.
- **Dedupe across queries**: URLs carry a query-string (`?extParam=...`); strip it before deduping.
- **Field names**: it is `product_name`, not `name`; `ProductData` has no `.name`.
- `search()` returns `None` on any exception (bare `except` prints traceback and returns None) - check for it before iterating.
- Result count may fall short of `max_result` even without blocking (e.g. 73 unique for a broad keyword at max_result=100) - that is the end of pagination, not an error.
- Plain `curl` against tokopedia.com gets blocked by TLS-fingerprint bot detection; do not hand-roll HTTP requests - always go through tokopaedi (it uses curl-cffi to impersonate Chrome).

## Interpreting results

- `sold_count > 100` signals a proven seller listing; near 0 means new or unproven.
