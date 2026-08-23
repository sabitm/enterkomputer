#!/usr/bin/env python3
"""Tokopedia per-variant prices via CDP Chrome.

PDPMainInfo GraphQL often arrives as an unparseable compact payload, so we
skip network capture entirely: wait for the buy box ("Subtotal"), read the
displayed price, then click group pills (variant families) and option pills
(spec axes like capacity/memory), re-reading the subtotal after each click.
"""
import json
import sys
import time

import requests
import websocket

PORT = 9226
BASE = f"http://127.0.0.1:{PORT}"


def ensure_chrome():
    try:
        requests.get(BASE + "/json/version", timeout=2)
        return
    except Exception:
        pass
    import subprocess, os
    subprocess.Popen(
        ["google-chrome-stable", f"--remote-debugging-port={PORT}", "--no-first-run",
         "--remote-allow-origins=*",
         f"--user-data-dir={os.path.join(os.getcwd(), 'ignored', 'shopee-profile')}",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(45):
        try:
            requests.get(BASE + "/json/version", timeout=2)
            return
        except Exception:
            time.sleep(1)
    sys.exit("chrome did not come up")


class Tab:
    def __init__(self):
        # reuse an existing idle tab before spawning a new one
        self.tab_id = None
        ws_url = None
        try:
            for t in requests.get(BASE + "/json/list", timeout=5).json():
                if t.get("type") == "page" and t.get("url", "").startswith(("about:blank", "chrome://newtab")):
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
            ws_url, timeout=300, origin=f"http://127.0.0.1:{PORT}")
        self.seq = 0

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
        my = self.seq
        self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        while True:
            data = json.loads(self.ws.recv())
            if data.get("id") == my:
                if "error" in data:
                    raise RuntimeError(str(data["error"]))
                return data.get("result", {})

    def js(self, expr, await_promise=False):
        res = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True,
                       awaitPromise=await_promise).get("result", {})
        if res.get("subtype") == "error":
            raise RuntimeError(res.get("description", "js error"))
        return res.get("value")


PILLS_JS = """
(() => {
  const out = [];
  document.querySelectorAll('button,[role="button"],label').forEach(el => {
    const t = (el.innerText || '').trim();
    if (!t || t.length >= 60 || t.startsWith('Terpilih')) return;
    if (/i[3579]|ryzen|ultra|SSD|RAM|\\d+\\s?(GB|TB)|gen\\s?\\d|G\\d\\b/i.test(t)) out.push(t);
  });
  return [...new Set(out)];
})()
"""

CLICK_PRICE_JS = """
(async (label) => {
  const els = [...document.querySelectorAll('button,[role="button"],label')];
  const el = els.find(e => (e.innerText || '').trim() === label);
  if (!el) return JSON.stringify({price: null, reason: 'pill not found'});
  el.click();
  await new Promise(r => setTimeout(r, 2200));
  const txt = document.body.innerText.replace(/\\n/g, ' ');
  const sub = txt.match(/Subtotal[^R]*Rp\\s*([\\d.,]+)/);
  if (sub) return JSON.stringify({price: sub[1], oos: false});
  // sold-out combos render '-' as subtotal; price still appears further down
  const all = [...txt.matchAll(/Rp\\s*([\\d.,]+)/g)].map(m => m[1]);
  const oos = /Stok varian ini habis|Stok:\\s*Habis/.test(txt);
  return JSON.stringify({price: all.length ? all[all.length - 1] : null, oos});
})
"""


def click_and_read(tab, label):
    raw = tab.js(
        f"(async () => {{ const f = {CLICK_PRICE_JS}; return await f({json.dumps(label)}); }})()",
        await_promise=True,
    )
    try:
        return json.loads(raw)
    except Exception:
        return {"price": None}


def check(tab, url):
    cb = url + ("&" if "?" in url else "?") + f"ck={int(time.time()*1000)}"
    tab.cmd("Page.navigate", url=cb)
    # wait for the buy box to render
    ok = False
    for _ in range(10):
        time.sleep(3)
        if tab.js("/Subtotal/.test(document.body.innerText)"):
            ok = True
            break
    if not ok:
        return {"note": "page never rendered buy box (delisted or blocked?)", "variants": []}

    def read_price():
        return tab.js(
            "(document.body.innerText.replace(/\\n/g,' ').match(/Subtotal[^R]*Rp\\s*([\\d.,]+)/)||[])[1] || null")

    title = tab.js("document.title.slice(0, 90)")
    base_price = read_price()
    result = {"title": title, "base_price": base_price, "variants": []}

    # selected combo shown under 'Terpilih:'
    sel = tab.js(
        "(document.body.innerText.match(/Terpilih:\\s*([^\\n]+)/)||[])[1] || null")
    if sel:
        result["default_selection"] = sel.strip()

    import re
    pills = tab.js(PILLS_JS) or []
    # group pills name the variant family; option pills are spec axes
    # (Tokopedia merges some spec axes into one pill without unit tokens)
    def is_group(p):
        if re.search(r"SSD|RAM|\d+\s?(GB|TB)", p, re.I):
            return False
        return bool(re.search(r"i[3579]|ryzen|ultra|gen\s?\d|G\d\b", p, re.I))
    groups = [p for p in pills if is_group(p)]
    options = [p for p in pills if p not in groups]

    seen_combos = set()
    for g in (groups[:4] or [None]):
        if g:
            r = click_and_read(tab, g)
            if r.get("price"):
                result["variants"].append({"combo": g, "price": r["price"],
                                           "oos": r.get("oos", False)})
                seen_combos.add(g)
        for o in options[:8]:
            combo_key = f"{g or ''}+{o}"
            if combo_key in seen_combos:
                continue
            seen_combos.add(combo_key)
            r = click_and_read(tab, o)
            if r.get("price"):
                result["variants"].append({"combo": f"{g or '-'} | {o}",
                                           "price": r["price"], "oos": r.get("oos", False)})
    return result


def main():
    ensure_chrome()
    tab = Tab()
    tab.cmd("Page.bringToFront")
    try:
        for url in sys.argv[1:]:
            try:
                r = check(tab, url)
            except Exception as e:
                r = {"error": str(e)}
            # one JSON object per line so callers can stream/parse partial results
            print(json.dumps({url: r}), flush=True)
            time.sleep(1)
    finally:
        tab.park()


if __name__ == "__main__":
    main()
