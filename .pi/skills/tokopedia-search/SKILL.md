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
results = search("thinkpad t14", max_result=10, filters=filters)

for r in results:
    print(f"Rp{r.price:,} | sold={r.sold_count} | {r.product_name}")
    print(f"   {(r.url or '').split('?')[0]}")
```

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

## Gotchas

- **Multi-variant listings show the starting variant's price** as `price`. The headline config may cost more via the variant picker on the product page. Advise users to confirm exact config with the seller before buying.
- **Rate limit**: add `time.sleep(2)` between successive `search()` calls.
- **Dedupe across queries**: URLs carry a query-string (`?extParam=...`); strip it before deduping.
- **Field names**: it is `product_name`, not `name`; `ProductData` has no `.name`.
- Plain `curl` against tokopedia.com gets blocked by TLS-fingerprint bot detection; do not hand-roll HTTP requests — always go through tokopaedi (it uses curl-cffi to impersonate Chrome).

## Interpreting results

- `sold_count > 100` signals a proven seller listing; near 0 means new or unproven.
- For used laptops, business lines (ThinkPad T14/T14s/X1 Carbon, EliteBook 840 G8, Latitude 7420/7320) dominate the 4-7jt IDR range and beat consumer laptops at the same price.
