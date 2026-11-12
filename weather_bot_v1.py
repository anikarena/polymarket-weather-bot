"""
Polymarket weather (temperature) "favorite No" bot.

What it does
------------
- Scans Polymarket (Gamma /events) for TEMPERATURE markets (e.g. "Highest
  temperature in NYC today", "Will the high temp in ... be above X?").
- Considers every OPEN temperature market resolving within MAX_HOURS_AHEAD
  (full-time: no 5-6h window — enter any time while the market is still open).
- For each such market it reads the "No" outcome's buy price (rate, 0..1).
- If that No-rate is inside the entry band (default 0.87-0.93) it BUYS the No
  token and HOLDS it to resolution. A 0.87-0.93 favorite that resolves correctly
  pays out at 1.0, so the edge is the (1 - entry) gap.
- EXTRA FILTER: the Tomorrow.io daily forecast average must also confirm the
  entry — highest-temp buckets need threshold <= forecast avg, lowest-temp
  buckets need threshold >= forecast avg. No forecast -> no entry.
- On every entry it sends a message to Telegram and/or Gmail.
- If DRY_RUN=True it only signals/notifies; if False it places a real order via
  the Polymarket SDK.
- Runs continuously, 24/7. Stop it with Ctrl+C.

Warnings
--------
- This can trade with real money. Run with DRY_RUN=True for a while first.
- "No" favorites are NOT free money: ~5-10% of the time they lose and you lose
  the whole stake. Only the long-run average matters. Size accordingly.
- Use at your own risk. The author takes no responsibility for any loss/gain.
"""

from __future__ import annotations

import json
import os
import re
import time
import smtplib
import logging
from email.mime.text import MIMEText
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests


# ----------------------------------------------------------------------------
# Load a local .env file (so secrets live in .env, not in this script)
# ----------------------------------------------------------------------------
def _load_dotenv() -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


_load_dotenv()


# ----------------------------------------------------------------------------
# CONFIG — change everything here
# ----------------------------------------------------------------------------

# --- Which markets / when to enter ---
ENTRY_OUTCOME = "No"        # which outcome to buy: "No" or "Yes"
MAX_HOURS_AHEAD = 48        # scan every open temperature market resolving within this many
                            # hours (full-time: no 5-6h window — enter any time it's open)
RATE_MIN = 0.87             # buy only when the chosen outcome's price is >= this (0..1)
RATE_MAX = 0.93             # ...and <= this

# Temperature-market detection (matched against the event/market title).
# Word-boundary regex so "attempt"/"temporary" do NOT match "temp".
TEMP_TITLE_RE = re.compile(
    r"\btemperature\b|\btemp\b|\bdegrees\b|\bfahrenheit\b|\bcelsius\b|°", re.IGNORECASE
)

# --- Order / risk parameters ---
DRY_RUN = True              # True = signal/notify only; False = place real orders
ORDER_USDC = 1.0            # fixed USDC to spend per entry (shares = usdc / price)
MAX_OPEN_POSITIONS = 200    # max simultaneous open positions (high = take ALL cities/buckets
                            # in band; ~$1 each in real mode, so 200 = up to ~$200 exposure)
ENTRY_PRICE_BUFFER = 0.01   # pay this much above the ask to fill fast (keep small to preserve edge)
MAX_ENTRY_PRICE = 0.96      # never buy a token above this price (slippage / edge guard)

POLL_SECONDS = 30           # how often to rescan (5-6h windows don't need fast polling)
ENTRY_COOLDOWN_SEC = 3600   # don't re-enter the same market within this long

# --- Tomorrow.io forecast confirmation filter ---
# Buying "No" on a bucket is only allowed when the Tomorrow.io daily forecast
# AVERAGE temperature confirms the bucket is unlikely to hit:
#   highest-temp market: bucket threshold <= forecast avg  (real high will be above avg)
#   lowest-temp market:  bucket threshold >= forecast avg  (real low will be below avg)
# If the forecast can't be fetched/parsed the entry is SKIPPED (conservative).
TOMORROW_FILTER = True
TOMORROWIO_API_KEY = os.getenv("TOMORROWIO_API_KEY", "")
TOMORROW_CACHE_TTL = 6 * 3600   # cache each city's forecast this long (free tier: 25 req/h, 500/day)
TOMORROW_FAIL_TTL = 600         # after an API error, don't retry that city for this long

# --- Market-vs-forecast AGREEMENT filter ---
# The bucket holding the HIGHEST Yes price is the market's own prediction of the
# actual temperature. Only trade a city when that favorite bucket agrees with
# the Open-Meteo forecast high/low within the tolerance below. If they disagree
# (e.g. forecast high 38°C but the market's favorite bucket is 32°C) the whole
# city is skipped for that day — one of the two sources is wrong, so no bet.
MARKET_AGREE_FILTER = True
MARKET_AGREE_TOL_C = 1.5    # max |market favorite - forecast| for °C markets
MARKET_AGREE_TOL_F = 2.7    # same for °F markets (1.5°C * 9/5)

