"""
Weather Bot v3 — Polymarket temperature markets, probability-model edition.

Built by combining the best ideas of three open-source bots on top of the
working Polymarket plumbing from weather_bot_v1.py:

1. PolyWeather (yangyuan-zhen)  -> MULTI-MODEL CONSENSUS
   Blend several deterministic weather models (ECMWF, GFS, ICON via Open-Meteo)
   into one consensus high/low, and use the disagreement between models as
   extra uncertainty. A sanity filter skips a city when the model view and the
   market's own favorite bucket are far apart (one of them is wrong -> no bet).

2. suislanchez/polymarket-kalshi-weather-bot -> ENSEMBLE PROBABILITY
   Pull the 31-member GFS ensemble (Open-Meteo ensemble API). Each member is
   one possible "tomorrow", so the members form an empirical probability
   distribution of the day's high/low. Every market bucket gets a real model
   probability P(bucket), not just a yes/no heuristic.

3. hermes_weatherbot (nicolastinkl) -> EV + KELLY EXECUTION
   For each bucket compare model probability vs market ask price on BOTH
   sides:  EV = p_win / price - 1.  Trade only when EV >= MIN_EDGE, and size
   the position with fractional Kelly on a fixed bankroll.

Reused from weather_bot_v1.py: .env loading, Gamma /events scanning
with paging + the 11:00-13:00 UTC endDate bracket, token-id/outcome parsing,
city extraction, CLOB ask prices, Telegram/Gmail notifications, the official
Polymarket SDK order path, state persistence, resolution tracking and the
periodic PnL summary. Runs 24/7 with no time-of-day window (like the repos):
entries are gated purely by EV, scanning every 5 minutes.

How one cycle works
-------------------
scan Gamma -> group temperature buckets per (city, date, highest/lowest)
  -> fetch forecast at the settlement station (ensemble members + multi-model
     consensus, cached 1h) -> centre members on consensus + calibrated bias,
     smooth with a kernel that fills the spread up to the calibrated sigma;
     today's markets combine that with the station's METARs so far
  -> for each bucket: P(bucket) from the distribution; read Yes/No asks
  -> best side by EV; if EV >= MIN_EDGE and prices/limits OK -> Kelly stake
  -> buy (or just notify in DRY_RUN), hold to resolution, track PnL.

v3.1 fixes (2026-10-28, after the July paper run lost 43%)
----------------------------------------------------------
1. STATION, NOT CITY CENTRE. Markets settle on one airport's routine METARs
   (ICAO in the market's resolutionSource, e.g. EGLC, RJTT). Forecasts are now
   pulled at that station; CITY_COORDS is only a fallback (Hong Kong / HKO).
2. TODAY = WHAT WAS ALREADY REPORTED + THE REST OF THE DAY. For a market on the
   station's current local date the final value is max(reported so far, rest
   of day) for highs / min(...) for lows. The old bot priced the whole day from
   forecasts and bet against Asian afternoon highs the market already knew.
   Today's market with no METAR to condition on -> no trade.
3. CALIBRATED ERROR MODEL. weather_calibrate.py measures bias + sigma of the
   model consensus against settled markets per station/kind/lead. Members are
   centred on consensus + bias and the kernel only tops the spread up to the
   calibrated sigma (the old kernel was added ON TOP of the member spread,
   underpricing every favourite). No calibration file -> no entries.

Warnings
--------
- Real-money trading. Run with DRY_RUN=True until the summary PnL convinces you.
- Model probabilities are estimates; ensembles can be confidently wrong
  (station micro-climate, sensor quirks). Small stakes, many independent bets.
- Use at your own risk.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import smtplib
import logging
from email.mime.text import MIMEText
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from statistics import mean, pvariance
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

# --- Trading mode / bankroll ---
DRY_RUN = True              # True = signal/notify only; False = real orders
BANKROLL_USDC = 50.0        # notional bankroll Kelly sizes against
KELLY_FRACTION = 0.15       # fraction of full Kelly (hermes/suislanchez both ~0.15)
MIN_TRADE_USDC = 1.0        # never stake less than this (below = skip)
MAX_TRADE_USDC = 5.0        # never stake more than this on one bucket

# --- Edge / entry rules ---
MIN_EDGE = 0.08             # required EV: model p_win / ask - 1 >= 8%
MIN_PRICE = 0.05            # don't buy tokens cheaper than this (junk/longshot zone)
MAX_PRICE = 0.92            # don't buy tokens dearer than this (no room left)
MIN_MODEL_PROB = 0.05       # our own p_win must be at least this
ENTRY_PRICE_BUFFER = 0.01   # pay this much above ask to fill fast
MAX_ENTRY_PRICE = 0.96      # absolute cap on the limit price

# --- Real-fill honesty (added 2026-07-05 after the price audit) ---
# Weather books are thin: far-OTM buckets often have NO real sellers below
# 0.97+ even when /price reports a pretty number. Every entry must now be
# verified against the live order book (VWAP across ask levels + depth), and
# the market's taker fee is baked into the recorded entry price so DRY_RUN PnL
# matches what real money would do.
# Fee per share = rate * (p * (1 - p)) ** exponent, from each market's own
# feeSchedule (docs.polymarket.com/trading/fees; some weather markets are
# zero_fees). The old rate * min(p, 1-p) charged up to 2x the real fee.
TAKER_FEE_RATE = 0.05       # fallback only, when a market carries no feeSchedule
MAX_SLIPPAGE = 0.02         # never walk the book more than this above best ask

MAX_OPEN_POSITIONS = 40
MAX_ENTRIES_PER_CITY_DAY = 2   # at most this many buckets per city per day
ENTRY_COOLDOWN_SEC = 3600      # don't re-enter the same token within this long
# 24/7, no time-of-day window — like all three source repos, entries are gated
# purely by EV. suislanchez scans weather markets every 5 minutes; same cadence.
POLL_SECONDS = 300

# --- Forecast engine ---
# Ensemble members = empirical distribution (suislanchez idea).
ENSEMBLE_MODELS = "gfs_seamless"            # add ",icon_seamless" for +40 members
# Deterministic multi-model consensus (PolyWeather idea).
DET_MODELS = "ecmwf_ifs025,gfs_seamless,icon_seamless"
CONSENSUS_WEIGHT = 1.0      # how far ensemble mean is pulled toward consensus mean
                            # (1.0: the calibration measures the consensus, keep it)
SIGMA_FLOOR_C = 0.3         # min Gaussian kernel width (°C); the kernel only tops the
                            # member spread up to the calibrated sigma
FORECAST_CACHE_TTL = 3600   # cache forecasts per station this long
FORECAST_FAIL_TTL = 600     # retry a failed station after this long

# --- Station observations + calibration (v3.1 fixes, 2026-10-28) ---
OBS_CACHE_TTL = 240         # re-read a station's METARs at most this often
NOWCAST_HOURS = 3.0         # today's model error = mean(report - model) over the last this many hours
NOWCAST_TAU_H = 6.0         # ...fading into the calibrated bias with this e-folding time
                            # (a guess: calibrate it in the backtest)
CALIBRATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather_calibration.json")
CAL_MIN_SAMPLES = 15        # fewer settled days than this -> use the pooled global fit

# --- Sanity filter: model vs market favorite must roughly agree ---
SANITY_FILTER = True
SANITY_TOL_C = 3.0          # skip city/day when |model mean - market favorite| > this

# --- Result tracking / summary ---
SUMMARY_EVERY_HOURS = 12
RESOLUTION_GRACE_SEC = 600
RESOLUTION_MAX_WAIT_SEC = 86400
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather_bot_v3_state.json")

# --- Notifications ---
NOTIFY_TELEGRAM = True
NOTIFY_GMAIL = False

# --- API endpoints ---
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
AVWX_API = "https://aviationweather.gov/api/data"

# --- Secrets (environment variables; do NOT hardcode) ---
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "") or os.getenv("WALLET_PRIVATE_KEY", "")
POLY_FUNDER = os.getenv("POLY_FUNDER", "") or os.getenv("POLYMARKET_RELAYER_ADDRESS", "")
CHAIN_ID = 137
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO = os.getenv("GMAIL_TO", "") or GMAIL_USER

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("weatherbot3")
for _noisy in ("httpx", "httpcore", "polymarket", "web3", "websocket"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# ----------------------------------------------------------------------------
# DATA STRUCTURES
# ----------------------------------------------------------------------------
@dataclass
class Bucket:
    """One temperature bucket market: 'Will the highest temp in NYC be 82-83°F?'"""
    title: str
    market_id: str
    yes_token_id: str
    no_token_id: str
    end_ts: float
    city: str                # lowercase city key
    date: str                # local settlement date "YYYY-MM-DD" (= endDate date)
    kind: str                # "highest" | "lowest"
    unit: str                # "C" | "F"
    lo: Optional[float]      # bucket lower integer (None = open low end, "X or below")
    hi: Optional[float]      # bucket upper integer (None = open high end, "X or higher")
    yes_price_gamma: Optional[float] = None   # indicative Yes price from Gamma
    station: str = ""        # settlement METAR station ICAO ("" = none, e.g. HKO)
    fee_rate: float = TAKER_FEE_RATE   # this market's taker feeSchedule
    fee_exp: float = 1.0

    def mid(self) -> Optional[float]:
        if self.lo is not None and self.hi is not None:
            return (self.lo + self.hi) / 2.0
        return self.lo if self.lo is not None else self.hi


@dataclass
class Forecast:
    """Calibrated probability model of a station's daily high/low for one date.
    Final high = max(obs_max, R), final low = min(obs_min, R), where R (the
    rest of the day, or the whole day ahead) = members + Gaussian kernel."""
    samples_max: list[float] = field(default_factory=list)   # °C, recentered members
    samples_min: list[float] = field(default_factory=list)
    sigma_max: float = SIGMA_FLOOR_C                          # kernel width °C
    sigma_min: float = SIGMA_FLOOR_C
    mu_max: Optional[float] = None                            # calibrated high °C
    mu_min: Optional[float] = None
    obs_max: Optional[float] = None                           # already reported today °C
    obs_min: Optional[float] = None
    lead: str = "d1"                                          # "d0" today, "d1" ahead
    n_det_models: int = 0
    n_members: int = 0


# ----------------------------------------------------------------------------
# TITLE PARSING (from v2, upgraded to full bucket RANGES for probabilities)
# ----------------------------------------------------------------------------
TEMP_TITLE_RE = re.compile(
    r"\btemperature\b|\btemp\b|\bdegrees\b|\bfahrenheit\b|\bcelsius\b|°", re.IGNORECASE
)
GLOBAL_EXCLUDE_RE = re.compile(
    r'\bglobal\b|\bannual\b|\byearly\b|\bclimate\b|\b2024\b|\b2025\b|\b2026\b.*\bby\s+\d+',
    re.IGNORECASE
)
_CITY_RE = re.compile(r'\bin\s+(.+?)\s+(?:be\b|on\s)', re.IGNORECASE)
# settlement station: 'metar?ids=EGLC&...' (now), 'wrh/timeseries?site=eglc' or
# '.../history/daily/cn/jinan/ZSJN' (older markets)
_RE_STATION = re.compile(
    r"[?&](?:ids|site)=([A-Za-z0-9]{4})\b|wunderground\.com/history/daily/(?:[^/\s]+/)+([A-Z0-9]{4})\b"
)

_RE_BETWEEN = re.compile(r"between\s+(-?\d+)\s*(?:-|to|and)\s*(-?\d+)\s*°?\s*([CF])", re.IGNORECASE)
_RE_OR_BELOW = re.compile(r"(-?\d+)\s*°\s*([CF])\s*or\s*(?:below|lower|less)", re.IGNORECASE)
_RE_OR_ABOVE = re.compile(r"(-?\d+)\s*°\s*([CF])\s*or\s*(?:higher|above|more)", re.IGNORECASE)
_RE_SINGLE = re.compile(r"(-?\d+)\s*°\s*([CF])", re.IGNORECASE)


def _is_temperature(title: str) -> bool:
    if GLOBAL_EXCLUDE_RE.search(title or ""):
        return False
    return bool(TEMP_TITLE_RE.search(title or ""))


def _extract_city(title: str) -> str:
    """'... in Seoul (Incheon) be 5°C ...' -> 'seoul'."""
    m = _CITY_RE.search(title or "")
    return re.sub(r"\s*\(.*?\)", "", m.group(1)).strip().lower() if m else ""


def _station_from_market(mk: dict, ev: dict) -> str:
    """ICAO of the settlement station, or '' when the market names none."""
    for text in (mk.get("resolutionSource"), ev.get("resolutionSource"), mk.get("description")):
        m = _RE_STATION.search(text or "")
        if m:
            return (m.group(1) or m.group(2)).upper()
    return ""


def _parse_bucket_range(title: str) -> Optional[tuple[str, Optional[float], Optional[float], str]]:
    """Return (kind, lo, hi, unit) or None.
    lo/hi are the bucket's integer bounds in market units; None = open end.
    'between 82-83°F' -> (82, 83); '17°C' -> (17, 17);
    '81°F or below' -> (None, 81); '100°F or higher' -> (100, None)."""
    t = title or ""
    if re.search(r"\bhighest\b", t, re.IGNORECASE):
        kind = "highest"
    elif re.search(r"\blowest\b", t, re.IGNORECASE):
        kind = "lowest"
    else:
        return None
    m = _RE_BETWEEN.search(t)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return kind, min(a, b), max(a, b), m.group(3).upper()
    m = _RE_OR_BELOW.search(t)
    if m:
        return kind, None, float(m.group(1)), m.group(2).upper()
    m = _RE_OR_ABOVE.search(t)
    if m:
        return kind, float(m.group(1)), None, m.group(2).upper()
    m = _RE_SINGLE.search(t)
    if m:
        v = float(m.group(1))
        return kind, v, v, m.group(2).upper()
    return None


# ----------------------------------------------------------------------------
# POLYMARKET: scan Gamma for open temperature bucket markets (from v2)
# ----------------------------------------------------------------------------
def _target_trade_date() -> str:
    """Markets close at 12:00 UTC daily. Before 12 UTC -> today, after -> tomorrow."""
    now_utc = datetime.now(timezone.utc)
    if now_utc.hour >= 12:
        return (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")
    return now_utc.strftime("%Y-%m-%d")


def _iso_to_epoch(iso: str) -> float:
    iso = iso.replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _parse_token_ids(market: dict) -> list[str]:
    raw = market.get("clobTokenIds")
    if not raw:
        return []
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        return [str(x) for x in ids]
    except Exception:
        return []


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


def _fee_schedule(mk: dict) -> tuple[float, float]:
    """(taker rate, exponent) of one market; fallback when it carries none."""
    fs = mk.get("feeSchedule")
    if mk.get("feesEnabled") is False:
        return 0.0, 1.0
    if not isinstance(fs, dict) or fs.get("rate") is None:
        return TAKER_FEE_RATE, 1.0
    return float(fs["rate"]), float(fs.get("exponent") or 1.0)


def _yes_price_from_gamma(mk: dict) -> Optional[float]:
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


def find_temperature_buckets() -> list[Bucket]:
    """Every OPEN temperature bucket market resolving on the target trade date."""
    now = time.time()
    target_date = _target_trade_date()
    # Temperature markets all resolve at 12:00 UTC — bracket the endDate window
    # tightly so 5-minute crypto markets don't flood the paging (v2 lesson).
    target_dt = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
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

    out: list[Bucket] = []
    for ev in events:
        ev_title = ev.get("title") or ""
        for mk in (ev.get("markets") or []):
            mk_title = mk.get("question") or ev_title
            if not (_is_temperature(mk_title) or _is_temperature(ev_title)):
                continue
            if mk.get("closed") or mk.get("acceptingOrders") is False:
                continue
            end_iso = mk.get("endDate")
            if not end_iso:
                continue
            end_ts = _iso_to_epoch(end_iso)
            if end_ts <= now or end_iso[:10] != target_date:
                continue
            parsed = _parse_bucket_range(mk_title)
            city = _extract_city(mk_title)
            station = _station_from_market(mk, ev)
            if not parsed or not city or not (station or city in CITY_COORDS):
                continue
            token_ids = _parse_token_ids(mk)
            if len(token_ids) < 2:
                continue
            yes_id, no_id = _order_yes_no(mk, token_ids)
            kind, lo, hi, unit = parsed
            fee_rate, fee_exp = _fee_schedule(mk)
            out.append(Bucket(
                title=mk_title, market_id=str(mk.get("id") or ""),
                yes_token_id=yes_id, no_token_id=no_id, end_ts=end_ts,
                city=city, date=end_iso[:10], kind=kind, unit=unit,
                lo=lo, hi=hi, yes_price_gamma=_yes_price_from_gamma(mk),
                station=station, fee_rate=fee_rate, fee_exp=fee_exp,
            ))
    return out


# ----------------------------------------------------------------------------
# CITY COORDINATES (from v2) — fallback only, for markets that name no METAR
# station (Hong Kong settles on HKO). Everything else uses the station.
# ----------------------------------------------------------------------------
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


# ----------------------------------------------------------------------------
# SETTLEMENT STATION + ITS REPORTS (fix 1 + fix 2)
# ----------------------------------------------------------------------------
_station_coords: dict[str, tuple[float, float]] = {}
# icao -> (fetched_at, [(obs_epoch, temp_c)])
_obs_cache: dict[str, tuple[float, list[tuple[float, float]]]] = {}

_RE_TGROUP = re.compile(r"\sT([01])(\d{3})[01]\d{3}\b")
_RE_BODY_TEMP = re.compile(r"\s(M?\d{2})/(?:M?\d{2})?\s")


def station_coords(icao: str) -> Optional[tuple[float, float]]:
    """(lat, lon) of a METAR station from aviationweather.gov (cached)."""
    if icao not in _station_coords:
        try:
            r = requests.get(f"{AVWX_API}/stationinfo", params={"ids": icao, "format": "json"}, timeout=20)
            r.raise_for_status()
            info = (r.json() or [{}])[0]
            _station_coords[icao] = (float(info["lat"]), float(info["lon"]))
        except Exception as e:
            log.warning("station lookup failed for %s: %s", icao, e)
            return None
    return _station_coords[icao]


def _metar_temp_c(raw: str) -> Optional[float]:
    """Qualifying temperature of one routine METAR, per the market rules: the
    hourly T-group in tenths when present, else the body group in whole °C."""
    m = _RE_TGROUP.search(raw)
    if m:
        v = int(m.group(2)) / 10.0
        return -v if m.group(1) == "1" else v
    m = _RE_BODY_TEMP.search(f" {raw} ")
    if m:
        s = m.group(1)
        return -float(s[1:]) if s.startswith("M") else float(s)
    return None


def _station_obs(icao: str) -> list[tuple[float, float]]:
    """[(obs_epoch, temp_c)] of the station's routine METARs over the last 30h.
    SPECI reports do not count for settlement, so they are dropped."""
    cached = _obs_cache.get(icao)
    if cached and time.time() - cached[0] < OBS_CACHE_TTL:
        return cached[1]
    r = requests.get(f"{AVWX_API}/metar", params={"ids": icao, "format": "json", "hours": 30}, timeout=20)
    r.raise_for_status()
    out = []
    for ob in r.json() or []:
        raw = str(ob.get("rawOb") or "")
        if raw.startswith("SPECI") or ob.get("obsTime") is None:
            continue
        t = _metar_temp_c(raw)
        if t is not None:
            out.append((float(ob["obsTime"]), t))
    _obs_cache[icao] = (time.time(), out)
    return out


def observed_extremes(icao: str, date: str, utc_offset: int) -> tuple[Optional[float], Optional[float], int]:
    """(max °C, min °C, n reports) of routine METARs whose station-LOCAL
    time falls on `date` — the same reports the market settles on."""
    temps = [t for ts, t in _station_obs(icao)
             if datetime.fromtimestamp(ts + utc_offset, timezone.utc).strftime("%Y-%m-%d") == date]
    if not temps:
        return None, None, 0
    return max(temps), min(temps), len(temps)


# ----------------------------------------------------------------------------
# CALIBRATION (fix 3) — written by weather_calibrate.py
# ----------------------------------------------------------------------------
_calibration: dict = {}
_calibration_mtime = 0.0


def load_calibration() -> dict:
    """weather_calibration.json, re-read whenever the file changes."""
    global _calibration, _calibration_mtime
    try:
        mtime = os.path.getmtime(CALIBRATION_FILE)
    except OSError:
        return {}
    if mtime != _calibration_mtime:
        try:
            with open(CALIBRATION_FILE, "r", encoding="utf-8") as f:
                _calibration = json.load(f)
            _calibration_mtime = mtime
            log.info("calibration loaded: %d stations, generated %s",
                     len(_calibration.get("stations", {})), _calibration.get("generated_at"))
        except Exception as e:
            log.warning("calibration load failed: %s", e)
    return _calibration


def error_model(station_key: str, kind: str, lead: str) -> Optional[tuple[float, float]]:
    """(bias °C, sigma °C) of truth - consensus for this station/kind/lead:
    the station's own fit when it has enough settled days, else the pooled
    global fit. None = uncalibrated -> no trade."""
    stations = load_calibration().get("stations", {})
    for key in (station_key, "_global"):
        e = stations.get(key, {}).get(kind, {}).get(lead)
        if e and e.get("n", 0) >= CAL_MIN_SAMPLES:
            return e["bias"], e["sigma"]
    return None


# ----------------------------------------------------------------------------
# FORECAST ENGINE: ensemble members + multi-model consensus -> Forecast
# ----------------------------------------------------------------------------
# station key -> (fetched_at, {"ens": (times, series), "det": (times, series), "offset": s})
_forecast_cache: dict[str, tuple[float, Optional[dict]]] = {}


def _fetch_hourly(url: str, lat: float, lon: float, models: str) -> tuple[list[str], list[list], int]:
    """(local times, one hourly temperature series per member/model, utc offset s).
    Hourly (not daily) so today's remaining hours can be split off."""
    r = requests.get(
        url,
        params={"latitude": lat, "longitude": lon, "hourly": "temperature_2m",
                "models": models, "forecast_days": 3, "timezone": "auto"},
        timeout=30,
    )
    r.raise_for_status()
    js = r.json()
    hourly = js.get("hourly", {})
    series = [hourly[k] or [] for k in hourly if k.startswith("temperature_2m")]
    return hourly.get("time", []), series, int(js.get("utc_offset_seconds") or 0)


