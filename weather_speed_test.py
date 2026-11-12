"""
Speed test: how fast do Polymarket temperature markets react to a new METAR?

The cleanest signal there is: a routine METAR that sets a new daily high (or
low) and pushes the settled whole-degree value past a bucket. Every bucket
below the new high is now certain to lose, so its No is worth exactly $1.
For each such "dead bucket" this measures, from REAL trades only:
  - did the market already know before the METAR was published? (Yes price
    just before aviationweather.gov received the report)
  - after publication, for how long could No still be had below $0.97, and how
    much money did the takers who grabbed it make (net of the market's fee),
    as seen by a bot reacting LATENCY seconds after publication
Cheap No after publication = a taker BUY of No at p (asks were there), or a
taker SELL of Yes at q (a stale Yes bid: buying No at 1 - q matched it).

Uses only weather_backtest_cache/ (no new Polymarket requests). Trades there
stop at the 12:00 UTC close, so this covers stations whose local day peaks
before that (Asia, Europe); the Americas mostly fall outside.

Run:  python weather_speed_test.py
"""

from __future__ import annotations

import glob
import json
import math
import os
import sys
from bisect import bisect_left
from datetime import datetime, timezone
from statistics import median

import weather_bot_v3 as bot
import weather_backtest as bt

bt.CACHED_ONLY = True
CHEAP_NO = 0.97               # No obtainable at or below this = money on the table
LATENCIES = (0, 30, 60, 120, 300, 900)
RESULTS_FILE = os.path.join(bt.HERE, "weather_speed_test_results.json")


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def settled_value(temp_c: float, unit: str) -> int:
    """The market's rounding: convert, then half up to a whole degree."""
    v = temp_c * 9 / 5 + 32 if unit == "F" else temp_c
    return math.floor(v + 0.5 + 1e-9)


