"""
Calibrate weather_bot_v3's error model from temperature markets that already settled.

For every resolved Polymarket temperature market over the last CAL_DAYS days:
  truth  = the winning bucket (the settled station value, to the bucket's width)
  d1     = consensus (mean of DET_MODELS) daily max/min from the runs ~1 day earlier
  d0     = the same from the latest runs (same-day lead)
  error  = truth - consensus  ->  bias + sigma per station / kind / lead
The bucket width is part of the spread of `truth`, not forecast error, so its
variance (width^2 / 12) is taken out of sigma. Open-ended winners ("X or
higher") only bound the value and are skipped.

Writes weather_calibration.json next to this file; weather_bot_v3.py re-reads it
whenever it changes. Refresh weekly:  python weather_calibrate.py
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from statistics import mean

import requests

import weather_bot_v3 as bot

CAL_DAYS = 45                 # settled days to learn from
BIAS_SHRINK_N = 10            # bias *= n / (n + this): few samples -> bias pulled toward 0
PREVIOUS_RUNS_API = "https://previous-runs-api.open-meteo.com/v1/forecast"
LEADS = {"d0": "temperature_2m_", "d1": "temperature_2m_previous_day1_"}


def settled_buckets(day: str) -> list[dict]:
    """The winning bucket of every temperature event observed on `day`."""
    params = {"closed": "true", "tag_slug": "weather", "limit": 100,
              "end_date_min": f"{day}T11:00:00Z", "end_date_max": f"{day}T13:00:00Z"}
    events: list = []
    for offset in range(0, 2100, 100):
        r = requests.get(f"{bot.GAMMA_API}/events", params={**params, "offset": offset}, timeout=30)
        r.raise_for_status()
        batch = r.json()
        events.extend(batch)
        if len(batch) < 100:
            break
    out = []
    for ev in events:
        for mk in ev.get("markets") or []:
            title = mk.get("question") or ""
            if not bot._is_temperature(title) or bot._yes_price_from_gamma(mk) != 1.0:
                continue
            if (mk.get("endDate") or "")[:10] != day:
                continue
            parsed = bot._parse_bucket_range(title)
            city = bot._extract_city(title)
            if not parsed or not city:
                continue
            kind, lo, hi, unit = parsed
            if lo is None or hi is None:
                continue
            station = bot._station_from_market(mk, ev)
            out.append({"key": station or f"city:{city}", "station": station, "city": city,
                        "date": day, "kind": kind, "lo": lo, "hi": hi, "unit": unit})
    return out


def consensus_by_day(lat: float, lon: float, start: str, end: str) -> dict[str, dict[str, tuple[float, float]]]:
    """{lead: {local_date: (consensus max °C, consensus min °C)}}, computed the
    same way the bot does it (hourly series -> local-day max/min -> model mean)."""
    r = requests.get(
        PREVIOUS_RUNS_API,
        params={"latitude": lat, "longitude": lon,
                "hourly": "temperature_2m,temperature_2m_previous_day1",
                "models": bot.DET_MODELS, "start_date": start, "end_date": end, "timezone": "auto"},
        timeout=60,
    )
    r.raise_for_status()
    hourly = r.json().get("hourly", {})
    times = hourly.get("time", [])
    out: dict[str, dict[str, tuple[float, float]]] = {}
    for lead, prefix in LEADS.items():
        series = [v or [] for k, v in hourly.items()
                  if k.startswith(prefix) and (lead == "d1" or "previous" not in k)]
        for day in sorted({t[:10] for t in times}):
            maxes, mins = bot._day_extremes(times, series, day)
            if maxes:
                out.setdefault(lead, {})[day] = (mean(maxes), mean(mins))
    return out


def coords_for(station: str, city: str) -> tuple[float, float] | None:
    """Station coordinates (a few tries: aviationweather.gov drops connections),
    or the city centre only for markets that name no station."""
    if not station:
        return bot.CITY_COORDS.get(city)
    for _ in range(3):
        coords = bot.station_coords(station)
        if coords:
            return coords
        time.sleep(2)
    return None


def fit(errs: list[tuple[float, float]]) -> dict:
    """bias + sigma from [(truth_mid - consensus, bucket width variance)]."""
    n = len(errs)
    raw_bias = mean(e for e, _ in errs)
    bias = raw_bias * n / (n + BIAS_SHRINK_N)
    var = mean((e - bias) ** 2 for e, _ in errs) - mean(w for _, w in errs)
    sigma = math.sqrt(max(var, bot.SIGMA_FLOOR_C ** 2))
    return {"bias": round(bias, 3), "sigma": round(sigma, 3), "n": n}


def main() -> None:
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(CAL_DAYS, 1, -1)]
    rows: list[dict] = []
    for day in days:
        try:
            rows += settled_buckets(day)
        except Exception as e:
            print(f"gamma {day}: {e}")
    print(f"{len(rows)} settled buckets over {len(days)} days ({days[0]} .. {days[-1]})")

    by_key: dict[str, list[dict]] = {}
    for r in rows:
        by_key.setdefault(r["key"], []).append(r)

    errors: dict[tuple[str, str, str], list[tuple[float, float]]] = {}
    for key, rs in sorted(by_key.items()):
        coords = coords_for(rs[0]["station"], rs[0]["city"])
        if not coords:
            print(f"{key}: no coordinates -> skipped")
            continue
        dates = sorted(r["date"] for r in rs)
        try:
            cons = consensus_by_day(*coords, dates[0], dates[-1])
        except Exception as e:
            print(f"{key}: forecast history failed: {e}")
            continue
        for r in rs:
            mid, width = (r["lo"] + r["hi"]) / 2.0, r["hi"] - r["lo"] + 1.0
            if r["unit"] == "F":
                mid, width = (mid - 32.0) * 5.0 / 9.0, width * 5.0 / 9.0
            for lead, per_day in cons.items():
                c = per_day.get(r["date"])
                if c is None:
                    continue
                fc_val = c[0] if r["kind"] == "highest" else c[1]
                errors.setdefault((key, r["kind"], lead), []).append((mid - fc_val, width ** 2 / 12.0))
        time.sleep(0.2)

    stations: dict[str, dict] = {}
    pooled: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for (key, kind, lead), errs in errors.items():
        stations.setdefault(key, {}).setdefault(kind, {})[lead] = fit(errs)
        pooled.setdefault((kind, lead), []).extend(errs)
    for (kind, lead), errs in pooled.items():
        stations.setdefault("_global", {}).setdefault(kind, {})[lead] = fit(errs)

    with open(bot.CALIBRATION_FILE, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                   "days": CAL_DAYS, "stations": stations}, f, indent=1)

    print(f"\n{'station':<14}{'kind':<9}{'lead':<6}{'n':>4}{'bias':>8}{'sigma':>8}")
    for key in sorted(stations, key=lambda k: (k != "_global", k)):
        for kind in ("highest", "lowest"):
            for lead in ("d1", "d0"):
                e = stations[key].get(kind, {}).get(lead)
                if e:
                    print(f"{key:<14}{kind:<9}{lead:<6}{e['n']:>4}{e['bias']:>+8.2f}{e['sigma']:>8.2f}")
    print(f"\nwrote {bot.CALIBRATION_FILE}")


if __name__ == "__main__":
    main()