def _day_extremes(times: list[str], series: list[list], date: str,
                  after: Optional[str] = None, min_hours: int = 20) -> tuple[list[float], list[float]]:
    """Per series: (max, min) over the local date's hours (suislanchez's core
    trick), or over only the hours after `after` ("YYYY-MM-DDTHH:MM") for the
    rest of today."""
    maxes, mins = [], []
    for vals in series:
        temps = [float(v) for t, v in zip(times, vals)
                 if v is not None and t[:10] == date and (after is None or t > after)]
        if not temps or (after is None and len(temps) < min_hours):
            continue        # partial day -> max/min would be biased
        maxes.append(max(temps))
        mins.append(min(temps))
    return maxes, mins


def _calibrated_samples(ens: list[float], det: list[float], bias: float,
                        sigma: float) -> tuple[list[float], float, Optional[float]]:
    """(samples, kernel, centre). Members are centred on consensus + bias and
    the kernel only tops their spread up to the calibrated sigma:
    var(members) + kernel^2 = sigma^2."""
    if not ens and not det:
        return [], sigma, None
    if not ens:
        mu = mean(det) + bias
        return [mu], max(sigma, SIGMA_FLOOR_C), mu
    centre = mean(ens) + (mean(det) - mean(ens)) * CONSENSUS_WEIGHT if det else mean(ens)
    mu = centre + bias
    shift = mu - mean(ens)
    samples = [v + shift for v in ens]
    kernel = math.sqrt(max(sigma ** 2 - pvariance(samples), SIGMA_FLOOR_C ** 2))
    return samples, kernel, mu


