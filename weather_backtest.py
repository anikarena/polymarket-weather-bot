"""
Backtest weather_bot_v3's entry rule on settled temperature markets, at REAL prices.

Prices
------
The bot can only buy at what sellers ask, and old order books are not public.
So a fill here = what takers ACTUALLY paid: the volume-weighted price of real
taker BUY trades of that exact token in the FILL_WINDOW_MIN after the decision
time (data-api.polymarket.com/trades, takerOnly). No such trade = no fill = no
trade. The market's own taker fee (rate * (p * (1 - p)) ** exponent, from its
feeSchedule) is added on top. No midpoints, no chart prices.

Forecasts (Open-Meteo previous-runs API, so each one existed at decision time)
------------------------------------------------------------------------------
  d1 strict      tomorrow's market, hourly from 12:30 UTC the day before until
                 local midnight, model runs >= 48h old: no lookahead.
  d1 optimistic  same with runs >= 24h old: in the Americas up to a few hours
                 of lookahead, so treat it as an upper bound.
  d0             today's market, hourly until the 12:00 UTC close, runs >= 24h
                 old + the station's real routine METARs up to that moment +
                 the nowcast, for several NOWCAST_TAU_H values.
The bot's freshest runs and ensemble shape are not archived, so the model here
is a Gaussian around consensus + bias with the calibrated sigma.

Error model: bias + sigma refit walk-forward; a test day only sees days that
had settled 2 days earlier. Entry rule, sizing and limits are the bot's own.

Also scores model vs market (log loss) on every bucket at decision time: when
the market's own price predicts better, the "edges" are the model's mistakes.

Run:  python weather_backtest.py      (API answers cached in weather_backtest_cache/)
      python weather_backtest.py --cached-only
            no new Polymarket trade requests: only events whose traded
            buckets are all cached already
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from statistics import mean

import requests

import weather_bot_v3 as bot
import weather_calibrate as cal

DAYS_BACK = 46                # oldest settled day used
TRAIN_DAYS = 15               # first days only train the error model
FILL_WINDOW_MIN = 30          # real taker BUYs within this long after a decision count
MIN_BUCKET_VOLUME = 200       # buckets the market barely traded are skipped
NOWCAST_TAUS = (0.0, 3.0, 6.0, 12.0)
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "weather_backtest_cache")
RESULTS_FILE = os.path.join(HERE, "weather_backtest_results.json")
DATA_API = "https://data-api.polymarket.com"
CACHED_ONLY = "--cached-only" in sys.argv

sys.stdout.reconfigure(encoding="utf-8")
os.makedirs(CACHE_DIR, exist_ok=True)


# ----------------------------------------------------------------------------
# HTTP + cache
# ----------------------------------------------------------------------------
def _get(url: str, params: dict, tries: int = 4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=60)
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2.0 * (i + 1))


def _cached(name: str, fetch):
    """Cached API answer; `fetch` None = cache only (missing -> None). A file
    cut short by an interrupted run is fetched again."""
    path = os.path.join(CACHE_DIR, name + ".json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            os.remove(path)
    if fetch is None:
        return None
    data = fetch()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def _utc(day: str, hour: float = 0.0) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() + hour * 3600


def _day_shift(day: str, n: int) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------
# DATA: settled events, forecasts, METARs, real trades
# ----------------------------------------------------------------------------
def settled_events(day: str) -> list[dict]:
    """Every temperature event observed on `day`, each bucket with its tokens,
    fee schedule, traded volume and whether it won."""
    def fetch():
        events: list = []
        for offset in range(0, 2100, 100):
            batch = _get(f"{bot.GAMMA_API}/events",
                         {"closed": "true", "tag_slug": "weather", "limit": 100, "offset": offset,
                          "end_date_min": f"{day}T11:00:00Z", "end_date_max": f"{day}T13:00:00Z"})
            events.extend(batch)
            if len(batch) < 100:
                break
        out = []
        for ev in events:
            buckets = []
            for mk in ev.get("markets") or []:
                title = mk.get("question") or ""
                if not bot._is_temperature(title) or (mk.get("endDate") or "")[:10] != day:
                    continue
                parsed = bot._parse_bucket_range(title)
                city = bot._extract_city(title)
                tokens = bot._parse_token_ids(mk)
                if not parsed or not city or len(tokens) < 2:
                    continue
                yes_id, no_id = bot._order_yes_no(mk, tokens)
                kind, lo, hi, unit = parsed
                rate, exp = bot._fee_schedule(mk)
                buckets.append({"title": title, "kind": kind, "lo": lo, "hi": hi, "unit": unit,
                                "city": city, "yes": yes_id, "no": no_id, "cid": mk.get("conditionId"),
                                "won": bot._yes_price_from_gamma(mk) == 1.0,
                                "volume": float(mk.get("volumeNum") or mk.get("volume") or 0),
                                "fee_rate": rate, "fee_exp": exp,
                                "station": bot._station_from_market(mk, ev)})
            if buckets and sum(b["won"] for b in buckets) == 1:
                b0 = buckets[0]
                out.append({"city": b0["city"], "kind": b0["kind"], "date": day,
                            "station": b0["station"], "buckets": buckets})
        return out
    return _cached(f"events_{day}", fetch)


def forecast_history(key: str, lat: float, lon: float, start: str, end: str) -> dict:
    """Hourly consensus inputs from runs >= 24h old (pd1) and >= 48h old (pd2)."""
    def fetch():
        js = _get(cal.PREVIOUS_RUNS_API,
                  {"latitude": lat, "longitude": lon,
                   "hourly": "temperature_2m_previous_day1,temperature_2m_previous_day2",
                   "models": bot.DET_MODELS, "start_date": start, "end_date": end, "timezone": "auto"})
        hourly = js.get("hourly", {})
        return {"times": hourly.get("time", []), "offset": int(js.get("utc_offset_seconds") or 0),
                "pd1": [v or [] for k, v in hourly.items() if k.startswith("temperature_2m_previous_day1")],
                "pd2": [v or [] for k, v in hourly.items() if k.startswith("temperature_2m_previous_day2")]}
    return _cached(f"fc_{key.replace(':', '_').replace(' ', '_')}_{start}_{end}", fetch)


_slices: dict[tuple[str, str], dict] = {}


def fh_slice(fhs: dict, key: str, day: str) -> dict | None:
    """The day before .. the day after of one station's history (cached), so
    the per-decision maths runs over 72 hours, not 45 days."""
    if (key, day) not in _slices:
        fh = fhs.get(key)
        if fh is None:
            return None
        want = {_day_shift(day, -1), day, _day_shift(day, 1)}
        idx = [i for i, t in enumerate(fh["times"]) if t[:10] in want]
        if not idx:
            return None
        i0, i1 = idx[0], idx[-1] + 1
        _slices[(key, day)] = {"times": fh["times"][i0:i1], "offset": fh["offset"],
                               "pd1": [s[i0:i1] for s in fh["pd1"]], "pd2": [s[i0:i1] for s in fh["pd2"]]}
    return _slices[(key, day)]


def metar_history(stations: list[str], day: str) -> dict[str, list[tuple[float, float]]]:
    """{icao: [(epoch, °C)]} routine METARs from day-1 10:00 to day 12:00 UTC
    (covers the local `day` up to the 12:00 UTC market close in every zone)."""
    out: dict[str, list] = {s: [] for s in stations}
    for i in range(0, len(stations), 10):
        ids = stations[i:i + 10]

        def fetch():
            try:
                return _get(f"{bot.AVWX_API}/metar", {"ids": ",".join(ids), "format": "json",
                                                     "date": f"{day}T12:00:00Z", "hours": 26})
            except Exception:
                return None
        rows = _cached(f"metar_{day}_{'-'.join(ids)}", fetch)
        for ob in rows or []:
            raw = str(ob.get("rawOb") or "")
            t = bot._metar_temp_c(raw) if not raw.startswith("SPECI") else None
            if t is not None and ob.get("icaoId") in out and ob.get("obsTime") is not None:
                out[ob["icaoId"]].append((float(ob["obsTime"]), t))
    return out


def market_trades(cid: str, day: str) -> list[list]:
    """[(ts, asset, side, price, size)] real taker trades from 12:00 UTC the day
    before to the 12:00 UTC close."""
    def fetch():
        rows = _get(f"{DATA_API}/trades", {"market": cid, "takerOnly": "true", "limit": 10000,
                                           "start": int(_utc(day, -12)), "end": int(_utc(day, 12.5))})
        return [[int(t["timestamp"]), str(t["asset"]), t["side"], float(t["price"]), float(t["size"])]
                for t in rows or []]
    return _cached(f"trades_{cid}", None if CACHED_ONLY else fetch)


# ----------------------------------------------------------------------------
# PRICES at a decision time
# ----------------------------------------------------------------------------
def real_fill(b: dict, token: str, t0: float) -> tuple[float, float] | None:
    """(vwap, usdc) of real taker BUYs of `token` in [t0, t0 + window)."""
    i, j = bisect_left(b["ts"], t0), bisect_left(b["ts"], t0 + FILL_WINDOW_MIN * 60)
    buys = [(p, s) for ts, a, side, p, s in b["trades"][i:j] if a == token and side == "BUY"]
    shares = sum(s for _, s in buys)
    if shares <= 0:
        return None
    usdc = sum(p * s for p, s in buys)
    return usdc / shares, usdc


def last_yes_price(b: dict, t0: float) -> float | None:
    """Market's Yes probability just before t0: the last real trade of either
    token within 6 hours."""
    i = bisect_left(b["ts"], t0) - 1
    if i < 0 or b["ts"][i] < t0 - 6 * 3600:
        return None
    _, a, _, p, _ = b["trades"][i]
    return p if a == b["yes"] else 1.0 - p


def fee(b: dict, price: float) -> float:
    return bot.taker_fee_per_share(price, b["fee_rate"], b["fee_exp"])


# ----------------------------------------------------------------------------
# MODEL at a decision time
# ----------------------------------------------------------------------------
class ErrorModel:
    """Walk-forward bias/sigma: a test day only sees days settled 2+ days earlier."""

    def __init__(self):
        self.errs: dict[tuple[str, str, str], list[tuple[str, float, float]]] = {}
        self._memo: dict = {}

    def add(self, key, kind, src, day, err, wvar):
        self.errs.setdefault((key, kind, src), []).append((day, err, wvar))

    def get(self, key, kind, src, day):
        if (key, kind, src, day) not in self._memo:
            self._memo[(key, kind, src, day)] = self._fit(key, kind, src, day)
        return self._memo[(key, kind, src, day)]

    def _fit(self, key, kind, src, day):
        cutoff = _day_shift(day, -2)
        own = [(e, w) for d, e, w in self.errs.get((key, kind, src), []) if d <= cutoff]
        if len(own) >= bot.CAL_MIN_SAMPLES:
            f = cal.fit(own)
            return f["bias"], f["sigma"]
        pooled = [(e, w) for (k, kd, s), rows in self.errs.items() if kd == kind and s == src
                  for d, e, w in rows if d <= cutoff]
        if len(pooled) < bot.CAL_MIN_SAMPLES:
            return None
        f = cal.fit(pooled)
        return f["bias"], f["sigma"]


def as_bucket(b: dict, day: str) -> bot.Bucket:
    return bot.Bucket(b["title"], "", b["yes"], b["no"], 0.0, b["city"], day, b["kind"], b["unit"],
                      b["lo"], b["hi"], station=b["station"], fee_rate=b["fee_rate"], fee_exp=b["fee_exp"])


_d1_memo: dict = {}


def model_d1(fh: dict, src: str, ev: dict, em: ErrorModel) -> bot.Forecast | None:
    """Tomorrow's market: the whole local day from runs of age `src` (memoised;
    the forecast does not change between hourly decisions)."""
    memo = (ev["key"], ev["kind"], ev["date"], src)
    if memo not in _d1_memo:
        _d1_memo[memo] = _model_d1(fh, src, ev, em)
    return _d1_memo[memo]


def _model_d1(fh: dict, src: str, ev: dict, em: ErrorModel) -> bot.Forecast | None:
    e = em.get(ev["key"], ev["kind"], src, ev["date"])
    if e is None:
        return None
    maxes, mins = bot._day_extremes(fh["times"], fh[src], ev["date"])
    det = maxes if ev["kind"] == "highest" else mins
    samples, kernel, mu = bot._calibrated_samples([], det, *e)
    if mu is None:
        return None
    if ev["kind"] == "highest":
        return bot.Forecast(samples_max=samples, sigma_max=kernel, mu_max=mu, lead="d1")
    return bot.Forecast(samples_min=samples, sigma_min=kernel, mu_min=mu, lead="d1")


def model_d0(fh: dict, obs: list[tuple[float, float]], t0: float, tau: float,
             ev: dict, em: ErrorModel) -> bot.Forecast | None:
    """Reports up to t0 + rest of the local day from pd1 runs with the nowcast."""
    e = em.get(ev["key"], ev["kind"], "pd1", ev["date"])
    if e is None:
        return None
    bias, sigma = e
    off = fh["offset"]
    local = lambda ts: datetime.fromtimestamp(ts + off, timezone.utc).replace(tzinfo=None)
    today = [(ts, t) for ts, t in obs if ts <= t0 and local(ts).strftime("%Y-%m-%d") == ev["date"]]
    if not today:
        return None
    now_local = local(t0)
    recent = [(local(ts), t) for ts, t in obs if t0 - bot.NOWCAST_HOURS * 3600 <= ts <= t0] if tau > 0 else []
    bot.NOWCAST_TAU_H = tau if tau > 0 else 1.0
    series = bot._nowcast(fh["times"], fh["pd1"], recent, now_local, bias)
    maxes, mins = bot._day_extremes(fh["times"], series, ev["date"], now_local.strftime("%Y-%m-%dT%H:%M"))
    highest = ev["kind"] == "highest"
    samples, kernel, mu = bot._calibrated_samples([], maxes if highest else mins, 0.0, sigma)
    obs_v = max(t for _, t in today) if highest else min(t for _, t in today)
    mu = obs_v if mu is None else (max(obs_v, mu) if highest else min(obs_v, mu))
    if highest:
        return bot.Forecast(samples_max=samples, sigma_max=kernel, mu_max=mu, obs_max=obs_v, lead="d0")
    return bot.Forecast(samples_min=samples, sigma_min=kernel, mu_min=mu, obs_min=obs_v, lead="d0")


def sanity_ok(ev: dict, fc: bot.Forecast, t0: float) -> bool:
    """The bot's sanity filter with the market favourite read from real trades."""
    mu_c = fc.mu_max if ev["kind"] == "highest" else fc.mu_min
    fav, fav_p = None, -1.0
    for b in ev["buckets"]:
        p = last_yes_price(b, t0) if b["trades"] is not None else None
        if p is not None and p > fav_p:
            fav, fav_p = b, p
    if fav is None or mu_c is None:
        return False
    mid = as_bucket(fav, ev["date"]).mid()
    mu = mu_c * 9 / 5 + 32 if fav["unit"] == "F" else mu_c
    tol = bot.SANITY_TOL_C * 9 / 5 if fav["unit"] == "F" else bot.SANITY_TOL_C
    return mid is not None and abs(mid - mu) <= tol