# Relax the average-temperature entry rule by this many °C (converted for °F):
# highest-temp buckets pass when threshold <= avg + buffer,
# lowest-temp buckets pass when threshold >= avg - buffer.
AVG_BUFFER_C = 1.0

# --- Result tracking / daily summary ---
SUMMARY_EVERY_HOURS = 12    # send a profit + entry summary email this often
RESOLUTION_GRACE_SEC = 600  # wait this long after a market ends before checking who won
                            # (the UMA oracle needs a little time to settle the result)
RESOLUTION_MAX_WAIT_SEC = 86400  # give up waiting for a result after this long (1 day)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather_bot_state.json")

# --- Notifications ---
NOTIFY_TELEGRAM = True      # send a Telegram message on each signal
NOTIFY_GMAIL = False        # send a Gmail email on each signal

# --- API endpoints ---
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# --- Secrets (read from environment variables; do NOT hardcode here) ---
# Wallet / CLOB (only needed when DRY_RUN=False)
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "") or os.getenv("WALLET_PRIVATE_KEY", "")  # Polygon wallet private key
POLY_FUNDER = os.getenv("POLY_FUNDER", "") or os.getenv("POLYMARKET_RELAYER_ADDRESS", "")  # Polymarket wallet address (maker/funder)
CHAIN_ID = 137                                          # Polygon
# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Gmail (use an App Password, not your normal password)
GMAIL_USER = os.getenv("GMAIL_USER", "")               # your gmail address (sender)
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO = os.getenv("GMAIL_TO", "") or GMAIL_USER     # recipient (defaults to sender)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("polyweather")
# quiet very noisy third-party loggers (httpx logs every RPC/HTTP call)
for _noisy in ("httpx", "httpcore", "polymarket", "web3", "websocket"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# ----------------------------------------------------------------------------
# DATA STRUCTURES
# ----------------------------------------------------------------------------
@dataclass
class Market:
    title: str
    yes_token_id: str
    no_token_id: str
    end_ts: float            # market close / resolution (epoch sec)
    market_id: str = ""      # Gamma market id (used later to look up who won)

    def __str__(self) -> str:
        return self.title


# ----------------------------------------------------------------------------
# POLYMARKET: find live TEMPERATURE markets with 5-6 hours left
# ----------------------------------------------------------------------------
GLOBAL_EXCLUDE_RE = re.compile(
    r'\bglobal\b|\bannual\b|\byearly\b|\bclimate\b|\b2024\b|\b2025\b|\b2026\b.*\bby\s+\d+',
    re.IGNORECASE
)

def _is_temperature(title: str) -> bool:
    if GLOBAL_EXCLUDE_RE.search(title or ""):
        return False  # skip global/annual climate markets
    return bool(TEMP_TITLE_RE.search(title or ""))


def _target_trade_date() -> str:
    """Return YYYY-MM-DD of the day whose markets we should trade right now.
    Markets close at 12:00 UTC (18:00 BD) daily.
    Before 12:00 UTC → today's markets. At/after 12:00 UTC → tomorrow's markets."""
    now_utc = datetime.now(timezone.utc)
    if now_utc.hour >= 12:
        return (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")
    return now_utc.strftime("%Y-%m-%d")


def find_temperature_markets() -> list[Market]:
    """Return every OPEN temperature market resolving on the target trade date."""
    now = time.time()
    target_date = _target_trade_date()
    # All temperature markets resolve at exactly 12:00 UTC. Bracket the API window
    # tightly around noon (11:00-13:00 UTC) so the thousands of 5-minute crypto
    # "Up or Down" markets don't flood the results and push temperature markets
    # past the ~2100 offset cap (which truncated them in the morning hours).
    target_dt = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_min = (target_dt + timedelta(hours=11)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_max = (target_dt + timedelta(hours=13)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Gamma caps each response at ~100 events, so page through with offset until a
    # short page comes back (otherwise temperature markets can fall off the end).
    events: list = []
    for offset in range(0, 2100, 100):
        try:
            r = requests.get(
                f"{GAMMA_API}/events",
                params={"closed": "false", "end_date_min": end_min,
                        "end_date_max": end_max, "limit": 100, "offset": offset,
                        "order": "endDate", "ascending": "false"},
                timeout=20,
            )
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            log.warning("Gamma API error: %s", e)
            break
        if not batch:
            break
        events.extend(batch)
        if len(batch) < 100:
            break

    # refresh the market-favorite map (highest-Yes bucket per city/day/kind)
    _refresh_market_favorites(events)

    out: list[Market] = []
    for ev in events:
        ev_title = ev.get("title") or ""
        markets = ev.get("markets") or []
        for mk in markets:
            mk_title = mk.get("question") or ev_title
            # a market is "temperature" if either its own question or the event title says so
            if not (_is_temperature(mk_title) or _is_temperature(ev_title)):
                continue
            if mk.get("closed") or mk.get("acceptingOrders") is False:
                continue

            end_iso = mk.get("endDate")
            if not end_iso:
                continue
            end_ts = _iso_to_epoch(end_iso)
            if end_ts <= now:
                continue
            # only trade markets closing on the target date (today or tomorrow based on 18:00 BD cutoff)
            if end_iso[:10] != target_date:
                continue

            token_ids = _parse_token_ids(mk)
            if len(token_ids) < 2:
                continue
            yes_id, no_id = _order_yes_no(mk, token_ids)

            out.append(Market(title=mk_title, yes_token_id=yes_id,
                              no_token_id=no_id, end_ts=end_ts,
                              market_id=str(mk.get("id") or "")))
    return out


def _parse_token_ids(market: dict) -> list[str]:
    raw = market.get("clobTokenIds")
    if not raw:
        return []
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        return [str(x) for x in ids]
    except Exception:
        return []


_CITY_RE = re.compile(r'\bin\s+(.+?)\s+(?:be\b|on\s)', re.IGNORECASE)

def _extract_city(title: str) -> str:
    """Extract city name from a temperature market title, e.g. 'London' from
    'Will the highest temperature in London be 23°C on June 29?'"""
    m = _CITY_RE.search(title or "")
    return m.group(1).strip() if m else ""


def _order_yes_no(market: dict, token_ids: list[str]) -> tuple[str, str]:
    """Decide which token is Yes and which is No from the outcomes. Returns (yes, no)."""
    outcomes_raw = market.get("outcomes")
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
    except Exception:
        outcomes = None

    if outcomes and len(outcomes) == len(token_ids):
        yes_id = no_id = None
        for name, tid in zip(outcomes, token_ids):
            n = str(name).lower()
            if "yes" in n or "up" in n:
                yes_id = tid
            elif "no" in n or "down" in n:
                no_id = tid
        if yes_id and no_id:
            return yes_id, no_id
    return token_ids[0], token_ids[1]   # fallback: first Yes, second No


def _iso_to_epoch(iso: str) -> float:
    iso = iso.replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ----------------------------------------------------------------------------
# TOMORROW.IO: daily forecast average as an entry confirmation
# ----------------------------------------------------------------------------
# Coordinates for every city Polymarket currently runs temperature markets on.
# A city missing here simply fails the filter (entry skipped) until added.
CITY_COORDS: dict[str, tuple[float, float]] = {
    "houston": (29.7604, -95.3698),
    "los angeles": (34.0522, -118.2437),
    "san francisco": (37.7749, -122.4194),
    "denver": (39.7392, -104.9903),
    "sao paulo": (-23.5505, -46.6333),
    "cape town": (-33.9249, 18.4241),
    "karachi": (24.8607, 67.0011),
    "austin": (30.2672, -97.7431),
    "mexico city": (19.4326, -99.1332),
    "tokyo": (35.6762, 139.6503),
    "hong kong": (22.3193, 114.1694),
    "shanghai": (31.2304, 121.4737),
    "paris": (48.8566, 2.3522),
    "chicago": (41.8781, -87.6298),
    "seoul": (37.5665, 126.9780),
    "buenos aires": (-34.6037, -58.3816),
    "panama city": (8.9824, -79.5199),
    "london": (51.5074, -0.1278),
    "lucknow": (26.8467, 80.9462),
    "wuhan": (30.5928, 114.3055),
    "chengdu": (30.5728, 104.0668),
    "milan": (45.4642, 9.1900),
    "munich": (48.1351, 11.5820),
    "tel aviv": (32.0853, 34.7818),
    "shenzhen": (22.5431, 114.0579),
    "helsinki": (60.1699, 24.9384),
    "madrid": (40.4168, -3.7038),
    "moscow": (55.7558, 37.6173),
    "ankara": (39.9334, 32.8597),
    "warsaw": (52.2297, 21.0122),
    "kuala lumpur": (3.1390, 101.6869),
    "wellington": (-41.2866, 174.7756),
    "beijing": (39.9042, 116.4074),
    "atlanta": (33.7490, -84.3880),
    "miami": (25.7617, -80.1918),
    "new york city": (40.7128, -74.0060),
    "nyc": (40.7128, -74.0060),
    "chongqing": (29.5630, 106.5516),
    "amsterdam": (52.3676, 4.9041),
    "istanbul": (41.0082, 28.9784),
    "busan": (35.1796, 129.0756),
    "jeddah": (21.4858, 39.1925),
    "taipei": (25.0330, 121.5654),
    "dallas": (32.7767, -96.7970),
    "seattle": (47.6062, -122.3321),
    "qingdao": (36.0671, 120.3826),
    "guangzhou": (23.1291, 113.2644),
    "singapore": (1.3521, 103.8198),
    "manila": (14.5995, 120.9842),
    "toronto": (43.6532, -79.3832),
}

# "between 82-83°F" -> lower bound 82; "81°F or below" / "100°F or higher" /
# "be 17°C" -> that single number. Unit letter (C/F) is captured for conversion.
_BUCKET_BETWEEN_RE = re.compile(r"between\s+(-?\d+)\s*(?:-|to|and)\s*(-?\d+)\s*°?\s*([CF])", re.IGNORECASE)
_BUCKET_VALUE_RE = re.compile(r"(-?\d+)\s*°\s*([CF])", re.IGNORECASE)

# city -> (fetched_at_epoch, {local_date "YYYY-MM-DD": avg_temp_celsius})
_tomorrow_cache: dict[str, tuple[float, dict]] = {}


def _parse_temp_bucket(title: str) -> Optional[tuple[str, float, str]]:
    """Return (kind, threshold, unit) from a market title, or None.
    kind='highest'|'lowest', threshold=lower bound of the bucket, unit='C'|'F'."""
    t = title or ""
    if re.search(r"\bhighest\b", t, re.IGNORECASE):
        kind = "highest"
    elif re.search(r"\blowest\b", t, re.IGNORECASE):
        kind = "lowest"
    else:
        return None
    m = _BUCKET_BETWEEN_RE.search(t)
    if m:
        return kind, float(m.group(1)), m.group(3).upper()
    m = _BUCKET_VALUE_RE.search(t)
    if m:
        return kind, float(m.group(1)), m.group(2).upper()
    return None


def _forecast_days(city: str) -> dict:
    """{local_date: {"min": c, "max": c, "avg": c}} for `city`.
    PRIMARY source: Open-Meteo (free, no API key, no rate limit).
    FALLBACK: Tomorrow.io when Open-Meteo fails. Cached per city."""
    key = city.lower().strip()
    coords = CITY_COORDS.get(key)
    if not coords:
        return {}
    cached = _tomorrow_cache.get(key)
    if cached and time.time() - cached[0] < (TOMORROW_CACHE_TTL if cached[1] else TOMORROW_FAIL_TTL):
        return cached[1]
    days: dict = {}
    # --- primary: Open-Meteo (timezone=auto -> daily rows are LOCAL dates) ---
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": coords[0], "longitude": coords[1],
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "timezone": "auto", "forecast_days": 4},
            timeout=20,
        )
        r.raise_for_status()
        daily = r.json().get("daily", {})
        for i, d in enumerate(daily.get("time", [])):
            tmax = daily.get("temperature_2m_max", [None] * 99)[i]
            tmin = daily.get("temperature_2m_min", [None] * 99)[i]
            if tmax is not None and tmin is not None:
                days[d] = {"min": float(tmin), "max": float(tmax),
                           "avg": (float(tmin) + float(tmax)) / 2.0}
    except Exception as e:
        log.warning("Open-Meteo fetch failed for %s: %s", city, e)
    # --- fallback: Tomorrow.io ---
    if not days and TOMORROWIO_API_KEY:
        try:
            r = requests.get(
                "https://api.tomorrow.io/v4/weather/forecast",
                params={"location": f"{coords[0]},{coords[1]}",
                        "timesteps": "daily", "apikey": TOMORROWIO_API_KEY},
                timeout=20,
            )
            r.raise_for_status()
            for d in (r.json().get("timelines", {}).get("daily") or []):
                iso = d.get("time", "")
                v = d.get("values") or {}
                avg, tmin, tmax = v.get("temperatureAvg"), v.get("temperatureMin"), v.get("temperatureMax")
                if not iso or avg is None:
                    continue
                # Daily entries start at 06:00 LOCAL time expressed in UTC (e.g. Tokyo
                # "2026-07-02T21:00:00Z" is local July 3). Recover the local date from
                # the UTC hour: local 6am = utc H  =>  offset = 6 - H (wrapped).
                dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                offset = 6 - dt.hour
                if offset <= -12:
                    offset += 24
                elif offset > 14:
                    offset -= 24
                local_date = (dt + timedelta(hours=offset)).strftime("%Y-%m-%d")
                days[local_date] = {"min": float(tmin) if tmin is not None else float(avg),
                                    "max": float(tmax) if tmax is not None else float(avg),
                                    "avg": float(avg)}
        except Exception as e:
            log.warning("Tomorrow.io fallback failed for %s: %s", city, e)
    _tomorrow_cache[key] = (time.time(), days)
    return days


def _tomorrow_daily_avg_c(city: str, date_str: str) -> Optional[float]:
    d = _forecast_days(city).get(date_str)
    return d["avg"] if d else None


def _forecast_high_low_c(city: str, date_str: str) -> Optional[tuple[float, float]]:
    """(min_c, max_c) forecast for the city's local date, or None."""
    d = _forecast_days(city).get(date_str)
    return (d["min"], d["max"]) if d else None


# ----------------------------------------------------------------------------
# MARKET-vs-FORECAST AGREEMENT: the market's favorite bucket must match forecast
# ----------------------------------------------------------------------------
# (city, "YYYY-MM-DD", kind) -> (bucket_temp, yes_price, unit); refreshed each scan
_market_favorite: dict = {}


def _bucket_mid(title: str) -> Optional[float]:
    """Representative temperature of a bucket: midpoint for 'between A-B',
    the single number otherwise ('31°C', '29°C or below', '38°C or higher')."""
    m = _BUCKET_BETWEEN_RE.search(title or "")
    if m:
        return (float(m.group(1)) + float(m.group(2))) / 2.0
    m = _BUCKET_VALUE_RE.search(title or "")
    if m:
        return float(m.group(1))
    return None


def _yes_price_from_gamma(mk: dict) -> Optional[float]:
    """Live Yes price from a Gamma /events market row, or None."""
    outcomes, prices = mk.get("outcomes"), mk.get("outcomePrices")
    try:
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        prices = json.loads(prices) if isinstance(prices, str) else prices
        for name, p in zip(outcomes or [], prices or []):
            if "yes" in str(name).lower():
                return float(p)
    except Exception:
        pass
    return None


def _refresh_market_favorites(events: list) -> None:
    """Rebuild the favorite-bucket map from a fresh Gamma /events scan."""
    global _market_favorite
    fav: dict = {}
    for ev in events:
        ev_title = ev.get("title") or ""
        for mk in (ev.get("markets") or []):
            q = mk.get("question") or ev_title
            parsed = _parse_temp_bucket(q)
            city = _extract_city(q).lower()
            end_iso = mk.get("endDate") or ""
            if not parsed or not city or not end_iso:
                continue
            kind, _, unit = parsed
            mid = _bucket_mid(q)
            yes_p = _yes_price_from_gamma(mk)
            if mid is None or yes_p is None:
                continue
            key = (city, end_iso[:10], kind)
            if key not in fav or yes_p > fav[key][1]:
                fav[key] = (mid, yes_p, unit)
    _market_favorite = fav


def market_agreement_ok(title: str, end_ts: float) -> bool:
    """True when the market's favorite bucket sits within tolerance of the
    Open-Meteo forecast high (or low for 'lowest' markets). Skip otherwise."""
    if not MARKET_AGREE_FILTER:
        return True
    parsed = _parse_temp_bucket(title)
    city = _extract_city(title).lower()
    if not parsed or not city:
        return False
    kind, _, _ = parsed
    date_str = datetime.fromtimestamp(end_ts, timezone.utc).strftime("%Y-%m-%d")
    fav = _market_favorite.get((city, date_str, kind))
    if not fav:
        log.info("MarketAgree %s %s: no favorite bucket found -> skip", city, date_str)
        return False
    fav_val, fav_yes, fav_unit = fav
    hl = _forecast_high_low_c(city, date_str)
    if hl is None:
        log.info("MarketAgree %s %s: no forecast -> skip", city, date_str)
        return False
    fc_c = hl[1] if kind == "highest" else hl[0]
    fc = fc_c * 9 / 5 + 32 if fav_unit == "F" else fc_c
    tol = MARKET_AGREE_TOL_F if fav_unit == "F" else MARKET_AGREE_TOL_C
    gap = abs(fav_val - fc)
    ok = gap <= tol
    log.info("MarketAgree %s %s: market-fav=%.1f°%s(yes=%.2f) vs forecast-%s=%.1f°%s | gap=%.1f tol=%.1f -> %s",
             city, date_str, fav_val, fav_unit, fav_yes, kind, fc, fav_unit, gap, tol,
             "OK" if ok else "SKIP city")
    return ok


def tomorrow_filter_ok(title: str, end_ts: float) -> bool:
    """True when the Tomorrow.io forecast confirms this No-entry, False to skip."""
    if not TOMORROW_FILTER:
        return True
    parsed = _parse_temp_bucket(title)
    city = _extract_city(title)
    if not parsed or not city:
        return False  # can't verify -> don't enter
    kind, threshold, unit = parsed
    date_str = datetime.fromtimestamp(end_ts, timezone.utc).strftime("%Y-%m-%d")
    avg_c = _tomorrow_daily_avg_c(city, date_str)
    if avg_c is None:
        log.info("no Tomorrow.io avg for %s %s -> skip [%s]", city, date_str, title[:60])
        return False
    avg = avg_c * 9 / 5 + 32 if unit == "F" else avg_c
    buf = AVG_BUFFER_C * 9 / 5 if unit == "F" else AVG_BUFFER_C
    ok = (threshold <= avg + buf) if kind == "highest" else (threshold >= avg - buf)
    log.info("Forecast(Open-Meteo) %s %s: avg=%.1f°%s (+/-%.1f buffer) vs threshold=%.0f (%s) -> %s",
             city, date_str, avg, unit, buf, threshold, kind, "OK" if ok else "NO")
    return ok


# ----------------------------------------------------------------------------
# POLYMARKET: token rate
# ----------------------------------------------------------------------------
def get_rate(token_id: str) -> Optional[float]:
    try:
        r = requests.get(f"{CLOB_API}/midpoint", params={"token_id": token_id}, timeout=10)
        r.raise_for_status()
        return float(r.json()["mid"])
    except Exception as e:
        log.debug("midpoint error for %s: %s", token_id, e)
        return None


def get_buy_price(token_id: str) -> Optional[float]:
    """Best ASK = the price you'd actually pay to BUY this token (0..1)."""
    try:
        r = requests.get(f"{CLOB_API}/price", params={"token_id": token_id, "side": "buy"}, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        log.debug("buy-price error for %s: %s", token_id, e)
        return get_rate(token_id)   # fallback to midpoint


# ----------------------------------------------------------------------------
# NOTIFICATIONS: Telegram + Gmail
# ----------------------------------------------------------------------------
def notify(text: str, subject: str = "Polymarket weather signal") -> None:
    if NOTIFY_TELEGRAM:
        _send_telegram(text)
    if NOTIFY_GMAIL:
        _send_gmail(text, subject)


def _send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        log.error("Telegram send failed: %s", e)


def _send_gmail(text: str, subject: str = "Polymarket weather signal") -> None:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        log.warning("Gmail not configured (GMAIL_USER / GMAIL_APP_PASSWORD).")
        return
    try:
        msg = MIMEText(text)
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = GMAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, [GMAIL_TO], msg.as_string())
    except Exception as e:
        log.error("Gmail send failed: %s", e)


# ----------------------------------------------------------------------------
# Place an order (Polymarket SDK) — only when DRY_RUN=False
# ----------------------------------------------------------------------------
_clob_client = None


def _get_clob_client():
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    # Official Polymarket SDK. SecureClient.create() defaults to the signer's
    # deterministic Deposit Wallet and derives API credentials the correct way.
    import polymarket
    from polymarket import RelayerApiKey
    # IMPORTANT: do NOT pass `wallet` — let the SDK derive the real Deposit Wallet.
    kwargs = dict(private_key=POLY_PRIVATE_KEY)
    relayer_key = os.getenv("POLYMARKET_RELAYER_API_KEY", "")
    if relayer_key and POLY_FUNDER:
        # enables gasless (relayed) trading approvals
        kwargs["api_key"] = RelayerApiKey(key=relayer_key, address=POLY_FUNDER)
    client = polymarket.SecureClient.create(**kwargs)
    log.info("Polymarket DEPOSIT WALLET (send USDC here): %s", getattr(client, "wallet", "?"))
    _clob_client = client
    return client


def place_buy(token_id: str, rate: float, usdc: float) -> Optional[float]:
    """Place a marketable BUY for `usdc` worth. Returns shares bought, or None."""
    price = min(round(rate + ENTRY_PRICE_BUFFER, 2), MAX_ENTRY_PRICE)   # slightly above to fill fast
    size = round(usdc / price, 2)                          # shares so that cost ~= usdc
    cost = round(size * price, 2)
    if DRY_RUN:
        log.info("DRY_RUN -> BUY %.2f shares @ %.3f = $%.2f (token %s)", size, price, cost, token_id[:10])
        return size
    try:
        client = _get_clob_client()
        client.place_limit_order(token_id=token_id, price=price, size=size, side="BUY")
        log.info("BUY placed: %.2f shares @ %.3f = $%.2f", size, price, cost)
        return size
    except Exception as e:
        log.error("buy order failed: %s", e)
        return None


# ----------------------------------------------------------------------------
# Result tracking: persistence, resolution lookup, 24h summary
# ----------------------------------------------------------------------------
def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        log.warning("state save failed: %s", e)


def check_resolution(position: dict) -> Optional[str]:
    """Return 'win' or 'loss' for this position, or None if not settled yet.
    A market is only counted once it is CLOSED and its outcome price is at an
    extreme (>=0.99 win / <=0.01 loss) — so a live high price never counts early."""
    mid = position.get("market_id")
    if not mid:
        return None
    try:
        r = requests.get(f"{GAMMA_API}/markets/{mid}", timeout=15)
        r.raise_for_status()
        mk = r.json()
    except Exception:
        return None
    if isinstance(mk, list):
        mk = mk[0] if mk else {}
    if not mk.get("closed"):
        return None  # trading still open / not resolved yet
    outcomes = mk.get("outcomes")
    prices = mk.get("outcomePrices")
    try:
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        prices = json.loads(prices) if isinstance(prices, str) else prices
    except Exception:
        return None
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None
    side = str(position.get("side", "No")).lower()
    for name, p in zip(outcomes, prices):
        n = str(name).lower()
        is_side = ("no" in n) if side == "no" else ("yes" in n)
        if is_side:
            try:
                pf = float(p)
            except Exception:
                return None
            if pf >= 0.99:
                return "win"
            if pf <= 0.01:
                return "loss"
            return None
    return None


def send_summary(entries: list, period: list, start_ts: float, end_ts: float) -> None:
    wins = [r for r in period if r.get("result") == "win"]
    losses = [r for r in period if r.get("result") == "loss"]
    net = round(sum(r.get("pnl", 0.0) for r in period), 2)
    entry_cost = round(sum(en.get("entry_rate", 0) * en.get("size", 0) for en in entries), 2)
    s = datetime.fromtimestamp(start_ts, timezone.utc).strftime("%Y-%m-%d %H:%M")
    e = datetime.fromtimestamp(end_ts, timezone.utc).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"Polymarket weather — {SUMMARY_EVERY_HOURS}h SUMMARY",
        f"{s} -> {e} UTC   (DRY_RUN={DRY_RUN})",
        "",
        "===== PROFIT (resolved trades) =====",
        f"WINS: {len(wins)}   LOSSES: {len(losses)}   NET PnL: ${net:+.2f}",
    ]
    for r in period:
        tag = "WIN " if r.get("result") == "win" else "LOSS"
        lines.append(f"  {tag} ${r.get('pnl', 0.0):+.2f} | {r.get('side')} @ "
                     f"{r.get('entry_rate', 0):.3f} x{r.get('size', 0):.2f} | {str(r.get('title',''))[:55]}")
    if not period:
        lines.append("  (no trades resolved in this period)")
    lines.append("")
    lines.append(f"===== TOTAL ENTRIES: {len(entries)}  (cost ~${entry_cost:.2f}) =====")
    for en in entries:
        t = datetime.fromtimestamp(en.get("time", end_ts), timezone.utc).strftime("%m-%d %H:%M")
        lines.append(f"  {en.get('side')} @ {en.get('entry_rate', 0):.3f} x{en.get('size', 0):.2f} | "
                     f"{str(en.get('title',''))[:55]}  ({t} UTC)")
    if not entries:
        lines.append("  (no entries taken in this period)")
    body = "\n".join(lines)
    log.warning(">>> SUMMARY | entries=%d wins=%d losses=%d net=$%.2f",
                len(entries), len(wins), len(losses), net)
    notify(body, subject=f"Polymarket weather {SUMMARY_EVERY_HOURS}h summary")


# ----------------------------------------------------------------------------
# MAIN LOOP — runs 24/7 until Ctrl+C
# ----------------------------------------------------------------------------
def main():
    log.info("Started | DRY_RUN=%s | TEMPERATURE markets | buy %s @ %.2f-%.2f | full-time (<=%.0fh ahead) | TG=%s Gmail=%s | 24/7 (Ctrl+C to stop)",
             DRY_RUN, ENTRY_OUTCOME, RATE_MIN, RATE_MAX, MAX_HOURS_AHEAD, NOTIFY_TELEGRAM, NOTIFY_GMAIL)
    if not DRY_RUN and not POLY_PRIVATE_KEY:
        log.error("DRY_RUN=False but POLY_PRIVATE_KEY is not set. Stopping.")
        return

    # ---- restore state (survives restarts) ----
    state = _load_state()
    last_entry: dict[str, float] = state.get("last_entry", {})   # token_id -> last entry epoch
    positions: dict[str, dict] = state.get("positions", {})      # token_id -> open position
    pending: list = state.get("pending", [])                     # positions awaiting resolution
    period: list = state.get("period", [])                       # resolved results since last summary
    entries: list = state.get("entries", [])                     # ALL entries taken since last summary
    report_start: float = state.get("report_start") or time.time()

    def save():
        _save_state({"last_entry": last_entry, "positions": positions,
                     "pending": pending, "period": period, "entries": entries,
                     "report_start": report_start})

    # rebuild city_day_keys from existing positions+pending so the 1-city rule
    # works even after a restart (entries placed before restart still block duplicates)
    for _p in list(positions.values()) + list(pending):
        _city = _extract_city(_p.get("title", ""))
        _end_date = datetime.fromtimestamp(_p["end_ts"], timezone.utc).strftime("%Y-%m-%d")
        _cdk = f"{_city}_{_end_date}" if _city else None
        if _cdk and _cdk not in last_entry:
            last_entry[_cdk] = _p.get("end_ts", time.time()) - 86400  # mark as entered
    save()
    log.info("restored: %d open, %d pending, %d entries/%d results this period",
             len(positions), len(pending), len(entries), len(period))

    while True:
        try:
            now = time.time()

            # ---- 1) move ended positions into the pending-resolution queue ----
            for tid in [t for t, p in positions.items() if now >= p["end_ts"]]:
                p = positions.pop(tid)
                pending.append(p)
                log.info("[%s] window ended -> awaiting resolution.", p["title"])
                save()

            # ---- 2) check pending positions for their final win/loss ----
            still_pending = []
            for p in pending:
                if now < p["end_ts"] + RESOLUTION_GRACE_SEC:
                    still_pending.append(p)
                    continue
                result = check_resolution(p)
                if result is None:
                    if now - p["end_ts"] > RESOLUTION_MAX_WAIT_SEC:
                        log.warning("[%s] result unknown after limit -> dropping.", p["title"])
                    else:
                        still_pending.append(p)
                    continue
                size, entry = p.get("size", 0.0), p.get("entry_rate", 0.0)
                pnl = round((1.0 - entry) * size, 4) if result == "win" else round(-entry * size, 4)
                period.append({"title": p["title"], "side": p["side"], "entry_rate": entry,
                               "size": size, "result": result, "pnl": pnl, "resolved_at": now,
                               "token_id": p.get("token_id", ""),
                               "entry_time": last_entry.get(p.get("token_id",""), 0)})
                log.warning(">>> RESULT %s [%s] pnl=$%.2f", result.upper(), p["title"], pnl)
                save()
            pending = still_pending

            # ---- 3) send the periodic profit + entry summary ----
            if now - report_start >= SUMMARY_EVERY_HOURS * 3600:
                send_summary(entries, period, report_start, now)
                period = []
                entries = []
                report_start = now
                save()

            # ---- 4) find markets and take new entries ----
            # Entry window: 18:00 BD (12:00 UTC) → 11:00 BD (05:00 UTC) next day
            # Blackout:     11:00 BD (05:00 UTC) → 18:00 BD (12:00 UTC)
            _utc_hour = datetime.now(timezone.utc).hour
            _entry_allowed = not (5 <= _utc_hour < 12)
            if not _entry_allowed:
                log.info("Entry blackout (BD 11:00-18:00). Monitoring only.")
                markets = []
            else:
                markets = find_temperature_markets()
                if not markets:
                    log.info("no open temperature markets right now.")

            for market in markets:
                tid = market.no_token_id if ENTRY_OUTCOME.lower() == "no" else market.yes_token_id

                if tid in positions or any(q.get("token_id") == tid for q in pending):
                    continue  # already holding / awaiting this market
                if now - last_entry.get(tid, 0) < ENTRY_COOLDOWN_SEC:
                    continue

                # 1 city = 1 entry per market-day (same end-date)
                city = _extract_city(market.title)
                end_date_str = datetime.fromtimestamp(market.end_ts, timezone.utc).strftime("%Y-%m-%d")
                city_day_key = f"{city}_{end_date_str}" if city else None
                if city_day_key and last_entry.get(city_day_key, 0) > 0:
                    continue  # already entered this city today

                if len(positions) >= MAX_OPEN_POSITIONS:
                    continue

                rate = get_buy_price(tid)
                if rate is None:
                    continue
                hours_left = (market.end_ts - time.time()) / 3600.0
                log.info("%s | %s rate=%.3f | %.2fh left", market, ENTRY_OUTCOME, rate, hours_left)

                if not (RATE_MIN <= rate <= RATE_MAX):
                    continue

                # NEW: the market's own favorite bucket (highest Yes price) must
                # agree with the Open-Meteo forecast high/low — otherwise the
                # whole city is skipped for the day (sources disagree = no bet)
                if not market_agreement_ok(market.title, market.end_ts):
                    continue

                # forecast avg must also confirm the entry (checked after the
                # price band so we only spend API calls on candidates)
                if not tomorrow_filter_ok(market.title, market.end_ts):
                    continue

                bought = place_buy(tid, rate, ORDER_USDC)
                if bought:
                    last_entry[tid] = time.time()
                    if city_day_key:
                        last_entry[city_day_key] = time.time()  # block other buckets for this city today
                    positions[tid] = {
                        "title": market.title, "end_ts": market.end_ts,
                        "side": ENTRY_OUTCOME, "token_id": tid, "market_id": market.market_id,
                        "entry_rate": rate, "size": bought,
                    }
                    entries.append({"title": market.title, "side": ENTRY_OUTCOME,
                                    "entry_rate": rate, "size": bought, "time": time.time()})
                    save()
                    gain = round((1.0 - rate) * bought, 2)
                    msg = (f"ENTRY buy {ENTRY_OUTCOME}\n{market.title}\n"
                           f"rate={rate:.3f} | {hours_left:.2f}h left | {bought:.2f} shares\n"
                           f"max gain if it resolves {ENTRY_OUTCOME}: ${gain:.2f} | DRY_RUN={DRY_RUN}")
                    log.warning(">>> %s", msg.replace("\n", " | "))
                    notify(msg)
        except requests.RequestException as e:
            log.warning("network error: %s", e)
        except Exception as e:
            log.exception("unexpected: %s", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by user (Ctrl+C).")