def _series_at(times: list[str], vals: list, local: datetime) -> Optional[float]:
    """One hourly local-time series, linearly interpolated at a local datetime."""
    t0 = local.replace(minute=0, second=0, microsecond=0)
    try:
        i = times.index(t0.strftime("%Y-%m-%dT%H:%M"))
    except ValueError:
        return None
    if i + 1 >= len(vals) or vals[i] is None or vals[i + 1] is None:
        return None
    return vals[i] + (vals[i + 1] - vals[i]) * (local - t0).total_seconds() / 3600.0


def _nowcast(times: list[str], series: list[list], recent: list[tuple[datetime, float]],
             now_local: datetime, bias: float) -> list[list]:
    """Series with the error the station shows RIGHT NOW carried forward: each
    hour gets bias + (err_now - bias) * exp(-hours_ahead / NOWCAST_TAU_H), so
    today's miss fades into the calibrated bias. `recent` = [(local time, °C)];
    with nothing to compare against, err_now = bias (plain calibrated shift)."""
    errs = []
    for local, temp in recent:
        vals = [v for v in (_series_at(times, s, local) for s in series) if v is not None]
        if vals:
            errs.append(temp - mean(vals))
    err_now = mean(errs) if errs else bias
    weights = [math.exp(-max((datetime.strptime(t, "%Y-%m-%dT%H:%M") - now_local).total_seconds(), 0.0)
                        / 3600.0 / NOWCAST_TAU_H) for t in times]
    return [[None if v is None else v + bias + (err_now - bias) * w for v, w in zip(vals, weights)]
            for vals in series]


