"""
geckoterminal.py — Lightweight wrapper for the GeckoTerminal Public API.

Used as an enrichment layer alongside the existing DexScreener boost feed.
DexScreener catches paid-promotion tokens; GeckoTerminal adds:
  - Real OHLCV history (replaces manual outcome tracker price fetches)
  - Better tx data (unique buyers/sellers, not just total tx count)
  - Trending and new pool feeds (catches non-boosted momentum)

API docs: https://api.geckoterminal.com/docs/index.html
Rate limit: 30 calls/min on free tier (no auth required).
Beta API — version is pinned to v2 in URLs to avoid surprises.
"""

import time
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://api.geckoterminal.com/api/v2"

# Map our internal chain IDs (matching DexScreener's chainId field) to
# GeckoTerminal network IDs. Add chains as needed.
CHAIN_MAP = {
    "solana":   "solana",
    "ethereum": "eth",
    "eth":      "eth",
    "bsc":      "bsc",
    "base":     "base",
    "arbitrum": "arbitrum",
    "polygon":  "polygon_pos",
}

# Rate limit guard: free tier is 30/min, but on shared GitHub Actions IPs
# the effective limit is much lower. 4.0s = ~15 calls/min, conservative.
# If you still hit 429s, raise to 6.0.
MIN_INTERVAL_SECONDS = 4.0

# Conservative default timeout. GeckoTerminal occasionally hangs.
HTTP_TIMEOUT = 15

# Headers — Accept JSON and identify ourselves politely.
HEADERS = {
    "Accept": "application/json;version=20230302",
    "User-Agent": "meme-scanner-v3/1.0 (+https://github.com)",
}


# ---------------------------------------------------------------------------
# Internal request helpers
# ---------------------------------------------------------------------------

_last_call_ts: float = 0.0


def _throttle() -> None:
    """Sleep just enough to stay under the 30/min ceiling."""
    global _last_call_ts
    elapsed = time.time() - _last_call_ts
    if elapsed < MIN_INTERVAL_SECONDS:
        time.sleep(MIN_INTERVAL_SECONDS - elapsed)
    _last_call_ts = time.time()


def _get(path: str, params: Optional[dict] = None, retries: int = 3) -> Optional[dict]:
    """Throttled GET with exponential backoff. Returns parsed JSON or None on failure."""
    url = f"{BASE_URL}{path}"
    for attempt in range(retries):
        _throttle()
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=HTTP_TIMEOUT)
            if resp.status_code == 429:
                # Hit rate limit — don't retry, just skip this token.
                # Retrying on shared IPs makes the problem worse, not better.
                # The token just gets gt_enriched=False and the scanner moves on.
                log.warning("GeckoTerminal 429 — skipping enrichment (rate limited)")
                return None
            if resp.status_code == 404:
                # Pool/token genuinely doesn't exist on GT — don't retry
                log.debug("GeckoTerminal 404: %s", url)
                return None
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("GeckoTerminal error attempt %d for %s: %s", attempt + 1, url, e)
            time.sleep(2 ** attempt)
    return None


def _normalize_chain(chain: str) -> Optional[str]:
    """Convert internal chain ID to GeckoTerminal network slug."""
    if not chain:
        return None
    return CHAIN_MAP.get(chain.lower().strip())


# ---------------------------------------------------------------------------
# Public API — pool data
# ---------------------------------------------------------------------------

def get_pool(chain: str, pool_address: str) -> Optional[dict]:
    """
    Fetch detailed pool data including price, liquidity, volume, tx counts,
    and unique buyer/seller counts.

    Returns the 'attributes' sub-dict of the pool object, or None.

    Useful fields in the response:
      - reserve_in_usd          (liquidity)
      - volume_usd.h1/h6/h24    (volume by window)
      - price_change_percentage.m5/h1/h6/h24
      - transactions.h1.buys / sells / buyers / sellers
      - base_token_price_usd
      - fdv_usd
    """
    network = _normalize_chain(chain)
    if not network:
        log.debug("Unsupported chain: %s", chain)
        return None
    data = _get(f"/networks/{network}/pools/{pool_address}")
    if not data or "data" not in data:
        return None
    return data["data"].get("attributes")


