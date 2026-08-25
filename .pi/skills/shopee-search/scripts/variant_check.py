#!/usr/bin/env python3
"""Verify real per-variant prices on Shopee/Tokopedia PDPs via CDP Chrome.

- Shopee: capture /api/v4/pdp/get_pc, read data.item.models[].
  Single model = title config exact. Multi models = advertised price is the
  cheapest variant until proven otherwise.
- Tokopedia: capture PDPMainInfo GraphQL; variant combos live in
  components[name=new_variant_options].data[0].children[].
  ~1 in 3 listings serves a compact binary payload with no parseable
  basicInfo -> fall back to clicking variant pills and reading the
  rendered price.

Usage: python variant_check.py <url> [url...]
"""
import argparse
import json
import os
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
    # CDP Input.* call; XWayland keeps input trusted
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
    def __init__(self):
        # reuse an existing idle tab before spawning a new one; leaving tens of
        # tabs open clutters the user's session
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
            ws_url, timeout=300, origin=f"http://127.0.0.1:{PORT}"
        )
        self.seq = 0
        self.event_handler = None
        # shopee PDP navigations are only safe once any shopee page has loaded;
        # check_shopee sets this after its homepage warm-up pass
        self.warmed = False

    def healthy(self):
        # wedge detector: a stuck renderer hangs even trivial CDP calls while
        # /json endpoints keep answering normally
        old = self.ws.gettimeout()
        try:
            self.ws.settimeout(8)
            self.seq += 1
            my_id = self.seq
            self.ws.send(json.dumps({"id": my_id, "method": "Runtime.evaluate",
                                     "params": {"expression": "1+1", "returnByValue": True}}))
            while True:
                data = json.loads(self.ws.recv())
                if data.get("id") == my_id:
                    return True
        except Exception:
            return False
        finally:
            self.ws.settimeout(old)

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
            raw = self.ws.recv()
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if self.event_handler and "method" in data and data.get("id") is None:
                try:
                    self.event_handler(data)
                except Exception:
                    pass
            if data.get("id") == my_id:
                if "error" in data:
                    raise RuntimeError(str(data["error"]))
                return data.get("result", {})

    def js(self, expr, await_promise=False, timeout_s=None):
        old = self.ws.gettimeout() if timeout_s else None
        if timeout_s:
            self.ws.settimeout(timeout_s)
        try:
            res = self.cmd(
                "Runtime.evaluate",
                expression=expr,
                returnByValue=True,
                awaitPromise=await_promise,
            ).get("result", {})
        finally:
            if old is not None:
                self.ws.settimeout(old)
        if res.get("subtype") == "error":
            raise RuntimeError(res.get("description", "js error"))
        return res.get("value")

    def pump(self, seconds):
        """Read pending CDP events for N seconds, feeding event_handler."""
        old_timeout = self.ws.gettimeout()
        self.ws.settimeout(0.5)
        end = time.time() + seconds
        while time.time() < end:
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            # no bare 'except: break' here: a dead socket must surface as an
            # error upstream, not masquerade as 'no pdp payload captured'
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if self.event_handler and "method" in data:
                try:
                    self.event_handler(data)
                except Exception:
                    pass
        self.ws.settimeout(old_timeout)


def is_shopee(url):
    return "shopee" in url


def check_shopee(tab, url):
    captured = []

    def handler(data):
        if data.get("method") == "Network.responseReceived":
            u = data.get("params", {}).get("response", {}).get("url", "")
            if "/api/v4/pdp/get_pc" in u:
                captured.append(data["params"]["requestId"])

    tab.cmd("Network.enable")
    tab.event_handler = handler
    if not tab.warmed:
        # a cold session whose FIRST shopee navigation is a PDP gets soft-redirected
        # to the homepage (no get_pc ever fires) or wedges the renderer outright.
        # Land on any shopee page first; PDP->PDP hops from there are reliable.
        tab.cmd("Page.bringToFront")
        tab.cmd("Page.navigate", url="https://shopee.co.id/")
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                if tab.js("document.readyState==='complete' && location.hostname==='shopee.co.id'", timeout_s=8):
                    break
            except (websocket.WebSocketTimeoutException, RuntimeError):
                pass
            time.sleep(2)
            if not tab.healthy():
                return {"title": None, "note": "warmup failed (renderer wedged)"}
        else:
            return {"title": None, "note": "warmup failed (homepage never loaded)"}
        time.sleep(4)
        tab.warmed = True
    tab.cmd("Page.navigate", url=url)

    deadline = time.time() + 30
    while time.time() < deadline:
        tab.pump(2)
        for rid in list(captured):
            captured.remove(rid)
            try:
                body = tab.cmd("Network.getResponseBody", requestId=rid)
                txt = body.get("body", "")
                if body.get("base64Encoded"):
                    import base64
                    txt = base64.b64decode(txt).decode()
                j = json.loads(txt)
            except Exception:
                continue
            item = ((j.get("data") or {}).get("item")) or {}
            ms = item.get("models")
            if not ms:
                continue
            variants = [
                {
                    "name": mm.get("name"),
                    "price_idr": round(mm.get("price", 0) / 100000),
                    "sold": mm.get("sold_count", 0),
                }
                for mm in ms
            ]
            tab.event_handler = None
            note = (
                "single model; title config is exact"
                if len(variants) == 1
                else "multiple models; headline price maps to the cheapest"
            )
            return {"title": item.get("name"), "note": note, "variants": variants}
    tab.event_handler = None
    return {"title": None, "note": "no pdp payload captured (login wall or captcha?)"}