def _forecast_raw(key: str, lat: float, lon: float) -> Optional[dict]:
    """Hourly ensemble + multi-model series for a station. Cached FORECAST_CACHE_TTL."""
    cached = _forecast_cache.get(key)
    if cached and time.time() - cached[0] < (FORECAST_CACHE_TTL if cached[1] else FORECAST_FAIL_TTL):
        return cached[1]
    raw: dict = {"ens": ([], []), "det": ([], []), "offset": None}
    for part, url, models in (("ens", OPEN_METEO_ENSEMBLE, ENSEMBLE_MODELS),
                              ("det", OPEN_METEO_FORECAST, DET_MODELS)):
        try:
            times, series, offset = _fetch_hourly(url, lat, lon, models)
            raw[part] = (times, series)
            raw["offset"] = offset
        except Exception as e:
            log.warning("%s fetch failed for %s: %s", part, key, e)
    if raw["offset"] is None:
        raw = None
    else:
        log.info("forecast %s: members=%d models=%d", key, len(raw["ens"][1]), len(raw["det"][1]))
    _forecast_cache[key] = (time.time(), raw)
    return raw


def get_forecast(city: str, station: str, date: str) -> Optional[Forecast]:
    """Calibrated Forecast for the settlement station's local `date`.
    Today: final = reported so far combined with the rest of the day.
    Ahead: the whole day from the forecast."""
    key = station or f"city:{city}"
    # a failed station lookup must not fall back to the city centre (that was bug 1)
    coords = station_coords(station) if station else CITY_COORDS.get(city)
    if not coords:
        return None
    raw = _forecast_raw(key, *coords)
    if raw is None:
        return None
    now_local = datetime.now(timezone.utc) + timedelta(seconds=raw["offset"])
    today = now_local.strftime("%Y-%m-%d")
    if date < today:
        return None             # local day already over -> settles on reports, no edge
    fc = Forecast(lead="d0" if date == today else "d1")
    after = None
    recent: list[tuple[datetime, float]] = []
    if date == today:
        if not station:
            log.info("%s %s: today's market but no METAR station to condition on -> skip", city, date)
            return None
        try:
            fc.obs_max, fc.obs_min, n_obs = observed_extremes(station, date, raw["offset"])
        except Exception as e:
            log.warning("METAR fetch failed for %s: %s", station, e)
            return None
        if not n_obs:
            log.info("%s %s: no %s reports for today yet -> skip", city, date, station)
            return None
        after = now_local.strftime("%Y-%m-%dT%H:%M")
        recent = [(datetime.fromtimestamp(ts + raw["offset"], timezone.utc).replace(tzinfo=None), t)
                  for ts, t in _station_obs(station) if time.time() - ts <= NOWCAST_HOURS * 3600]

    now_naive = now_local.replace(tzinfo=None)
    for kind, obs in (("highest", fc.obs_max), ("lowest", fc.obs_min)):
        em = error_model(key, kind, fc.lead)
        if em is None:
            continue
        bias, sigma = em
        (e_times, e_series), (d_times, d_series) = raw["ens"], raw["det"]
        if fc.lead == "d0":     # bias goes in hour by hour, led by what the station shows now
            e_series = _nowcast(e_times, e_series, recent, now_naive, bias)
            d_series = _nowcast(d_times, d_series, recent, now_naive, bias)
            bias = 0.0
        ens = _day_extremes(e_times, e_series, date, after)[0 if kind == "highest" else 1]
        det = _day_extremes(d_times, d_series, date, after)[0 if kind == "highest" else 1]
        fc.n_members, fc.n_det_models = max(fc.n_members, len(ens)), max(fc.n_det_models, len(det))
        samples, kernel, mu = _calibrated_samples(ens, det, bias, sigma)
        if obs is not None:
            mu = obs if mu is None else (max(obs, mu) if kind == "highest" else min(obs, mu))
        if kind == "highest":
            fc.samples_max, fc.sigma_max, fc.mu_max = samples, kernel, mu
        else:
            fc.samples_min, fc.sigma_min, fc.mu_min = samples, kernel, mu
    return fc


