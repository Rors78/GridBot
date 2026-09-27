"""Read-only Binance.US public probe for the GridBot port audit (2026-09-27).
No keys. Measures, for the bases the Kraken scanner ranked in scan.json: listing
(USD / USDT), 24h quote volume, median-of-3 book spread, and tape density
(share of the last 1440 CLOSED 1m bars with >= 1 trade). Prints the IP's
X-MBX-USED-WEIGHT-1m before and after."""
import json
import statistics
import sys
import time

import requests

API = "https://api.binance.us/api/v3"
S = requests.Session()
S.headers["User-Agent"] = "gridbot-port-audit"
weights = []


def get(path, **params):
    r = S.get(f"{API}/{path}", params=params, timeout=(5, 15))
    w = r.headers.get("X-MBX-USED-WEIGHT-1m")
    weights.append((path, w))
    if r.status_code in (418, 429):
        print(f"STOP: HTTP {r.status_code} on {path}; Retry-After={r.headers.get('Retry-After')}")
        sys.exit(2)
    r.raise_for_status()
    return r.json()


# The Kraken scanner's latest ranking (2026-09-26 14:05 UTC run: 24 bases).
scan = json.load(open(sys.argv[1] if len(sys.argv) > 1 else r"D:\GridBot\scan.json",
                      encoding="utf-8"))
bases = [r["symbol"].split("/")[0] for r in scan["results"]]

get("ping")
print("weight before:", weights[-1][1])
info = get("exchangeInfo")
limits = [(x["rateLimitType"], x["interval"], x["intervalNum"], x["limit"]) for x in info.get("rateLimits", [])]
print("rateLimits:", limits)
trading = {s["symbol"]: s for s in info["symbols"] if s.get("status") == "TRADING"}
q_counts = {}
for s in trading.values():
    q_counts[s["quoteAsset"]] = q_counts.get(s["quoteAsset"], 0) + 1
print("TRADING pairs by quote:", dict(sorted(q_counts.items(), key=lambda kv: -kv[1])[:6]))

want = []
for b in bases:
    for q in ("USD", "USDT"):
        if f"{b}{q}" in trading:
            want.append(f"{b}{q}")

t24 = {t["symbol"]: t for t in get("ticker/24hr", symbols=json.dumps(want, separators=(",", ":")))}
snaps = []
for i in range(3):
    snaps.append({t["symbol"]: t for t in get("ticker/bookTicker")})
    if i < 2:
        time.sleep(15)


def spread_pct(t):
    try:
        bid, ask = float(t["bidPrice"]), float(t["askPrice"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (bid > 0 and ask > 0 and bid < ask):
        return None          # locked / crossed / empty: not a sample
    return (ask - bid) / ((ask + bid) / 2) * 100


server_ms = get("time")["serverTime"]
end_ms = (server_ms // 60000) * 60000          # start of the forming minute: excluded
start_ms = end_ms - 1440 * 60000
rows = []
for sym in want:
    bars = {}
    cursor = start_ms
    while cursor < end_ms:
        page = get("klines", symbol=sym, interval="1m", startTime=cursor,
                   endTime=end_ms - 1, limit=1000)
        if not page:
            break
        for k in page:
            if start_ms <= k[0] < end_ms:
                bars[k[0]] = int(k[8])
        cursor = page[-1][0] + 60000
    active = sum(1 for n in bars.values() if n > 0)
    sp = [x for x in (spread_pct(s.get(sym, {})) for s in snaps) if x is not None]
    t = t24.get(sym, {})
    rows.append({
        "symbol": sym,
        "quote_vol_24h": round(float(t.get("quoteVolume") or 0.0)),
        "trades_24h": int(t.get("count") or 0),
        "spread_med_pct": round(statistics.median(sp), 4) if sp else None,
        "spread_samples": len(sp),
        "active_1m_pct": round(active / 1440 * 100, 1),
        "bars_returned": len(bars),
    })

print("window (UTC):", time.strftime("%Y-%m-%d %H:%M", time.gmtime(start_ms / 1000)),
      "->", time.strftime("%Y-%m-%d %H:%M", time.gmtime(end_ms / 1000)))
print(f"{'symbol':<11}{'qvol24h':>12}{'trades':>8}{'spr%med3':>10}{'act1m%':>8}{'bars':>6}")
for r in sorted(rows, key=lambda r: -r["quote_vol_24h"]):
    print(f"{r['symbol']:<11}{r['quote_vol_24h']:>12,}{r['trades_24h']:>8}"
          f"{(r['spread_med_pct'] if r['spread_med_pct'] is not None else '-'):>10}"
          f"{r['active_1m_pct']:>8}{r['bars_returned']:>6}")
missing = [b for b in bases if f"{b}USD" not in trading and f"{b}USDT" not in trading]
print("not listed (USD or USDT):", missing)
print("weight after:", weights[-1][1], "| calls:", len(weights))
json.dump({"rows": rows, "missing": missing, "limits": limits, "quotes": q_counts,
           "window": [start_ms, end_ms], "weights": weights},
          open(__file__.replace(".py", "_out.json"), "w"), indent=1)
