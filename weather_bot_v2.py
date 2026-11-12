"""
Weather Market Bot — Polymarket temperature "No-favorite" bot with a real
forecast edge (v2, built from weather_bot_v1.py).

Why this exists
---------------
The old bot bought No at 0.87-0.93 on every bucket. Market price ~= probability,
so at avg entry 0.91 the breakeven win rate is exactly 91% — and the recorded
performance was 91.6% (207W/19L over 4 days): breakeven, no profit.

The edge added here
-------------------
1) FORECAST-DISTANCE FILTER (Open-Meteo, free, no API key):
   Before entering, fetch the forecast MAX (or MIN for "lowest" markets) for the
   market's local day. Only buy No when the forecast sits at least
   FORECAST_MARGIN away from the bucket. Almost every historical loss
   (Wuhan 31C, Jeddah 36C, Shanghai 28C, Seattle 62-63F, ...) was a bucket the
   actual temperature landed ON — the forecast knew hours earlier.
   Fallback: Tomorrow.io (if key present) when Open-Meteo is unreachable.

2) ENTRY-TIME CORRECTION:
   - Scans BOTH today's and tomorrow's markets (each in its tight 11:00-13:00
     UTC endDate bracket) instead of a single hard-switched target date.
   - No blackout hours: the 0.87-0.93 rate band plus the forecast filter decide
     naturally when an entry makes sense.
   - Skips markets with less than MIN_HOURS_LEFT to live.

3) Keeps from the old bot: 1 city = 1 trade per market-day, cooldowns, state
   persistence (own file), resolution tracking, 12h Telegram summary, DRY_RUN.

Run:  python weather_bot_v2.py       (DRY_RUN=True by default — signals only)
Stop: Ctrl+C
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
# CONFIG
# ----------------------------------------------------------------------------
ENTRY_OUTCOME = "No"        # which outcome to buy
RATE_MIN = 0.87             # entry band on the No price
RATE_MAX = 0.93
MIN_HOURS_LEFT = 1.0        # never enter with less than this long to resolution

# Forecast filter — the actual edge.
FORECAST_FILTER = True
FORECAST_MARGIN_C = 2.0     # forecast must be >= this far (deg C) outside the bucket
FORECAST_MARGIN_F = 3.6     # same margin expressed in deg F (2.0C * 9/5)
FORECAST_CACHE_TTL = 3 * 3600   # refresh each city's forecast every 3h
FORECAST_FAIL_TTL = 600         # retry a failed city after 10 min
CITY_BLACKLIST: set[str] = set()   # e.g. {"seattle"} to never trade a city

TEMP_TITLE_RE = re.compile(
    r"\btemperature\b|\btemp\b|\bdegrees\b|\bfahrenheit\b|\bcelsius\b|°", re.IGNORECASE
)
GLOBAL_EXCLUDE_RE = re.compile(
    r'\bglobal\b|\bannual\b|\byearly\b|\bclimate\b', re.IGNORECASE
)

# --- Order / risk parameters ---
DRY_RUN = True              # True = signal/notify only; False = real orders
ORDER_USDC = 1.0            # fixed USDC per entry
MAX_OPEN_POSITIONS = 200
ENTRY_PRICE_BUFFER = 0.01
MAX_ENTRY_PRICE = 0.96
POLL_SECONDS = 30
ENTRY_COOLDOWN_SEC = 3600

# --- Result tracking / summary ---
SUMMARY_EVERY_HOURS = 12
RESOLUTION_GRACE_SEC = 600
RESOLUTION_MAX_WAIT_SEC = 86400
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "weather_bot_v2_state.json")

# --- Notifications ---
NOTIFY_TELEGRAM = True
NOTIFY_GMAIL = False

# --- APIs ---
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
OPEN_METEO_API = "https://api.open-meteo.com/v1/forecast"

# --- Secrets ---
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "") or os.getenv("WALLET_PRIVATE_KEY", "")
POLY_FUNDER = os.getenv("POLY_FUNDER", "") or os.getenv("POLYMARKET_RELAYER_ADDRESS", "")
CHAIN_ID = 137
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO = os.getenv("GMAIL_TO", "") or GMAIL_USER
TOMORROWIO_API_KEY = os.getenv("TOMORROWIO_API_KEY", "")   # fallback source only

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("weathermkt")
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
    end_ts: float
    market_id: str = ""

    def __str__(self) -> str:
        return self.title


# ----------------------------------------------------------------------------
# POLYMARKET: find open temperature markets (today + tomorrow)
# ----------------------------------------------------------------------------
def _is_temperature(title: str) -> bool:
    if GLOBAL_EXCLUDE_RE.search(title or ""):
        return False
    return bool(TEMP_TITLE_RE.search(title or ""))


def _scan_dates() -> list[str]:
    """Dates whose markets we may trade: today and tomorrow (UTC calendar).
    Both are scanned every cycle — the rate band + forecast filter decide
    which entries actually make sense, so no blackout hours are needed."""
    now_utc = datetime.now(timezone.utc)
    return [now_utc.strftime("%Y-%m-%d"),
            (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")]


def find_temperature_markets() -> list[Market]:
    """Every OPEN temperature market resolving today or tomorrow (12:00 UTC).
    Each date is scanned in a tight 11:00-13:00 UTC endDate bracket so crypto
    5-minute markets can't flood the paged results past the API offset cap."""
    now = time.time()
    out: list[Market] = []
    for date_str in _scan_dates():
        target_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_min = (target_dt + timedelta(hours=11)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_max = (target_dt + timedelta(hours=13)).strftime("%Y-%m-%dT%H:%M:%SZ")

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

        for ev in events:
            ev_title = ev.get("title") or ""
            for mk in (ev.get("markets") or []):
                mk_title = mk.get("question") or ev_title
                if not (_is_temperature(mk_title) or _is_temperature(ev_title)):
                    continue
                if mk.get("closed") or mk.get("acceptingOrders") is False:
                    continue
                end_iso = mk.get("endDate")
                if not end_iso or end_iso[:10] != date_str:
                    continue
                end_ts = _iso_to_epoch(end_iso)
                if end_ts <= now + MIN_HOURS_LEFT * 3600:
                    continue    # too late to enter (entry-time guard)
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
    m = _CITY_RE.search(title or "")
    return m.group(1).strip() if m else ""


def _order_yes_no(market: dict, token_ids: list[str]) -> tuple[str, str]:
    outcomes_raw = market.get("outcomes")
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
    except Exception:
        outcomes = None
    if outcomes and len(outcomes) == len(token_ids):
        yes_id = no_id = None
        for name, tid in zip(outcomes, token_ids):
            n = str(name).lower()
            if "yes" in n:
                yes_id = tid
            elif "no" in n:
                no_id = tid
        if yes_id and no_id:
            return yes_id, no_id
    return token_ids[0], token_ids[1]


def _iso_to_epoch(iso: str) -> float:
    iso = iso.replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ----------------------------------------------------------------------------
# FORECAST FILTER (Open-Meteo primary, Tomorrow.io fallback)
# ----------------------------------------------------------------------------
CITY_COORDS: dict[str, tuple[float, float]] = {
    "houston": (29.7604, -95.3698),
    "los angeles": (34.0522, -118.2437),
    "san francisco": (37.7749, -122.4194),
    "seattle": (47.6062, -122.3321),
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
    "busan": (35.1796, 129.0756),
    "buenos aires": (-34.6037, -58.3816),
    "panama city": (8.9824, -79.5199),
    "london": (51.5074, -0.1278),
    "lucknow": (26.8467, 80.9462),
    "wuhan": (30.5928, 114.3055),
    "chengdu": (30.5728, 104.0668),
    "qingdao": (36.0671, 120.3826),
    "guangzhou": (23.1291, 113.2644),
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
    "singapore": (1.3521, 103.8198),
    "manila": (14.5995, 120.9842),
    "wellington": (-41.2866, 174.7756),
    "beijing": (39.9042, 116.4074),
    "atlanta": (33.7490, -84.3880),
    "miami": (25.7617, -80.1918),
    "new york city": (40.7128, -74.0060),
    "nyc": (40.7128, -74.0060),
    "toronto": (43.6532, -79.3832),
    "chongqing": (29.5630, 106.5516),
    "amsterdam": (52.3676, 4.9041),
    "istanbul": (41.0082, 28.9784),
    "jeddah": (21.4858, 39.1925),
    "taipei": (25.0330, 121.5654),
    "dallas": (32.7767, -96.7970),
}

# Bucket parsing: the No side wins when the actual temperature lands OUTSIDE
# the bucket interval. All four Polymarket phrasings are supported:
#   "between 92-93°F"  -> [92, 93]
#   "29°C or below"    -> (-inf, 29]
#   "30°C or higher"   -> [30, +inf)
#   "31°C"             -> [31, 31]
_BUCKET_BETWEEN_RE = re.compile(
    r"between\s+(-?\d+)\s*(?:-|to|and)\s*(-?\d+)\s*°?\s*([CF])", re.IGNORECASE)
_BUCKET_BELOW_RE = re.compile(r"(-?\d+)\s*°\s*([CF])\s+or\s+below", re.IGNORECASE)
_BUCKET_HIGHER_RE = re.compile(r"(-?\d+)\s*°\s*([CF])\s+or\s+(?:higher|above)", re.IGNORECASE)
_BUCKET_VALUE_RE = re.compile(r"(-?\d+)\s*°\s*([CF])", re.IGNORECASE)


def _parse_bucket(title: str) -> Optional[tuple[str, float, float, str]]:
    """Return (kind, lo, hi, unit) or None.
    kind='highest'|'lowest'; [lo, hi] = bucket interval; unit='C'|'F'."""
    t = title or ""
    if re.search(r"\bhighest\b", t, re.IGNORECASE):
        kind = "highest"
    elif re.search(r"\blowest\b", t, re.IGNORECASE):
        kind = "lowest"
    else:
        return None
    m = _BUCKET_BETWEEN_RE.search(t)
    if m:
        return kind, float(m.group(1)), float(m.group(2)), m.group(3).upper()
    m = _BUCKET_BELOW_RE.search(t)
    if m:
        return kind, float("-inf"), float(m.group(1)), m.group(2).upper()
    m = _BUCKET_HIGHER_RE.search(t)
    if m:
        return kind, float(m.group(1)), float("inf"), m.group(2).upper()
    m = _BUCKET_VALUE_RE.search(t)
    if m:
        v = float(m.group(1))
        return kind, v, v, m.group(2).upper()
    return None


# city -> (fetched_at_epoch, {"YYYY-MM-DD": (min_c, max_c)})
_forecast_cache: dict[str, tuple[float, dict]] = {}


def _openmeteo_minmax_c(city_key: str, date_str: str) -> Optional[tuple[float, float]]:
    """Forecast (min_c, max_c) for the city's LOCAL day `date_str`.
    Open-Meteo with timezone=auto returns daily aggregates in local time, which
    matches how Polymarket/wunderground define the day. Free, no API key."""
    coords = CITY_COORDS.get(city_key)
    if not coords:
        return None
    cached = _forecast_cache.get(city_key)
    if cached and time.time() - cached[0] < (FORECAST_CACHE_TTL if cached[1] else FORECAST_FAIL_TTL):
        return cached[1].get(date_str)
    days: dict = {}
    try:
        r = requests.get(
            OPEN_METEO_API,
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
                days[d] = (float(tmin), float(tmax))
    except Exception as e:
        log.warning("Open-Meteo fetch failed for %s: %s", city_key, e)
        days = _tomorrowio_minmax_days(city_key)   # fallback source
    _forecast_cache[city_key] = (time.time(), days)
    return days.get(date_str)


def _tomorrowio_minmax_days(city_key: str) -> dict:
    """Fallback: Tomorrow.io daily min/max keyed by local date. Empty on error."""
    if not TOMORROWIO_API_KEY:
        return {}
    coords = CITY_COORDS.get(city_key)
    if not coords:
        return {}
    days: dict = {}
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
            tmax, tmin = v.get("temperatureMax"), v.get("temperatureMin")
            if not iso or tmax is None or tmin is None:
                continue
            # daily entries start 06:00 local expressed in UTC -> recover local date
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            offset = 6 - dt.hour
            if offset <= -12:
                offset += 24
            elif offset > 14:
                offset -= 24
            local_date = (dt + timedelta(hours=offset)).strftime("%Y-%m-%d")
            days[local_date] = (float(tmin), float(tmax))
    except Exception as e:
        log.warning("Tomorrow.io fallback failed for %s: %s", city_key, e)
    return days


def forecast_filter_ok(title: str, end_ts: float) -> tuple[bool, str]:
    """(ok, reason). ok=True only when the forecast clearly disagrees with the
    bucket — i.e. the No side has a genuine weather-model edge, not just price."""
    if not FORECAST_FILTER:
        return True, "filter off"
    parsed = _parse_bucket(title)
    city = _extract_city(title).lower()
    if not parsed or not city:
        return False, "unparseable title"
    if city in CITY_BLACKLIST:
        return False, "city blacklisted"
    kind, lo, hi, unit = parsed
    date_str = datetime.fromtimestamp(end_ts, timezone.utc).strftime("%Y-%m-%d")
    mm = _openmeteo_minmax_c(city, date_str)
    if mm is None:
        return False, "no forecast"
    fc_c = mm[1] if kind == "highest" else mm[0]     # the value the market measures
    fc = fc_c * 9 / 5 + 32 if unit == "F" else fc_c
    margin_req = FORECAST_MARGIN_F if unit == "F" else FORECAST_MARGIN_C
    if fc < lo:
        dist = lo - fc
    elif fc > hi:
        dist = fc - hi
    else:
        dist = 0.0    # forecast INSIDE the bucket -> No probably loses
    ok = dist >= margin_req
    # human-friendly bucket text: "26°C" / "92-93°F" / "<=22°C" / ">=38°C"
    if lo == hi:
        bucket_txt = f"{lo:g}°{unit}"
    elif lo == float("-inf"):
        bucket_txt = f"<={hi:g}°{unit}"
    elif hi == float("inf"):
        bucket_txt = f">={lo:g}°{unit}"
    else:
        bucket_txt = f"{lo:g}-{hi:g}°{unit}"
    reason = (f"bucket {bucket_txt} | forecast {kind} {fc:.1f}°{unit} | "
              f"gap {dist:.1f}° (need {margin_req:g}+)")
    return ok, reason


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
    """True cost to BUY right now = best ASK. The CLOB /price endpoint names
    sides by the order book: side=buy is the best BID (what buyers offer),
    side=sell is the best ASK (what you actually pay). The old bot queried
    side=buy and therefore recorded entries ~2-3c cheaper than reality,
    overstating every win and understating every loss."""
    try:
        r = requests.get(f"{CLOB_API}/price", params={"token_id": token_id, "side": "sell"}, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        log.debug("buy-price error for %s: %s", token_id, e)
        return get_rate(token_id)


# ----------------------------------------------------------------------------
# NOTIFICATIONS
# ----------------------------------------------------------------------------
def notify(text: str, subject: str = "Weather market bot") -> None:
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


def _send_gmail(text: str, subject: str = "Weather market bot") -> None:
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
    import polymarket
    from polymarket import RelayerApiKey
    kwargs = dict(private_key=POLY_PRIVATE_KEY)
    relayer_key = os.getenv("POLYMARKET_RELAYER_API_KEY", "")
    if relayer_key and POLY_FUNDER:
        kwargs["api_key"] = RelayerApiKey(key=relayer_key, address=POLY_FUNDER)
    client = polymarket.SecureClient.create(**kwargs)
    log.info("Polymarket DEPOSIT WALLET (send USDC here): %s", getattr(client, "wallet", "?"))
    _clob_client = client
    return client


def place_buy(token_id: str, rate: float, usdc: float) -> Optional[float]:
    price = min(round(rate + ENTRY_PRICE_BUFFER, 2), MAX_ENTRY_PRICE)
    size = round(usdc / price, 2)
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
# Result tracking: persistence, resolution lookup, periodic summary
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
    """'win'/'loss' once the market is closed with an extreme outcome price."""
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
        return None
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
        f"Weather Market Bot — {SUMMARY_EVERY_HOURS}h SUMMARY",
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
                     f"{str(en.get('title',''))[:48]} | fc:{en.get('forecast','?')} ({t} UTC)")
    if not entries:
        lines.append("  (no entries taken in this period)")
    body = "\n".join(lines)
    log.warning(">>> SUMMARY | entries=%d wins=%d losses=%d net=$%.2f",
                len(entries), len(wins), len(losses), net)
    notify(body, subject=f"Weather market bot {SUMMARY_EVERY_HOURS}h summary")


# ----------------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------------
def main():
    log.info("Started | DRY_RUN=%s | buy %s @ %.2f-%.2f | forecast filter=%s "
             "(margin %.1fC/%.1fF) | scans today+tomorrow | TG=%s Gmail=%s",
             DRY_RUN, ENTRY_OUTCOME, RATE_MIN, RATE_MAX, FORECAST_FILTER,
             FORECAST_MARGIN_C, FORECAST_MARGIN_F, NOTIFY_TELEGRAM, NOTIFY_GMAIL)
    if not DRY_RUN and not POLY_PRIVATE_KEY:
        log.error("DRY_RUN=False but POLY_PRIVATE_KEY is not set. Stopping.")
        return

    state = _load_state()
    last_entry: dict[str, float] = state.get("last_entry", {})
    positions: dict[str, dict] = state.get("positions", {})
    pending: list = state.get("pending", [])
    period: list = state.get("period", [])
    entries: list = state.get("entries", [])
    report_start: float = state.get("report_start") or time.time()

    def save():
        _save_state({"last_entry": last_entry, "positions": positions,
                     "pending": pending, "period": period, "entries": entries,
                     "report_start": report_start})

    # rebuild city_day_keys so the 1-city rule survives restarts
    for _p in list(positions.values()) + list(pending):
        _city = _extract_city(_p.get("title", ""))
        _end_date = datetime.fromtimestamp(_p["end_ts"], timezone.utc).strftime("%Y-%m-%d")
        _cdk = f"{_city}_{_end_date}" if _city else None
        if _cdk and _cdk not in last_entry:
            last_entry[_cdk] = _p.get("end_ts", time.time()) - 86400
    save()
    log.info("restored: %d open, %d pending, %d entries/%d results this period",
             len(positions), len(pending), len(entries), len(period))

    while True:
        try:
            now = time.time()

            # 1) ended positions -> pending resolution
            for tid in [t for t, p in positions.items() if now >= p["end_ts"]]:
                p = positions.pop(tid)
                pending.append(p)
                log.info("[%s] window ended -> awaiting resolution.", p["title"])
                save()

            # 2) resolve pending
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
                               "entry_time": last_entry.get(p.get("token_id", ""), 0),
                               "forecast": p.get("forecast", "")})
                log.warning(">>> RESULT %s [%s] pnl=$%.2f", result.upper(), p["title"], pnl)
                save()
            pending = still_pending

            # 3) periodic summary
            if now - report_start >= SUMMARY_EVERY_HOURS * 3600:
                send_summary(entries, period, report_start, now)
                period = []
                entries = []
                report_start = now
                save()

            # 4) scan + enter (today & tomorrow; no blackout — filters decide)
            markets = find_temperature_markets()
            if not markets:
                log.info("no open temperature markets right now.")

            for market in markets:
                tid = market.no_token_id if ENTRY_OUTCOME.lower() == "no" else market.yes_token_id

                if tid in positions or any(q.get("token_id") == tid for q in pending):
                    continue
                if now - last_entry.get(tid, 0) < ENTRY_COOLDOWN_SEC:
                    continue

                # 1 city = 1 entry per market-day
                city = _extract_city(market.title)
                end_date_str = datetime.fromtimestamp(market.end_ts, timezone.utc).strftime("%Y-%m-%d")
                city_day_key = f"{city}_{end_date_str}" if city else None
                if city_day_key and last_entry.get(city_day_key, 0) > 0:
                    continue

                if len(positions) >= MAX_OPEN_POSITIONS:
                    continue

                rate = get_buy_price(tid)
                if rate is None:
                    continue
                if not (RATE_MIN <= rate <= RATE_MAX):
                    continue

                # THE EDGE: forecast must clearly disagree with the bucket
                ok, why = forecast_filter_ok(market.title, market.end_ts)
                hours_left = (market.end_ts - time.time()) / 3600.0
                log.info("%s | rate=%.3f | %.1fh left | %s -> %s",
                         market.title[:60], rate, hours_left, why, "ENTER" if ok else "skip")
                if not ok:
                    continue

                bought = place_buy(tid, rate, ORDER_USDC)
                if bought:
                    last_entry[tid] = time.time()
                    if city_day_key:
                        last_entry[city_day_key] = time.time()
                    positions[tid] = {
                        "title": market.title, "end_ts": market.end_ts,
                        "side": ENTRY_OUTCOME, "token_id": tid, "market_id": market.market_id,
                        "entry_rate": rate, "size": bought, "forecast": why,
                    }
                    entries.append({"title": market.title, "side": ENTRY_OUTCOME,
                                    "entry_rate": rate, "size": bought, "time": time.time(),
                                    "forecast": why})
                    save()
                    gain = round((1.0 - rate) * bought, 2)
                    bd_now = (datetime.now(timezone.utc) + timedelta(hours=6)).strftime("%d-%b %H:%M")
                    msg = (f"ENTRY buy {ENTRY_OUTCOME} @ {rate*100:.1f}c (price {rate:.3f})\n"
                           f"{market.title}\n"
                           f"entry time: {bd_now} BD | {hours_left:.1f}h left | {bought:.2f} shares\n"
                           f"forecast: {why}\n"
                           f"max gain: ${gain:.2f} | DRY_RUN={DRY_RUN}")
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