# ----------------------------------------------------------------------------
# PROBABILITY: P(bucket) from the calibrated distribution
# ----------------------------------------------------------------------------
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_probability(bucket: Bucket, fc: Forecast) -> Optional[float]:
    """P(settled temperature falls in this bucket). Final high = max(obs, R),
    final low = min(obs, R), R = Gaussian-kernel-smoothed members. Settlement
    rounds half up to whole degrees, so bucket [lo, hi] covers the continuous
    interval [lo-0.5, hi+0.5)."""
    highest = bucket.kind == "highest"
    samples_c = fc.samples_max if highest else fc.samples_min
    sigma_c = fc.sigma_max if highest else fc.sigma_min
    obs_c = fc.obs_max if highest else fc.obs_min
    if not samples_c and obs_c is None:
        return None
    if bucket.unit == "F":
        samples = [v * 9 / 5 + 32 for v in samples_c]
        sigma = sigma_c * 9 / 5
        obs = obs_c * 9 / 5 + 32 if obs_c is not None else None
    else:
        samples, sigma, obs = samples_c, sigma_c, obs_c

    def cdf(x: Optional[float], open_end: float) -> float:
        """P(final < x); x None = the bucket's open end."""
        if x is None:
            return open_end
        if obs is not None:
            if highest and x <= obs:
                return 0.0      # the high can only go up from what was reported
            if not highest and x > obs:
                return 1.0      # the low can only go down
        if not samples:         # no hours left: the reports are final
            return 1.0 if highest else 0.0
        return sum(_norm_cdf((x - m) / sigma) for m in samples) / len(samples)

    a = bucket.lo - 0.5 if bucket.lo is not None else None   # T >= lo-0.5
    b = bucket.hi + 0.5 if bucket.hi is not None else None   # T <  hi+0.5
    return max(0.0, cdf(b, 1.0) - cdf(a, 0.0))