# ----------------------------------------------------------------------------
# SIMULATION
# ----------------------------------------------------------------------------
def entries_at(ev: dict, fc: bot.Forecast, t0: float, held: set, slots_left: int) -> list[dict]:
    """The bot's decision at t0 for one event, filled at real taker prices."""
    if slots_left <= 0 or not sanity_ok(ev, fc, t0):
        return []
    picks = []
    for b in ev["buckets"]:
        if b["trades"] is None or b["yes"] in held or b["no"] in held:
            continue
        p_yes = bot.bucket_probability(as_bucket(b, ev["date"]), fc)
        if p_yes is None:
            continue
        best = None
        for side, token, p_win in (("Yes", b["yes"], p_yes), ("No", b["no"], 1.0 - p_yes)):
            fill = real_fill(b, token, t0)
            if fill is None or p_win < bot.MIN_MODEL_PROB:
                continue
            vwap, usdc = fill
            cost = vwap + fee(b, vwap)
            if not (bot.MIN_PRICE <= cost <= bot.MAX_PRICE):
                continue
            edge = p_win / cost - 1.0
            if edge >= bot.MIN_EDGE and (best is None or edge > best["ev"]):
                best = {"side": side, "token": token, "p_win": p_win, "vwap": vwap,
                        "cost": cost, "ev": edge, "usdc": usdc}
        if best:
            picks.append((best, b))
    out = []
    for best, b in sorted(picks, key=lambda x: -x[0]["ev"]):
        if len(out) >= slots_left:
            break
        stake = min(bot.kelly_stake(best["p_win"], best["cost"]), best["usdc"])
        if stake < bot.MIN_TRADE_USDC:
            continue
        won = b["won"] if best["side"] == "Yes" else not b["won"]
        shares = stake / best["cost"]
        out.append({"title": b["title"], "date": ev["date"], "lead": fc.lead, "t": t0,
                    "side": best["side"], "p_win": round(best["p_win"], 4), "vwap": round(best["vwap"], 4),
                    "cost": round(best["cost"], 4), "ev": round(best["ev"], 4), "stake": round(stake, 2),
                    "won": won, "pnl": round(shares - stake if won else -stake, 4), "token": best["token"]})
    return out