def check_tokopedia(tab, url):
    # cache-bust: prefetch cache otherwise serves a stale payload
    cb_url = url + ("&" if "?" in url else "?") + f"ck={int(time.time()*1000)}"
    captured = []

    def handler(data):
        if data.get("method") == "Network.responseReceived":
            u = data.get("params", {}).get("response", {}).get("url", "")
            if "PDPMainInfo" in u or ("graphql" in u.lower() and "pdpmaininfo" in u.lower()):
                captured.append(data["params"]["requestId"])

    tab.cmd("Network.enable")
    tab.event_handler = handler
    tab.cmd("Page.navigate", url=cb_url)
    result = {"title": None, "note": "", "price_displayed": None, "variants": None}

    deadline = time.time() + 16
    while time.time() < deadline:
        tab.pump(2)
        for rid in list(captured):
            captured.remove(rid)
            try:
                body = tab.cmd("Network.getResponseBody", requestId=rid).get("body", "")
                j = json.loads(body)
            except Exception:
                continue
            info = None
            if isinstance(j, list) and j:
                info = (j[0].get("data") or {}).get("pdpMainInfo")
            elif isinstance(j, dict):
                info = (j.get("data") or {}).get("pdpMainInfo") or j.get("pdpMainInfo")
            if not isinstance(info, dict):
                continue
            bi = info.get("basicInfo") or {}
            if not bi.get("name"):
                continue  # empty shell or compact binary payload
            result["title"] = bi.get("name")
            price = bi.get("price") or {}
            result["price_displayed"] = price.get("value")
            variants = []
            for comp in info.get("components") or []:
                comp_data = comp.get("data")
                if not isinstance(comp_data, list):
                    continue
                for group in comp_data:
                    for child in group.get("children") or []:
                        if child.get("priceFmt"):
                            variants.append(
                                {
                                    "name": (child.get("optionName") or "")[:70],
                                    "price_fmt": child.get("priceFmt"),
                                }
                            )
            if variants:
                result["variants"] = variants
                result["note"] = "variant payload"
            else:
                result["note"] = "payload ok, no variant options listed (single config)"
            tab.event_handler = None
            return result

    # compact binary payload case: no parseable GraphQL on any fetch.
    # pill-click fallback on the already-loaded page.
    result["note"] = "no variant payload; pill-click fallback"
    tab.pump(max(0, 14 - 8))
    time.sleep(6)
    tab.js("window.scrollTo(0, document.body.scrollHeight / 3)")
    time.sleep(2)
    pills_js = """
(() => {
  const out = [];
  const els = document.querySelectorAll('button,[role="button"],label');
  els.forEach(el => {
    const t = (el.innerText || '').trim();
    if (t && t.length < 60 && /i5|i7|i9|ryzen|SSD|RAM|\\d+\\s?(GB|TB)|ultra\\s?\\d/i.test(t)) {
      out.push(t);
    }
  });
  return [...new Set(out)];
})()
"""
    pills = tab.js(pills_js) or []
    clicked = []
    for label in pills[:12]:
        safe = json.dumps(label)
        price = tab.js(
            f"""
(async () => {{
  const els = [...document.querySelectorAll('button,[role="button"],label')];
  const el = els.find(e => (e.innerText||'').trim() === {safe});
  if (!el) return null;
  el.click();
  await new Promise(r => setTimeout(r, 2000));
  const p = document.querySelector('[data-testid="lblPDPDetailProductPriceAmount"]');
  return p ? p.innerText : null;
}})()
""",
            await_promise=True,
        )
        if price:
            clicked.append({"pill": label, "displayed_price": price})
    result["variants"] = clicked
    tab.event_handler = None
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="+")
    args = ap.parse_args()

    ensure_chrome()
    tab = Tab()
    try:
        for i, url in enumerate(args.urls):
            if i and not tab.healthy():
                r = {"error": "renderer wedged; kill chrome, relaunch, rerun this url"}
            else:
                try:
                    r = check_shopee(tab, url) if is_shopee(url) else check_tokopedia(tab, url)
                except Exception as e:
                    r = {"error": str(e)}
            print(json.dumps({url: r}, indent=2), flush=True)
            time.sleep(1)
    finally:
        tab.park()


if __name__ == "__main__":
    main()