def sanity_ok(city: str, date: str, kind: str, fc: Forecast,
              buckets: list[Bucket]) -> bool:
    """PolyWeather-style guard: the market's favorite bucket (highest Gamma Yes
    price) must sit near our consensus mean, else skip the whole city/day."""
    if not SANITY_FILTER:
        return True
    mu_c = fc.mu_max if kind == "highest" else fc.mu_min
    if mu_c is None:
        return False
    fav = None
    for b in buckets:
        if b.kind != kind or b.mid() is None or b.yes_price_gamma is None:
            continue
        if fav is None or b.yes_price_gamma > fav.yes_price_gamma:
            fav = b
    if fav is None:
        log.info("sanity %s %s %s: no market favorite -> skip", city, date, kind)
        return False
    mu = mu_c * 9 / 5 + 32 if fav.unit == "F" else mu_c
    tol = SANITY_TOL_C * 9 / 5 if fav.unit == "F" else SANITY_TOL_C
    gap = abs(fav.mid() - mu)
    ok = gap <= tol
    log.info("sanity %s %s %s: market-fav=%.1f°%s vs model=%.1f°%s gap=%.1f tol=%.1f -> %s",
             city, date, kind, fav.mid(), fav.unit, mu, fav.unit, gap, tol,
             "OK" if ok else "SKIP city")
    return ok


# ----------------------------------------------------------------------------
# EV + KELLY (hermes-style decision core)
# ----------------------------------------------------------------------------
def best_side(p_yes: float, ask_yes: Optional[float],
              ask_no: Optional[float]) -> Optional[tuple[str, float, float, float]]:
    """Pick the higher-EV side. Returns (side, p_win, ask, ev) or None."""
    candidates = []
    if ask_yes is not None and MIN_PRICE <= ask_yes <= MAX_PRICE and p_yes >= MIN_MODEL_PROB:
        candidates.append(("Yes", p_yes, ask_yes, p_yes / ask_yes - 1.0))
    p_no = 1.0 - p_yes
    if ask_no is not None and MIN_PRICE <= ask_no <= MAX_PRICE and p_no >= MIN_MODEL_PROB:
        candidates.append(("No", p_no, ask_no, p_no / ask_no - 1.0))
    if not candidates:
        return None
    side = max(candidates, key=lambda c: c[3])
    return side if side[3] >= MIN_EDGE else None


def kelly_stake(p_win: float, price: float) -> float:
    """Fractional-Kelly stake in USDC for a binary token bought at `price`
    paying 1.0 on a win: full Kelly f* = (p - price) / (1 - price)."""
    if price >= 1.0 or p_win <= price:
        return 0.0
    f_full = (p_win - price) / (1.0 - price)
    stake = BANKROLL_USDC * KELLY_FRACTION * f_full
    return min(stake, MAX_TRADE_USDC)


