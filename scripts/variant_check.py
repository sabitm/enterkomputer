#!/usr/bin/env python3
"""Extract real per-variant prices from product pages via CDP-attached Chrome.

Tokopedia: capture the PDPMainInfo GraphQL response; all RAM/SSD combos live in
components[name=new_variant_options].data[0].children[].price.
Shopee: capture /api/v4/pdp/get_pc; item.models[] holds per-variant prices
(prices are in units of 100000). A single model means the title config is exact.
"""

import argparse
import json
import sys
import time

import requests
import websocket

PORT = 9226


def attach():
    tl = requests.get(f"http://127.0.0.1:{PORT}/json", timeout=3).json()
    pages = [t for t in tl if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        raise SystemExit("no debuggable page; launch Chrome first")
    return pages[0]["webSocketDebuggerUrl"]


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=120)
        self.mid = 0

    def cmd(self, method, **params):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def wait_responses(self, match, timeout=35):
        """Collect matching response ids; stop early once traffic goes quiet."""
        end = time.time() + timeout
        rids = []
        last_new = None
        while time.time() < end:
            self.ws.settimeout(3)
            try:
                msg = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                if rids and last_new and time.time() - last_new > 5:
                    break
                continue
            except Exception:
                break
            if msg.get("method") == "Network.responseReceived":
                url = msg["params"]["response"].get("url", "")
                if match(url):
                    rids.append(msg["params"]["requestId"])
                    last_new = time.time()
        return rids

    def body_of(self, rid):
        body = self.cmd("Network.getResponseBody", requestId=rid)
        txt = body.get("body", "")
        if body.get("base64Encoded"):
            import base64
            txt = base64.b64decode(txt).decode()
        return txt


def eval_js(cdp, expr):
    return cdp.cmd("Runtime.evaluate", expression=expr, returnByValue=True)["result"].get("value")


def check_tokopedia(cdp, url):
    # prefetch cache can serve a compact form without basicInfo; bust it
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}ck={int(time.time())}"
    cdp.cmd("Page.navigate", url="about:blank")
    time.sleep(1)
    nav = cdp.cmd("Page.navigate", url=url)
    if nav.get("errorText"):
        return {"url": url, "error": nav["errorText"]}
    rids = cdp.wait_responses(lambda u: "PDPMainInfo" in u)
    if not rids:
        return {"url": url, "error": "PDPMainInfo not captured (slow/blocked)"}
    root = None
    basic = {}
    for rid in reversed(rids):
        try:
            d = json.loads(cdp.body_of(rid))
        except Exception:
            continue
        cand = d[0]["data"]["pdpMainInfo"] if isinstance(d, list) else d.get("data", {}).get("pdpMainInfo")
        if not cand:
            continue
        b = (cand.get("data") or {}).get("basicInfo") or {}
        if b.get("name"):
            root, basic = cand, b
            break
    if not root:
        # compact/no-variant payload: fall back to rendered DOM
        time.sleep(6)
        dom = eval_js(cdp, """
          (() => {
            const prices = [...document.querySelectorAll('p,div,span')]
              .filter(e => e.children.length === 0)
              .map(e => e.textContent.trim())
              .filter(t => /^Rp\\d{1,3}(\\.\\d{3})+$/.test(t))
              .map(t => parseInt(t.replace(/\\D/g,'')))
              .filter(p => p >= 100000);
            const optBtns = [...document.querySelectorAll('button,label,[role=button]')]
              .map(b => b.textContent.trim())
              .filter(t => t && t.length < 40 && /\\d+\\s?(GB|TB)/i.test(t));
            return {prices: [...new Set(prices)].sort((a,b)=>a-b),
                    optionPills: [...new Set(optBtns)].slice(0,30)};
          })()
        """) or {}
        return {"url": url, "marketplace": "tokopedia",
                "name": None,
                "dom_prices": dom.get("prices", []),
                "option_pills": dom.get("optionPills", []),
                "note": "no variant payload; DOM read"}
    out = {"url": url, "marketplace": "tokopedia",
           "name": basic.get("name"), "condition": basic.get("condition"),
           "listed_price": basic.get("price", {}).get("value"),
           "combos": []}
    for comp in root.get("components", []):
        if comp.get("name") not in ("new_variant_options", "variant_options"):
            continue
        for group in comp.get("data") or []:
            labels = {}
            for v in group.get("variants") or []:
                for opt in v.get("option") or []:
                    labels[opt.get("productVariantOptionID")] = opt.get("value")
            for ch in group.get("children") or []:
                names = ch.get("optionName") or [labels.get(str(oid), str(oid)) for oid in ch.get("optionID", [])]
                out["combos"].append({
                    "options": names,
                    "price": ch.get("price"),
                    "price_fmt": ch.get("priceFmt"),
                    "sold_out": bool(ch.get("isSoldOut")) if "isSoldOut" in ch else None,
                })
    return out


def check_shopee(cdp, url):
    cdp.cmd("Page.navigate", url="about:blank")
    time.sleep(1)
    cdp.cmd("Page.navigate", url=url)
    rid = cdp.wait_responses(lambda u: "/api/v4/pdp/get_pc" in u or "/api/v4/item/get" in u)
    if not rid:
        return {"url": url, "error": "pdp api not captured"}
    d = json.loads(cdp.body_of(rid[-1] if isinstance(rid, list) else rid))
    item = (d.get("data") or {}).get("item") or d.get("item") or {}
    models = []
    for m in item.get("models") or []:
        models.append({"name": m.get("name"), "price": (m.get("price") or 0) // 100000,
                       "sold": m.get("sold_count")})
    return {"url": url, "marketplace": "shopee",
            "name": item.get("name"),
            "price_range": [(item.get("price_min") or 0) // 100000, (item.get("price_max") or 0) // 100000],
            "has_variants": len(models) > 1,
            "models": models[:40]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="+")
    args = ap.parse_args()
    cdp = CDP(attach())
    cdp.cmd("Page.enable")
    cdp.cmd("Runtime.enable")
    cdp.cmd("Network.enable")
    for u in args.urls:
        try:
            res = check_tokopedia(cdp, u) if "tokopedia" in u else check_shopee(cdp, u)
        except Exception as e:
            res = {"url": u, "error": str(e)[:200]}
        print(json.dumps(res, ensure_ascii=False))
        sys.stdout.flush()
        time.sleep(2)


if __name__ == "__main__":
    main()