def run_scenario(name: str, groups: dict, fhs: dict, obs: dict, em: ErrorModel,
                 d1_src: str | None, d0_tau: float | None) -> list[dict]:
    trades_out: list[dict] = []
    for (city, day), evs in sorted(groups.items(), key=lambda kv: kv[0][1]):
        held: set = set()
        taken = 0
        fh = fh_slice(fhs, evs[0]["key"], day)
        if fh is None:
            continue
        midnight = _utc(day) - fh["offset"]          # local 00:00 of `day`, in UTC
        t = _utc(day, -11.5)                          # 12:30 UTC the day before
        while t <= _utc(day, 11.5) and taken < bot.MAX_ENTRIES_PER_CITY_DAY:
            lead = "d1" if t < midnight else "d0"
            for ev in evs:
                if lead == "d1" and d1_src:
                    fc = model_d1(fh, d1_src, ev, em)
                elif lead == "d0" and d0_tau is not None and ev["station"]:
                    fc = model_d0(fh, obs.get((ev["station"], day), []), t, d0_tau, ev, em)
                else:
                    fc = None
                if fc is None:
                    continue
                new = entries_at(ev, fc, t, held, bot.MAX_ENTRIES_PER_CITY_DAY - taken)
                for e in new:
                    held.add(e["token"])
                    e["scenario"] = name
                taken += len(new)
                trades_out += new
            t += 3600
    return trades_out