# ----------------------------------------------------------------------------
# CLOB prices (from v2)
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
    """TRUE best ASK — what sellers actually charge right now.
    The CLOB /price endpoint names sides by the ORDER BOOK: side=sell is the
    sell side (asks). The old side=buy returned the best BID, which recorded
    entries ~2-25c cheaper than reality on thin weather books. No midpoint
    fallback either — the midpoint of a 0.03/0.99 book is a fantasy price."""
    try:
        r = requests.get(f"{CLOB_API}/price", params={"token_id": token_id, "side": "sell"}, timeout=10)
        r.raise_for_status()
        p = float(r.json()["price"])
        return p if 0.0 < p < 1.0 else None
    except Exception as e:
        log.debug("ask error for %s: %s", token_id, e)
        return None


def taker_fee_per_share(price: float, rate: float = TAKER_FEE_RATE, exponent: float = 1.0) -> float:
    """Polymarket taker fee per share: rate * (p * (1 - p)) ** exponent."""
    return rate * (price * (1.0 - price)) ** exponent


def book_fill(token_id: str, usdc: float) -> Optional[tuple[float, float]]:
    """Walk the REAL sell side of the live order book to fill `usdc` worth.
    Returns (vwap_price, shares) or None when the book cannot fill the stake
    within MAX_SLIPPAGE of the best ask — thin/empty books mean NO trade,
    which is exactly what would happen with real money."""
    try:
        r = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=10)
        r.raise_for_status()
        raw = r.json().get("asks") or []
        asks = sorted((float(a["price"]), float(a["size"])) for a in raw)
    except Exception as e:
        log.debug("book error for %s: %s", token_id, e)
        return None
    if not asks:
        return None
    cap = asks[0][0] + MAX_SLIPPAGE
    remaining, cost, shares = usdc, 0.0, 0.0
    for price, size in asks:
        if price > cap:
            break
        take_usd = min(remaining, price * size)
        cost += take_usd
        shares += take_usd / price
        remaining -= take_usd
        if remaining <= 0.005:
            break
    if remaining > 0.005 or shares <= 0:
        return None
    return cost / shares, shares


# ----------------------------------------------------------------------------
# NOTIFICATIONS: Telegram + Gmail (from v2)
# ----------------------------------------------------------------------------
def notify(text: str, subject: str = "Weather bot v3 signal") -> None:
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


def _send_gmail(text: str, subject: str = "Weather bot v3 signal") -> None:
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
# ORDER PLACEMENT via the official Polymarket SDK (from v2)
# ----------------------------------------------------------------------------
_clob_client = None


def _get_clob_client():
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    import polymarket
    from polymarket import RelayerApiKey
    # IMPORTANT: do NOT pass `wallet` — let the SDK derive the real Deposit Wallet.
    kwargs = dict(private_key=POLY_PRIVATE_KEY)
    relayer_key = os.getenv("POLYMARKET_RELAYER_API_KEY", "")
    if relayer_key and POLY_FUNDER:
        kwargs["api_key"] = RelayerApiKey(key=relayer_key, address=POLY_FUNDER)
    client = polymarket.SecureClient.create(**kwargs)
    log.info("Polymarket DEPOSIT WALLET (send USDC here): %s", getattr(client, "wallet", "?"))
    _clob_client = client
    return client


def place_buy(token_id: str, ask: float, usdc: float) -> Optional[float]:
    """Marketable BUY for `usdc` worth. Returns shares bought, or None."""
    price = min(round(ask + ENTRY_PRICE_BUFFER, 2), MAX_ENTRY_PRICE)
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
# STATE / RESOLUTION / SUMMARY (from v2)
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
    """'win' / 'loss' for this position, or None if not settled yet."""
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
        f"Weather bot v3 — {SUMMARY_EVERY_HOURS}h SUMMARY",
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
        lines.append(f"  {en.get('side')} @ {en.get('entry_rate', 0):.3f} x{en.get('size', 0):.2f} "
                     f"ev={en.get('ev', 0):.0%} p={en.get('p_win', 0):.2f} | "
                     f"{str(en.get('title',''))[:50]}  ({t} UTC)")
    if not entries:
        lines.append("  (no entries taken in this period)")
    body = "\n".join(lines)
    log.warning(">>> SUMMARY | entries=%d wins=%d losses=%d net=$%.2f",
                len(entries), len(wins), len(losses), net)
    notify(body, subject=f"Weather bot v3 {SUMMARY_EVERY_HOURS}h summary")