def get_ohlcv(
    chain: str,
    pool_address: str,
    timeframe: str = "hour",
    aggregate: int = 1,
    limit: int = 24,
    before_timestamp: Optional[int] = None,
) -> Optional[list]:
    """
    Fetch OHLCV candles for a pool.

    Args:
        timeframe: 'day' | 'hour' | 'minute'
        aggregate: aggregation factor
            day:    1
            hour:   1, 4, 12
            minute: 1, 5, 15
        limit:     max candles (1-1000, default 100 in API)
        before_timestamp: unix seconds — fetch candles before this time

    Returns a list of [timestamp, open, high, low, close, volume_usd] arrays,
    or None on error.

    Replaces manual outcome tracking — instead of sampling price at 1h/6h/24h/7d,
    you can compute any windowed return from full candle history.
    """
    network = _normalize_chain(chain)
    if not network:
        return None
    if timeframe not in ("day", "hour", "minute"):
        log.error("Invalid timeframe: %s", timeframe)
        return None

    params = {"aggregate": aggregate, "limit": limit, "currency": "usd"}
    if before_timestamp:
        params["before_timestamp"] = before_timestamp

    data = _get(
        f"/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}",
        params=params,
    )
    if not data:
        return None
    try:
        return data["data"]["attributes"]["ohlcv_list"]
    except (KeyError, TypeError):
        return None


def get_pool_info(chain: str, pool_address: str) -> Optional[dict]:
    """
    Fetch metadata about a pool's tokens — name, symbol, image_url,
    description, social links. Used to enrich Telegram alerts.
    """
    network = _normalize_chain(chain)
    if not network:
        return None
    data = _get(f"/networks/{network}/pools/{pool_address}/info")
    if not data:
        return None
    return data.get("data")


# ---------------------------------------------------------------------------
# Public API — discovery feeds
# ---------------------------------------------------------------------------

def get_trending_pools(chain: str, page: int = 1) -> list:
    """
    Get trending pools for a network. Trending is computed from web visits
    + onchain activity, so it's a complementary signal to DexScreener boosts.
    Returns list of pool dicts (each with 'id' and 'attributes').
    """
    network = _normalize_chain(chain)
    if not network:
        return []
    data = _get(f"/networks/{network}/trending_pools", params={"page": page})
    if not data:
        return []
    return data.get("data", [])


def get_new_pools(chain: str, page: int = 1) -> list:
    """
    Get the 20 newest pools on a network. This catches tokens BEFORE they
    get boosted on DexScreener — earliest possible signal.
    """
    network = _normalize_chain(chain)
    if not network:
        return []
    data = _get(f"/networks/{network}/new_pools", params={"page": page})
    if not data:
        return []
    return data.get("data", [])


# ---------------------------------------------------------------------------
# Convenience helpers — domain-specific
# ---------------------------------------------------------------------------

def compute_buyer_seller_ratio(pool_attrs: dict, window: str = "h1") -> Optional[float]:
    """
    Compute unique-buyer / unique-seller ratio from a pool's tx data.
    Higher = more accumulation. < 0.5 is heavy distribution (warning signal).
    Returns None if data is missing.
    """
    try:
        txs = pool_attrs["transactions"][window]
        buyers = int(txs.get("buyers", 0) or 0)
        sellers = int(txs.get("sellers", 0) or 0)
        if sellers == 0:
            return None if buyers == 0 else 999.0
        return round(buyers / sellers, 3)
    except (KeyError, TypeError, ValueError):
        return None


def compute_return_since(
    chain: str,
    pool_address: str,
    hours_ago: int,
) -> Optional[float]:
    """
    Compute % price change from N hours ago to now using OHLCV.
    Replaces manual snapshot-based outcome tracking.

    Returns: percentage change as float (e.g. 23.5 for +23.5%), or None.
    """
    # Fetch enough hourly candles to cover the window with a small buffer
    candles = get_ohlcv(
        chain=chain,
        pool_address=pool_address,
        timeframe="hour",
        aggregate=1,
        limit=min(hours_ago + 2, 1000),
    )
    if not candles or len(candles) < 2:
        return None

    # OHLCV format: [timestamp, open, high, low, close, volume]
    # Newest candle is first in the list.
    try:
        latest_close = float(candles[0][4])
        # Find candle closest to N hours ago
        idx = min(hours_ago, len(candles) - 1)
        old_close = float(candles[idx][4])
        if old_close == 0:
            return None
        return round((latest_close - old_close) / old_close * 100, 2)
    except (IndexError, ValueError, TypeError):
        return None


