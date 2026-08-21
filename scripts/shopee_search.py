#!/usr/bin/env python3
"""Search Shopee Indonesia via CDP attached to a logged-in Chrome profile.

Requires a Chrome instance running with:
  google-chrome-stable --remote-debugging-port=9226 \
    --user-data-dir=<profile dir> --remote-allow-origins=*

The profile must have an active Shopee login session (log in once manually).
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse

import requests
import websocket

PORT = 9226
PROFILE = "/home/sabit/Downloads/projs/my/enterkomputer/ignored/shopee-profile"


def ensure_chrome():
    """Attach to running Chrome or launch a headed one with the shared profile."""
    try:
        tl = requests.get(f"http://127.0.0.1:{PORT}/json", timeout=2).json()
        if tl:
            return None
    except Exception:
        pass
    return subprocess.Popen([
        "google-chrome-stable", f"--remote-debugging-port={PORT}",
        "--no-first-run", "--remote-allow-origins=*",
        f"--user-data-dir={PROFILE}",
        "--window-size=1400,900", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def attach():
    for _ in range(40):
        try:
            tl = requests.get(f"http://127.0.0.1:{PORT}/json", timeout=2).json()
            pages = [t for t in tl if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit("no debuggable page found; is Chrome running?")


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=90)
        self.mid = 0

    def cmd(self, method, **params):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.mid:
                res = msg.get("result", {})
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return res


def parse_price(text):
    # card text glues price to rating/sold ("Rp5.199.0004.91"); only accept
    # fully dot-separated thousand groups so "5.199.0004" never parses
    m = re.search(r"\d{1,3}(?:\.\d{3})+", text)
    return int(m.group(0).replace(".", "")) if m else 0


def search(keyword, pmin=0, pmax=None, limit=15, max_wait=45, pages=1):
    proc = ensure_chrome()
    cdp = CDP(attach())
    items = []
    seen_urls = set()
    try:
        cdp.cmd("Page.enable")
        cdp.cmd("Runtime.enable")
        # shopee pagination is 0-indexed: page=0 is the first result page
        for page in range(pages):
            url = f"https://shopee.co.id/search?keyword={urllib.parse.quote(keyword)}&page={page}"
            cdp.cmd("Page.navigate", url=url)

            # poll until product cards render; grid hydration is gated on scroll
            # input, so scroll incrementally while polling. early counts can be
            # partial (ads-only), so keep the best seen
            deadline = time.time() + max_wait
            count = best = 0
            while time.time() < deadline:
                time.sleep(2)
                count = cdp.cmd(
                    "Runtime.evaluate",
                    expression="[...document.querySelectorAll('a')].filter(a => /-i\\.\\d+\\.\\d+/.test(a.href)).length",
                    returnByValue=True,
                )["result"]["value"]
                best = max(best, count)
                if count >= 30:
                    break
                cdp.cmd("Runtime.evaluate", expression=f"window.scrollTo(0, {600 * (best + 1)})",
                        returnByValue=True)
            count = best

            # grid lazy-loads more rows on scroll; without this only ~1 row is parsed
            if count:
                for h in range(4):
                    cdp.cmd("Runtime.evaluate", expression=f"window.scrollTo(0, {1400 * (h + 1)})",
                            returnByValue=True)
                    time.sleep(1.5)

            if not count:
                if page > 0:
                    break  # past last page of results
                page_url = cdp.cmd("Runtime.evaluate", expression="location.href", returnByValue=True)[
                    "result"]["value"]
                if "captcha" in page_url:
                    raise SystemExit("CAPTCHA shown - solve it in the Chrome window and retry")
                if "Masuk" in cdp.cmd("Runtime.evaluate",
                                      expression="document.body.innerText.slice(0,2000)",
                                      returnByValue=True)["result"]["value"]:
                    raise SystemExit("login required - log in once in the Chrome window and retry")
                raise SystemExit(f"no results rendered at {page_url}")

            js = """
(() => {
  const anchors = [...document.querySelectorAll('a')].filter(a => /-i\\.\\d+\\.\\d+/.test(a.href));
  const out = [];
  const seen = new Set();
  for (const a of anchors) {
    const url = a.href.split('?')[0];
    if (seen.has(url)) continue;
    seen.add(url);
    const label = (a.getAttribute('aria-label') || '').replace(/View product:\\s*/, '');
    const text = a.textContent || '';
    const pm = (label + ' ' + text).match(/Rp\\s?(\\d{1,3}(?:\\.\\d{3})+)/);
    const sm = text.match(/(\\d+[.,]?\\d*)\\s*\\+?\\s*terjual/i);
    out.push({
      url: url,
      name: (label || text).slice(0, 100),
      price_text: pm ? 'Rp' + pm[1] : '',
      sold: sm ? parseInt(sm[1].replace(',', ''), 10) : 0,
    });
  }
  return JSON.stringify(out.slice(0, 60));
})()
"""
            for it in json.loads(cdp.cmd("Runtime.evaluate", expression=js,
                                         returnByValue=True)["result"]["value"]):
                if it["url"] not in seen_urls:
                    seen_urls.add(it["url"])
                    items.append(it)
    finally:
        # only kill chrome if this script launched it
        if proc:
            time.sleep(1)
            proc.terminate()

    results = []
    seen = set()
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        price = parse_price(it["price_text"])
        if price < pmin or (pmax is not None and price > pmax):
            continue
        results.append({"price": price, "price_text": it["price_text"],
                        "sold": it["sold"], "name": it["name"], "url": it["url"]})
    results.sort(key=lambda r: r["price"])
    return results[:limit]


def main():
    ap = argparse.ArgumentParser(description="Search Shopee via logged-in CDP Chrome")
    ap.add_argument("keyword")
    ap.add_argument("--pmin", type=int, default=0, help="min price IDR")
    ap.add_argument("--pmax", type=int, default=None, help="max price IDR")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--pages", type=int, default=1, help="number of result pages to walk")
    args = ap.parse_args()

    results = search(args.keyword, pmin=args.pmin, pmax=args.pmax,
                     limit=args.limit, pages=args.pages)
    print(f"{len(results)} results for '{args.keyword}':")
    for r in results:
        print(f"  {r['price_text']:>15} | sold={r['sold']:<5} | {r['name']}")
        print(f"     {r['url']}")


if __name__ == "__main__":
    main()