# ----------------------------------------------------------------------------
# MAIN LOOP — runs 24/7 until Ctrl+C
# ----------------------------------------------------------------------------
def main():
    log.info("Weather bot v3 started | DRY_RUN=%s | edge>=%.0f%% price %.2f-%.2f | "
             "kelly=%.2f x bankroll $%.0f (stake $%.0f-$%.0f) | ensemble=%s | models=%s",
             DRY_RUN, MIN_EDGE * 100, MIN_PRICE, MAX_PRICE,
             KELLY_FRACTION, BANKROLL_USDC, MIN_TRADE_USDC, MAX_TRADE_USDC,
             ENSEMBLE_MODELS, DET_MODELS)
    if not DRY_RUN and not POLY_PRIVATE_KEY:
        log.error("DRY_RUN=False but POLY_PRIVATE_KEY is not set. Stopping.")
        return

    # ---- restore state (survives restarts) ----
    state = _load_state()
    last_entry: dict[str, float] = state.get("last_entry", {})   # token_id -> entry epoch
    positions: dict[str, dict] = state.get("positions", {})      # token_id -> open position
    pending: list = state.get("pending", [])
    period: list = state.get("period", [])
    entries: list = state.get("entries", [])
    report_start: float = state.get("report_start") or time.time()

    def save():
        _save_state({"last_entry": last_entry, "positions": positions,
                     "pending": pending, "period": period, "entries": entries,
                     "report_start": report_start})

    def city_day_count(city: str, date: str) -> int:
        n = 0
        for p in list(positions.values()) + pending:
            if p.get("city") == city and p.get("date") == date:
                n += 1
        return n

    log.info("restored: %d open, %d pending, %d entries/%d results this period",
             len(positions), len(pending), len(entries), len(period))
    if not load_calibration():
        log.warning("no %s -> run weather_calibrate.py first; no entries until then",
                    os.path.basename(CALIBRATION_FILE))

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
                               "token_id": p.get("token_id", "")})
                log.warning(">>> RESULT %s [%s] pnl=$%.2f", result.upper(), p["title"], pnl)
                save()
            pending = still_pending

            # ---- 3) periodic profit + entry summary ----
            if now - report_start >= SUMMARY_EVERY_HOURS * 3600:
                send_summary(entries, period, report_start, now)
                period = []
                entries = []
                report_start = now
                save()

            # ---- 4) scan markets, compute probabilities, take EV entries ----
            buckets = find_temperature_buckets()
            if not buckets:
                log.info("no open temperature markets right now.")

            # group per (city, date, kind) so the sanity filter sees the whole ladder
            groups: dict[tuple[str, str, str], list[Bucket]] = {}
            for b in buckets:
                groups.setdefault((b.city, b.date, b.kind), []).append(b)

            for (city, date, kind), group in groups.items():
                if len(positions) >= MAX_OPEN_POSITIONS:
                    break
                fc = get_forecast(city, group[0].station, date)
                if fc is None:
                    log.info("%s %s: no forecast -> skip", city, date)
                    continue
                if not sanity_ok(city, date, kind, fc, group):
                    continue

                # score every bucket, then take the best edges first
                scored: list[tuple[float, Bucket, str, float, float]] = []
                for b in group:
                    p_yes = bucket_probability(b, fc)
                    if p_yes is None:
                        continue
                    tid_yes, tid_no = b.yes_token_id, b.no_token_id
                    if any(t in positions or t in [q.get("token_id") for q in pending]
                           or now - last_entry.get(t, 0) < ENTRY_COOLDOWN_SEC
                           for t in (tid_yes, tid_no)):
                        continue
                    ask_yes = get_buy_price(tid_yes)
                    ask_no = get_buy_price(tid_no)
                    pick = best_side(p_yes, ask_yes, ask_no)
                    log.info("%s | p_yes=%.2f ask_yes=%s ask_no=%s -> %s",
                             b.title[:60], p_yes,
                             f"{ask_yes:.2f}" if ask_yes is not None else "?",
                             f"{ask_no:.2f}" if ask_no is not None else "?",
                             f"{pick[0]} EV={pick[3]:+.0%}" if pick else "no edge")
                    if pick:
                        side, p_win, ask, ev = pick
                        scored.append((ev, b, side, p_win, ask))

                scored.sort(key=lambda s: s[0], reverse=True)
                for ev, b, side, p_win, ask in scored:
                    if len(positions) >= MAX_OPEN_POSITIONS:
                        break
                    if city_day_count(city, date) >= MAX_ENTRIES_PER_CITY_DAY:
                        break
                    stake = kelly_stake(p_win, ask)
                    if stake < MIN_TRADE_USDC:
                        log.info("%s: kelly stake $%.2f < min -> skip", b.title[:50], stake)
                        continue
                    tid = b.yes_token_id if side == "Yes" else b.no_token_id

                    # REAL-FILL GATE: verify against the live order book.
                    # (a) enough sellers within MAX_SLIPPAGE of best ask,
                    # (b) recompute EV at the VWAP we'd actually pay + taker fee;
                    # if the honest number no longer clears MIN_EDGE, no trade.
                    fill = book_fill(tid, round(stake, 2))
                    if fill is None:
                        log.info("%s %s: book too thin near best ask -> skip", b.title[:50], side)
                        continue
                    vwap, book_shares = fill
                    eff = round(vwap + taker_fee_per_share(vwap, b.fee_rate, b.fee_exp), 4)   # true cost/share
                    if eff > MAX_PRICE:
                        log.info("%s %s: real cost %.3f above MAX_PRICE -> skip",
                                 b.title[:50], side, eff)
                        continue
                    ev_eff = p_win / eff - 1.0
                    if ev_eff < MIN_EDGE:
                        log.info("%s %s: EV %+.0f%% -> %+.0f%% after real book+fee -> skip",
                                 b.title[:50], side, ev * 100, ev_eff * 100)
                        continue

                    bought = place_buy(tid, vwap, round(stake, 2))
                    if not bought:
                        continue
                    shares = round(min(bought, book_shares), 2)   # never more than the book held
                    last_entry[tid] = time.time()
                    positions[tid] = {
                        "title": b.title, "end_ts": b.end_ts, "side": side,
                        "token_id": tid, "market_id": b.market_id,
                        "entry_rate": eff, "size": shares,
                        "city": city, "date": date,
                        "station": b.station, "lead": fc.lead,
                    }
                    entries.append({"title": b.title, "side": side, "entry_rate": eff,
                                    "size": shares, "time": time.time(),
                                    "ev": ev_eff, "p_win": p_win})
                    save()
                    obs = fc.obs_max if kind == "highest" else fc.obs_min
                    sigma = fc.sigma_max if kind == "highest" else fc.sigma_min
                    msg = (f"ENTRY buy {side}\n{b.title}\n"
                           f"model p={p_win:.2f} | book vwap={vwap:.3f} +fee -> cost={eff:.3f} "
                           f"| EV={ev_eff:+.0%}\n"
                           f"stake=${stake:.2f} ({shares:.2f} shares) | "
                           f"station={b.station or 'none'} lead={fc.lead} "
                           f"reported={'n/a' if obs is None else f'{obs:.1f}°C'} | "
                           f"members={fc.n_members} models={fc.n_det_models} "
                           f"kernel={sigma:.1f}°C | DRY_RUN={DRY_RUN}")
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
