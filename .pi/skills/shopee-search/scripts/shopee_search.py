#!/usr/bin/env python3
"""Shopee search via CDP-driven Chrome (headed, logged-in profile).

Extracts product anchors matching -i.<shopid>.<itemid>, parses headline
price/name from card text, filters by IDR range. Grid hydration is gated on
scroll input, so we scroll between polls.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

import requests
import websocket

PORT = int(os.environ.get("PORT", "9226"))
BASE = f"http://127.0.0.1:{PORT}"
PROFILE_DIR = os.path.join(os.getcwd(), "ignored", "shopee-profile")


def ensure_chrome():
    try:
        requests.get(BASE + "/json/version", timeout=2)
        return
    except Exception:
        pass
    # stale locks from an unclean kill make a fresh launch hand off and exit silently
    for sfx in ("Lock", "Cookie", "Socket"):
        try:
            os.remove(os.path.join(PROFILE_DIR, f"Singleton{sfx}"))
        except OSError:
            pass
    # --ozone-platform=x11: on Wayland sessions, native-Wayland Chrome hangs every
    # CDP Input.* call (result grid never hydrates); XWayland keeps input trusted
    subprocess.Popen(
        [
            "google-chrome-stable",
            f"--remote-debugging-port={PORT}",
            "--ozone-platform=x11",
            "--no-first-run",
            "--remote-allow-origins=*",
            f"--user-data-dir={PROFILE_DIR}",
            "--restore-last-session=false",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(45):
        try:
            requests.get(BASE + "/json/version", timeout=2)
            return
        except Exception:
            time.sleep(1)
    sys.exit("chrome did not come up on port %d" % PORT)


class Tab:
    def _responsive(self, ws_url):
        # a parked tab can belong to a wedged renderer (all CDP calls hang while
        # /json endpoints stay alive); probe before reusing it
        try:
            ws = websocket.create_connection(ws_url, timeout=8, origin=f"http://127.0.0.1:{PORT}")
            ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": "1+1", "returnByValue": True}}))
            ws.settimeout(8)
            json.loads(ws.recv())
            ws.close()
            return True
        except Exception:
            return False

    def __init__(self):
        # Chrome >=111 requires PUT for /json/new; reuse an idle tab if present
        self.tab_id = None
        ws_url = None
        try:
            for t in requests.get(BASE + "/json/list", timeout=5).json():
                if (t.get("type") == "page"
                        and t.get("url", "").startswith(("about:blank", "chrome://newtab"))
                        and self._responsive(t["webSocketDebuggerUrl"])):
                    ws_url = t["webSocketDebuggerUrl"]
                    self.tab_id = t["id"]
                    break
        except Exception:
            pass
        if not ws_url:
            r = requests.put(BASE + "/json/new?about:blank", timeout=10)
            t = r.json()
            ws_url = t["webSocketDebuggerUrl"]
            self.tab_id = t["id"]
        self.ws = websocket.create_connection(
            ws_url, timeout=180, origin=f"http://127.0.0.1:{PORT}"
        )
        self.seq = 0
        self.event_handler = None

    def park(self):
        # never close the last page target: that exits Chrome entirely.
        # navigate back to about:blank instead so the next run can reuse it
        # via the idle-tab lookup in __init__.
        if self.tab_id:
            try:
                self.cmd("Page.navigate", url="about:blank")
            except Exception:
                pass

    def cmd(self, method, **params):
        self.seq += 1
        my_id = self.seq
        self.ws.send(json.dumps({"id": my_id, "method": method, "params": params}))
        while True:
            data = json.loads(self.ws.recv())
            if data.get("id") == my_id:
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data.get("result", {})
            if self.event_handler and "method" in data:
                try:
                    self.event_handler(data)
                except Exception:
                    pass

    def js(self, expr, await_promise=False):
        res = self.cmd(
            "Runtime.evaluate",
            expression=expr,
            returnByValue=True,
            awaitPromise=await_promise,
        ).get("result", {})
        if res.get("subtype") == "error":
            raise RuntimeError(res.get("description", "js error"))
        return res.get("value")


COLLECT_JS = """
(() => {
  const seen = {};
  document.querySelectorAll('a[href*="-i."]').forEach(a => {
    const m = (a.href || '').match(/-i\\.(\\d+)\\.(\\d+)/);
    if (!m) return;
    const key = m[1] + '.' + m[2];
    if (!seen[key]) {
      seen[key] = {
        shop: m[1], item: m[2],
        href: a.href.split('?')[0],
        text: (a.innerText || '').trim()
      };
    }
  });
  return Object.values(seen);
})()
"""

JUNK_RE = re.compile(r"\s*(Rp[\d.,]+|\d+(\.\d+)?\s?(rb|jt|ribu|juta)|Bergaransi.*|.*Terjual.*)$", re.I)


NUM_RE = re.compile(r"^[\d.,]+$")
DISCOUNT_RE = re.compile(r"^-\d+%$")
SOLD_RE = re.compile(r"\d+[+]?\s?(terjual|RB\+?)", re.I)


def parse_card(card):
    lines = [ln.strip() for ln in card.get("text", "").split("\n") if ln.strip()]
    # price renders as its own 'Rp' line followed by a bare number line
    price = None
    price_idx = None
    for i, ln in enumerate(lines):
        if ln == "Rp" and i + 1 < len(lines) and NUM_RE.match(lines[i + 1]):
            raw = lines[i + 1].replace(".", "").replace(",", "")
            if raw.isdigit():
                price = int(raw)
                price_idx = i
                break
    if price is None:
        return "", None
    name_lines = [
        ln for ln in lines[:price_idx]
        if not DISCOUNT_RE.match(ln)
    ]
    name = name_lines[0] if name_lines else ""
    # strip trailing rating/sold-count noise glued onto the name line
    name = JUNK_RE.sub("", name).strip()
    return name, price


def search_page(tab, keyword, page, pmin, pmax, want, deadline_s=110):
    url = f"https://shopee.co.id/search?keyword={requests.utils.quote(keyword)}&page={page}"
    if pmin > 0 or pmax < 10**12:
        # server-side PRICE_RANGE filter; only in-band cards hydrate.
        # The client-side band check below stays as a safety net because
        # sponsored/ad cards can bypass server filters.
        lo = max(pmin, 0)
        hi = pmax if pmax < 10**12 else 10**13
        opts = requests.utils.quote(
            json.dumps([{"group_name": "PRICE_RANGE",
                         "values": [f"{lo}\u25b6\u25c0{hi}"]}],
                       separators=(",", ":"), ensure_ascii=False),
            safe="")
        url += f"&fe_filter_options={opts}"
    tab.cmd("Page.navigate", url=url)
    # wait for full load BEFORE any input; scrolling mid-load breaks hydration
    load_deadline = time.time() + 30
    while time.time() < load_deadline:
        try:
            if tab.js("document.readyState") == "complete":
                break
        except Exception:
            pass
        time.sleep(1)
    time.sleep(10)  # client-side render settle after readyState flips

    items, seen = [], {}
    start = time.time()
    stale_at_bottom = 0
    # walk the full result list: wheel down to the bottom each cycle;
    # Shopee appends the next batch every time we hit bottom
    while time.time() - start < deadline_s:
        try:
            cards = tab.js(COLLECT_JS) or []
        except Exception:
            cards = []
        before = len(seen)
        for c in cards:
            seen[c["shop"] + "." + c["item"]] = c

        # ease toward the bottom in small steps; a hard jump can skip
        # lazy-mounted cards before they hydrate
        for _ in range(5):
            tab.cmd("Input.dispatchMouseEvent", type="mouseWheel", x=640, y=400,
                    deltaX=0, deltaY=1000)
            time.sleep(0.6)
        # give the next batch time to fetch + mount before judging stale
        time.sleep(5)

        pos = tab.js(
            "({y: window.scrollY, h: document.documentElement.scrollHeight,"
            " ih: window.innerHeight})"
        ) or {}
        at_bottom = pos.get("y", 0) + pos.get("ih", 0) >= pos.get("h", 10**9) - 300
        grew = len(seen) > before
        stale_at_bottom = stale_at_bottom + 1 if (at_bottom and not grew) else 0
        # one search page serves exactly 60 organic items (plus ad cards)
        if len(seen) >= 62 or stale_at_bottom >= 3:
            break
    for c in seen.values():
        name, price = parse_card(c)
        if price is None or not name:
            continue
        if pmin <= price <= pmax:
            items.append({"name": name, "price": price, "url": c["href"]})
        if len(items) >= want:
            break
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--pmin", type=int, default=0)
    ap.add_argument("--pmax", type=int, default=10**12)
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--pages", type=int, default=1)
    args = ap.parse_args()

    ensure_chrome()
    tab = Tab()
    # background tabs never hydrate their result grid
    tab.cmd("Page.bringToFront")
    all_items = []
    try:
        for page in range(args.pages):
            try:
                got = search_page(tab, args.query, page, args.pmin, args.pmax, args.limit)
            except Exception as e:
                print(f"page {page} failed: {e}", file=sys.stderr)
                got = []
            all_items.extend(got)
    finally:
        tab.park()
    # dedupe by url
    out, seen = [], set()
    for it in all_items:
        if it["url"] not in seen:
            seen.add(it["url"])
            out.append(it)
    print(json.dumps(out[: args.limit], indent=2))


if __name__ == "__main__":
    main()