def log_loss(p: float, y: bool) -> float:
    p = min(max(p, 0.01), 0.99)
    return -math.log(p if y else 1.0 - p)


def score_vs_market(groups: dict, fhs: dict, obs: dict, em: ErrorModel) -> dict:
    """Mean log loss of model vs market Yes price on every bucket that traded,
    at the first d1 decision (12:30 UTC day before) and at 06:30 UTC on the day."""
    out = {}
    for label, hour in (("d1 strict @12:30 UTC day before", -11.5), ("d0 tau=6 @06:30 UTC", 6.5)):
        m_ll, k_ll, n = 0.0, 0.0, 0
        for (city, day), evs in groups.items():
            for ev in evs:
                fh = fh_slice(fhs, ev["key"], day)
                if fh is None:
                    continue
                t0 = _utc(day, hour)
                if hour < 0:
                    if t0 >= _utc(day) - fh["offset"]:
                        continue
                    fc = model_d1(fh, "pd2", ev, em)
                else:
                    fc = model_d0(fh, obs.get((ev["station"], day), []), t0, 6.0, ev, em) if ev["station"] else None
                if fc is None:
                    continue
                for b in ev["buckets"]:
                    if b["trades"] is None:
                        continue
                    pm = last_yes_price(b, t0)
                    pk = bot.bucket_probability(as_bucket(b, day), fc)
                    if pm is None or pk is None:
                        continue
                    m_ll += log_loss(pm, b["won"])
                    k_ll += log_loss(pk, b["won"])
                    n += 1
        out[label] = {"n": n, "market": round(m_ll / n, 4) if n else None, "model": round(k_ll / n, 4) if n else None}
    return out


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return {"n": 0}
    staked = sum(t["stake"] for t in trades)
    pnl = sum(t["pnl"] for t in trades)
    return {"n": n, "wins": sum(t["won"] for t in trades), "staked": round(staked, 2),
            "pnl": round(pnl, 2), "roi_pct": round(100 * pnl / staked, 1),
            "avg_cost": round(mean(t["cost"] for t in trades), 3),
            "avg_model_p": round(mean(t["p_win"] for t in trades), 3)}


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main() -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    days = [_day_shift(today, -i) for i in range(DAYS_BACK, 0, -1)]
    test_days = set(days[TRAIN_DAYS:])
    print(f"days {days[0]} .. {days[-1]}  (test from {min(test_days)})")

    events = []
    for day in days:
        try:
            events += settled_events(day)
        except Exception as e:
            print(f"events {day}: {e}")
    for ev in events:
        ev["key"] = ev["station"] or f"city:{ev['city']}"
    print(f"{len(events)} settled events")

    # forecasts per station
    fhs: dict[str, dict] = {}
    for key in sorted({ev["key"] for ev in events}):
        ev0 = next(ev for ev in events if ev["key"] == key)
        coords = cal.coords_for(ev0["station"], ev0["city"])
        if not coords:
            print(f"{key}: no coordinates")
            continue
        try:
            fhs[key] = forecast_history(key, *coords, _day_shift(days[0], -1), _day_shift(days[-1], 1))
        except Exception as e:
            print(f"{key}: forecast history failed: {e}")
    print(f"forecast history for {len(fhs)} stations")

    # walk-forward error model inputs
    em = ErrorModel()
    for ev in events:
        fh = fhs.get(ev["key"])
        win = next(b for b in ev["buckets"] if b["won"])
        if fh is None or win["lo"] is None or win["hi"] is None:
            continue
        mid, width = (win["lo"] + win["hi"]) / 2.0, win["hi"] - win["lo"] + 1.0
        if win["unit"] == "F":
            mid, width = (mid - 32.0) * 5.0 / 9.0, width * 5.0 / 9.0
        for src in ("pd1", "pd2"):
            maxes, mins = bot._day_extremes(fh["times"], fh[src], ev["date"])
            det = maxes if ev["kind"] == "highest" else mins
            if det:
                em.add(ev["key"], ev["kind"], src, ev["date"], mid - mean(det), width ** 2 / 12.0)

    # real trades for every bucket the market actually traded, test days only
    test_events = [ev for ev in events if ev["date"] in test_days and ev["key"] in fhs]
    jobs = [(b, ev["date"]) for ev in test_events for b in ev["buckets"] if b["volume"] >= MIN_BUCKET_VOLUME]
    print(f"fetching real trades for {len(jobs)} buckets ...")

    def load(job):
        b, day = job
        try:
            rows = market_trades(b["cid"], day)
            b["trades"] = None if rows is None else sorted(rows, key=lambda r: r[0])
            b["ts"] = [r[0] for r in b["trades"] or []]
        except Exception:
            b["trades"] = None
    for ev in test_events:
        for b in ev["buckets"]:
            b["trades"] = None
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, _ in enumerate(pool.map(load, jobs), 1):
            if i % 1000 == 0:
                print(f"  {i}/{len(jobs)}")
    print(f"  trades missing for {sum(1 for b, _ in jobs if b['trades'] is None)} buckets")
    if CACHED_ONLY:
        test_events = [ev for ev in test_events
                       if all(b["trades"] is not None for b in ev["buckets"] if b["volume"] >= MIN_BUCKET_VOLUME)]
        test_days = {ev["date"] for ev in test_events}
        print(f"cached-only: {len(test_events)} complete events on {len(test_days)} days "
              f"({min(test_days)} .. {max(test_days)})")

    # METARs for the d0 decisions
    stations = sorted({ev["station"] for ev in test_events if ev["station"]})
    obs: dict[tuple[str, str], list] = {}
    for day in sorted(test_days):
        for icao, rows in metar_history(stations, day).items():
            obs[(icao, day)] = rows
    d0_days = sorted({d for (s, d), rows in obs.items() if rows})
    print(f"METAR history on {len(d0_days)} test days ({d0_days[0] if d0_days else '-'} ..)")

    groups: dict[tuple[str, str], list] = {}
    for ev in test_events:
        groups.setdefault((ev["city"], ev["date"]), []).append(ev)

    scenarios = [("d1 strict (runs>=48h)", "pd2", None),
                 ("d1 optimistic (runs>=24h)", "pd1", None)]
    scenarios += [(f"d0 only, tau={tau:g}h", None, tau) for tau in NOWCAST_TAUS]
    scenarios += [("full bot (d1 strict + d0 tau=6h)", "pd2", 6.0)]

    results = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
               "days": [min(test_days), max(test_days)], "fill_window_min": FILL_WINDOW_MIN,
               "scenarios": {}, "trades": []}
    print(f"\n{'scenario':<36}{'n':>5}{'wins':>6}{'staked':>9}{'pnl':>9}{'roi%':>7}{'cost':>7}{'model p':>9}")
    for name, src, tau in scenarios:
        trades = run_scenario(name, groups, fhs, obs, em, src, tau)
        s = summarize(trades)
        results["scenarios"][name] = s
        results["trades"] += trades
        if s["n"]:
            print(f"{name:<36}{s['n']:>5}{s['wins']:>6}{s['staked']:>9.2f}{s['pnl']:>+9.2f}"
                  f"{s['roi_pct']:>+7.1f}{s['avg_cost']:>7.3f}{s['avg_model_p']:>9.3f}")
        else:
            print(f"{name:<36}{0:>5}")
        for side in ("Yes", "No"):
            ss = summarize([t for t in trades if t["side"] == side])
            if ss["n"]:
                print(f"   {side:<33}{ss['n']:>5}{ss['wins']:>6}{ss['staked']:>9.2f}{ss['pnl']:>+9.2f}"
                      f"{ss['roi_pct']:>+7.1f}{ss['avg_cost']:>7.3f}{ss['avg_model_p']:>9.3f}")

    results["log_loss"] = score_vs_market(groups, fhs, obs, em)
    print("\nlog loss on every traded bucket (lower = better forecaster)")
    for label, r in results["log_loss"].items():
        print(f"  {label:<34} n={r['n']:<6} market={r['market']}  model={r['model']}")

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)
    print(f"\nwrote {RESULTS_FILE}")


if __name__ == "__main__":
    main()