def enrich_token(token: dict) -> dict:
    """
    Drop-in enrichment for a token dict from the existing scanner.

    Expects the input dict to contain at minimum:
      - chain  (e.g. 'solana')
      - pair_address  (the pool/pair address from DexScreener)

    Adds these keys (all optional — None if data unavailable):
      - gt_liquidity_usd
      - gt_volume_h1, gt_volume_h6, gt_volume_h24
      - gt_buyers_h1, gt_sellers_h1, gt_buyer_seller_ratio_h1
      - gt_price_change_h1, gt_price_change_h6, gt_price_change_h24
      - gt_fdv_usd
      - gt_pool_created_at
      - gt_enriched (bool — True if any data added)

    Never raises. On failure, returns the input dict unchanged with
    gt_enriched = False.
    """
    chain = token.get("chain") or token.get("chainId")
    pool_address = token.get("pair_address") or token.get("pairAddress")

    if not chain or not pool_address:
        token["gt_enriched"] = False
        return token

    attrs = get_pool(chain, pool_address)
    if not attrs:
        token["gt_enriched"] = False
        return token

    # Liquidity
    try:
        token["gt_liquidity_usd"] = float(attrs.get("reserve_in_usd") or 0) or None
    except (ValueError, TypeError):
        token["gt_liquidity_usd"] = None

    # Volume by window
    vol = attrs.get("volume_usd") or {}
    for window in ("h1", "h6", "h24"):
        try:
            token[f"gt_volume_{window}"] = float(vol.get(window) or 0) or None
        except (ValueError, TypeError):
            token[f"gt_volume_{window}"] = None

    # Price change by window
    pcp = attrs.get("price_change_percentage") or {}
    for window in ("h1", "h6", "h24"):
        try:
            token[f"gt_price_change_{window}"] = float(pcp.get(window) or 0)
        except (ValueError, TypeError):
            token[f"gt_price_change_{window}"] = None

    # Tx data — unique buyers/sellers in the last hour
    txs_h1 = (attrs.get("transactions") or {}).get("h1") or {}
    try:
        token["gt_buyers_h1"] = int(txs_h1.get("buyers") or 0) or None
        token["gt_sellers_h1"] = int(txs_h1.get("sellers") or 0) or None
    except (ValueError, TypeError):
        token["gt_buyers_h1"] = None
        token["gt_sellers_h1"] = None

    token["gt_buyer_seller_ratio_h1"] = compute_buyer_seller_ratio(attrs, "h1")

    # FDV and creation timestamp
    try:
        token["gt_fdv_usd"] = float(attrs.get("fdv_usd") or 0) or None
    except (ValueError, TypeError):
        token["gt_fdv_usd"] = None

    token["gt_pool_created_at"] = attrs.get("pool_created_at")

    token["gt_enriched"] = True
    return token


# ---------------------------------------------------------------------------
# Smoke test — run this file directly to verify connectivity
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    print("=" * 60)
    print("GeckoTerminal connectivity smoke test")
    print("=" * 60)

    # Test 1: Trending pools on Solana
    print("\n[1/3] Fetching trending Solana pools...")
    trending = get_trending_pools("solana")
    print(f"  Got {len(trending)} trending pools")
    if trending:
        first = trending[0]["attributes"]
        print(f"  Top: {first.get('name')} — liq ${float(first.get('reserve_in_usd', 0)):,.0f}")

    # Test 2: Enrich a known token (WIF on Solana)
    print("\n[2/3] Enriching WIF/SOL pool...")
    fake_token = {
        "chain": "solana",
        "pair_address": "EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx",
    }
    enriched = enrich_token(fake_token)
    if enriched.get("gt_enriched"):
        print(f"  Liquidity: ${enriched.get('gt_liquidity_usd', 0):,.0f}")
        print(f"  Volume 24h: ${enriched.get('gt_volume_h24', 0):,.0f}")
        print(f"  Price change 24h: {enriched.get('gt_price_change_h24')}%")
        print(f"  Buyer/seller ratio (1h): {enriched.get('gt_buyer_seller_ratio_h1')}")
    else:
        print("  Enrichment failed — pool may not exist or API down")

    # Test 3: OHLCV
    print("\n[3/3] Fetching last 6 hours of OHLCV...")
    candles = get_ohlcv("solana", "EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx",
                        timeframe="hour", limit=6)
    if candles:
        print(f"  Got {len(candles)} candles")
        for c in candles[:3]:
            print(f"    ts={c[0]}  close=${c[4]}  vol=${c[5]:,.0f}")
    else:
        print("  OHLCV fetch failed")

    print("\nSmoke test complete.")