def load_metars() -> dict[str, list[dict]]:
    """{icao: [{obs, receipt, temp}]} routine reports from the cache, deduped."""
    out: dict[str, dict] = {}
    for path in glob.glob(os.path.join(bt.CACHE_DIR, "metar_*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                rows = json.load(f) or []
        except ValueError:
            continue
        for ob in rows:
            raw = str(ob.get("rawOb") or "")
            if raw.startswith("SPECI") or not ob.get("receiptTime") or ob.get("obsTime") is None:
                continue
            t = bot._metar_temp_c(raw)
            if t is None:
                continue
            key = (ob["obsTime"], raw)
            out.setdefault(ob["icaoId"], {})[key] = {"obs": float(ob["obsTime"]),
                                                     "receipt": _epoch(ob["receiptTime"]), "temp": t}
    return {icao: sorted(d.values(), key=lambda r: r["obs"]) for icao, d in out.items()}


def station_offsets() -> dict[str, int]:
    offs = {}
    for path in glob.glob(os.path.join(bt.CACHE_DIR, "fc_*.json")):
        key = os.path.basename(path)[3:].rsplit("_", 2)[0]
        try:
            with open(path, "r", encoding="utf-8") as f:
                offs[key] = int(json.load(f)["offset"])
        except (ValueError, KeyError):
            pass
    return offs


def load_trades(b: dict, day: str) -> bool:
    rows = bt.market_trades(b["cid"], day)
    if rows is None:
        return False
    b["trades"] = sorted(rows, key=lambda r: r[0])
    b["ts"] = [r[0] for r in b["trades"]]
    return True


def yes_price_before(b: dict, t0: float) -> float | None:
    return bt.last_yes_price(b, t0)


def cheap_no_after(b: dict, t0: float) -> list[tuple[float, float, float]]:
    """[(seconds after t0, obtainable No price, shares)] for real trades after
    t0 that left No at or below CHEAP_NO."""
    out = []
    for ts, asset, side, p, size in b["trades"][bisect_left(b["ts"], t0):]:
        if asset == b["no"] and side == "BUY":
            no_p = p
        elif asset == b["yes"] and side == "SELL":
            no_p = 1.0 - p
        else:
            continue
        if no_p <= CHEAP_NO:
            out.append((ts - t0, no_p, size))
    return out


def main() -> None:
    metars = load_metars()
    offsets = station_offsets()
    print(f"routine METARs cached for {len(metars)} stations")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    days = [bt._day_shift(today, -i) for i in range(bt.DAYS_BACK - bt.TRAIN_DAYS, 0, -1)]
    events = [ev for day in days for ev in (bt._cached(f"events_{day}", None) or [])]

    cases = []
    for ev in events:
        icao, day = ev["station"], ev["date"]
        if not icao or icao not in metars or icao not in offsets:
            continue
        off = offsets[icao]
        close = bt._utc(day, 12.0)
        reports = [r for r in metars[icao]
                   if datetime.fromtimestamp(r["obs"] + off, timezone.utc).strftime("%Y-%m-%d") == day]
        if len(reports) < 3:
            continue
        unit = ev["buckets"][0]["unit"]
        highest = ev["kind"] == "highest"
        ext = None
        for r in reports:
            prev = ext
            ext = r["temp"] if ext is None else (max(ext, r["temp"]) if highest else min(ext, r["temp"]))
            if prev is None or r["receipt"] >= close:
                continue
            old_v, new_v = settled_value(prev, unit), settled_value(ext, unit)
            if new_v == old_v:
                continue
            for b in ev["buckets"]:
                if highest:
                    died = b["hi"] is not None and old_v <= b["hi"] < new_v
                else:
                    died = b["lo"] is not None and new_v < b["lo"] <= old_v
                if not died or not load_trades(b, day):
                    continue
                cheap = cheap_no_after(b, r["receipt"])
                cases.append({
                    "station": icao, "date": day, "kind": ev["kind"], "title": b["title"],
                    "settled_no": not b["won"], "obs": r["obs"], "receipt": r["receipt"],
                    "publish_delay_s": r["receipt"] - r["obs"],
                    "yes_before_obs": yes_price_before(b, r["obs"]),
                    "yes_before_receipt": yes_price_before(b, r["receipt"]),
                    "cheap": [(round(dt, 1), round(p, 4), round(s, 2)) for dt, p, s in cheap],
                    "fee_rate": b["fee_rate"], "fee_exp": b["fee_exp"],
                })

    print(f"{len(cases)} dead buckets with real trades "
          f"({sum(c['settled_no'] for c in cases)} settled No as the rules say)")
    cases = [c for c in cases if c["settled_no"]]
    if not cases:
        return

    delays = [c["publish_delay_s"] for c in cases]
    print(f"\nMETAR observed -> published on aviationweather.gov: median {median(delays) / 60:.1f} min")

    known = [c for c in cases if c["yes_before_receipt"] is not None and c["yes_before_receipt"] <= 1 - CHEAP_NO]
    priced = [c for c in cases if c["yes_before_receipt"] is not None]
    print(f"Yes already <= {1 - CHEAP_NO:.2f} just before publication: {len(known)}/{len(priced)} "
          f"(the market knew before the METAR was public)")
    knew_at_obs = [c for c in priced if c["yes_before_obs"] is not None and c["yes_before_obs"] <= 1 - CHEAP_NO]
    print(f"   ... already before the observation time itself: {len(knew_at_obs)}/{len(priced)}")

    open_cases = [c for c in cases if c["cheap"]]
    print(f"\ncheap No (<= {CHEAP_NO}) still traded after publication: {len(open_cases)}/{len(cases)} dead buckets")
    if open_cases:
        firsts = [c["cheap"][0][0] for c in open_cases]
        lasts = [c["cheap"][-1][0] for c in open_cases]
        print(f"   first such trade: median {median(firsts):.0f} s after publication")
        print(f"   last such trade:  median {median(lasts):.0f} s after publication")

    print(f"\nmoney on the table for a bot reacting N seconds after publication")
    print(f"(real takers' profit on cheap No after that moment, net of fee)")
    print(f"{'latency':>9}{'buckets':>9}{'shares':>10}{'profit $':>10}{'avg No':>8}")
    table = []
    for lat in LATENCIES:
        n, shares, profit, cost = 0, 0.0, 0.0, 0.0
        for c in cases:
            got = [(p, s) for dt, p, s in c["cheap"] if dt >= lat]
            if got:
                n += 1
            for p, s in got:
                fee = bot.taker_fee_per_share(p, c["fee_rate"], c["fee_exp"])
                profit += (1.0 - p - fee) * s
                shares += s
                cost += p * s
        avg = cost / shares if shares else 0.0
        table.append({"latency_s": lat, "buckets": n, "shares": round(shares, 1),
                      "profit": round(profit, 2), "avg_no": round(avg, 3)})
        print(f"{lat:>8}s{n:>9}{shares:>10.0f}{profit:>10.2f}{avg:>8.3f}")

    span_days = len({c["date"] for c in cases})
    print(f"\nover {span_days} days, {len({c['station'] for c in cases})} stations")

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                   "cheap_no": CHEAP_NO, "latency_table": table, "cases": cases}, f, indent=1)
    print(f"wrote {RESULTS_FILE}")


if __name__ == "__main__":
    main()
