"""
Viral Meme Coin Tracker v2 — GitHub Actions Edition
Runs once per trigger. GitHub Actions cron calls it on schedule.
"""

import requests
import json
import time
import os
import sys
import html
import logging
from datetime import datetime, timezone
from urllib.parse import quote as url_quote

# ============================================================================
# CONFIGURATION
# ============================================================================

CONFIG = {
    "subreddits": ["cryptocurrency", "cryptomoonshots", "memecoins"],
    "posts_per_subreddit": 10,
    "min_upvotes": 1500,
    "min_comments": 200,
    "max_token_age_hours": 48,
    "min_liquidity_usd": 100,
    "min_volume_24h": 5000,
    "min_transactions_24h": 20,
    "min_safety_score": 1,
    "dex_pairs_to_scan": 10,       # Check top N results, not just the first
    "claude_model": "claude-haiku-4-5-20251001",
    "dedup_file": "alerted_tokens.json",  # Stored as GitHub Actions artifact
}

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tracker")


# ============================================================================
# HELPERS
# ============================================================================

def safe_num(val):
    """Convert a value to float, handling None/null/strings."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def http_get(url, headers=None, retries=1):
    """GET with retry on transient failures."""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers or {}, timeout=15)
            return resp
        except requests.exceptions.RequestException as e:
            if attempt < retries:
                log.warning(f"  Retry {attempt+1} for GET {url[:60]}...")
                time.sleep(3)
            else:
                raise e
    return None


def http_post(url, headers=None, json_data=None, retries=1):
    """POST with retry on transient failures."""
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=json_data, timeout=30)
            return resp
        except requests.exceptions.RequestException as e:
            if attempt < retries:
                log.warning(f"  Retry {attempt+1} for POST {url[:60]}...")
                time.sleep(3)
            else:
                raise e
    return None


# ============================================================================
# DEDUPLICATION
# ============================================================================

def load_alerted_tokens():
    """Load previously alerted tokens from file (persisted via GitHub Actions cache)."""
    path = CONFIG["dedup_file"]
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        # Clean out entries older than 72 hours
        now = time.time()
        return {k: v for k, v in data.items() if now - v < 72 * 3600}
    except Exception:
        return {}


def save_alerted_tokens(tokens):
    """Save alerted tokens to file."""
    with open(CONFIG["dedup_file"], "w") as f:
        json.dump(tokens, f)


def already_alerted(pair_address):
    """Check if we've already sent an alert for this pair."""
    tokens = load_alerted_tokens()
    return pair_address in tokens


def mark_alerted(pair_address):
    """Record that we've alerted on this pair."""
    tokens = load_alerted_tokens()
    tokens[pair_address] = time.time()
    save_alerted_tokens(tokens)


# ============================================================================
# CORE FUNCTIONS
# ============================================================================

def fetch_reddit_posts(subreddit, limit=10):
    """Fetch top daily posts from a subreddit."""
    url = f"https://www.reddit.com/r/{subreddit}/top/.json?t=day&limit={limit}"
    headers = {"User-Agent": "ViralCoinTracker/2.1 (GitHub Actions; educational)"}
    try:
        resp = http_get(url, headers=headers, retries=1)
        if resp.status_code == 429:
            log.warning(f"  Rate limited on r/{subreddit}. Waiting 60s...")
            time.sleep(60)
            resp = http_get(url, headers=headers)
        if resp.status_code != 200:
            log.error(f"  r/{subreddit} returned {resp.status_code}")
            return []
        try:
            data = resp.json()
        except ValueError:
            log.error(f"  r/{subreddit} returned non-JSON response")
            return []
        posts = []
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            posts.append({
                "title": d.get("title", ""),
                "score": d.get("score", 0) or 0,
                "num_comments": d.get("num_comments", 0) or 0,
                "subreddit": d.get("subreddit", subreddit),
            })
        return posts
    except Exception as e:
        log.error(f"  Reddit error r/{subreddit}: {e}")
        return []


def filter_viral(posts):
    """Keep only posts meeting viral thresholds."""
    return [
        p for p in posts
        if p["score"] >= CONFIG["min_upvotes"]
        or p["num_comments"] >= CONFIG["min_comments"]
    ]


def extract_trending_names(posts):
    """Ask Claude to extract trending names/tickers from viral posts."""
    posts_text = "\n".join(
        f"{p['title']} ({p['score']} upvotes, {p['num_comments']} comments) | r/{p['subreddit']}"
        for p in posts
    )
    prompt = (
        "You analyze viral Reddit crypto posts. Extract ONLY names, memes, projects, "
        "or tickers that are EXPLICITLY mentioned or clearly referenced in the posts. "
        "Do NOT invent or guess tickers that aren't in the posts.\n\n"
        "For each, include the most likely ticker symbol and a short reason.\n\n"
        "Respond ONLY with a JSON array. No markdown, no backticks, no explanation.\n"
        "Example: "
        '[{"name":"ghibli","ticker":"GHIBLI","reason":"Studio Ghibli memes trending"}]\n\n'
        "If nothing is clearly extractable, respond with exactly: []\n\n"
        f"Posts:\n{posts_text}"
    )
    try:
        resp = http_post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json_data={
                "model": CONFIG["claude_model"],
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            retries=1,
        )
        if resp.status_code != 200:
            log.error(f"  Claude {resp.status_code}: {resp.text[:200]}")
            return []
        text = resp.json()["content"][0]["text"].strip()
        # Strip markdown code fences if Claude wraps the response
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        if not isinstance(result, list):
            return []
        # Validate each item has at least a name or ticker
        valid = []
        for item in result:
            if isinstance(item, dict) and (item.get("ticker") or item.get("name")):
                valid.append(item)
        return valid
    except json.JSONDecodeError:
        log.error(f"  Claude returned non-JSON response")
        return []
    except Exception as e:
        log.error(f"  Claude error: {e}")
        return []


def search_dexscreener(query):
    """Search DexScreener and return the first pair that passes our age filter.

    BUG FIX: pairs[0] is usually the ESTABLISHED token (e.g. official $TRUMP).
    We scan the first N pairs and return the first one young enough to be a
    new meme token created in response to a viral trend.
    """
    encoded_query = url_quote(query.strip())
    try:
        resp = http_get(
            f"https://api.dexscreener.com/latest/dex/search?q={encoded_query}",
            retries=1,
        )
        if resp.status_code != 200:
            return None
        pairs = resp.json().get("pairs") or []  # Handle null
        if not pairs:
            return None

        now_ms = int(time.time() * 1000)
        max_age_ms = CONFIG["max_token_age_hours"] * 3600 * 1000
        limit = min(CONFIG["dex_pairs_to_scan"], len(pairs))

        for i in range(limit):
            pair = pairs[i]
            created = pair.get("pairCreatedAt", 0) or 0
            if created > 0 and (now_ms - created) <= max_age_ms:
                return pair  # Found a young pair

        # None of the top results are new enough
        return None
    except Exception as e:
        log.error(f"  DexScreener error '{query}': {e}")
        return None


def passes_quality_filters(pair):
    """Check if a pair passes liquidity, volume, and transaction filters."""
    liq = safe_num(pair.get("liquidity", {}).get("usd"))
    if liq < CONFIG["min_liquidity_usd"]:
        return False, f"liquidity ${liq:,.0f}"

    vol = safe_num(pair.get("volume", {}).get("h24"))
    if vol < CONFIG["min_volume_24h"]:
        return False, f"volume ${vol:,.0f}"

    txns = pair.get("txns", {}).get("h24", {})
    total = safe_num(txns.get("buys")) + safe_num(txns.get("sells"))
    if total < CONFIG["min_transactions_24h"]:
        return False, f"txns {int(total)}"

    return True, "passed"


def safety_score(pair):
    """Calculate 0-10 safety score from on-chain metrics."""
    s = 0
    liq = safe_num(pair.get("liquidity", {}).get("usd"))
    vol = safe_num(pair.get("volume", {}).get("h24"))
    txns = pair.get("txns", {}).get("h24", {})
    total_tx = safe_num(txns.get("buys")) + safe_num(txns.get("sells"))
    pc = safe_num(pair.get("priceChange", {}).get("h6"))

    s += 3 if liq > 50000 else 2 if liq > 20000 else 1 if liq > 10000 else 0
    s += 3 if vol > 100000 else 2 if vol > 25000 else 1 if vol > 5000 else 0
    s += 2 if total_tx > 500 else 1 if total_tx > 100 else 0
    s += 2 if 10 < pc < 300 else 0
    return s


def age_hours(pair):
    """Calculate pair age in hours."""
    created = pair.get("pairCreatedAt", 0) or 0
    if not created:
        return "?"
    return f"{(int(time.time() * 1000) - created) / 3600000:.1f}"


def send_telegram(item, pair, score):
    """Send formatted alert to Telegram with HTML-escaped dynamic values."""
    txns = pair.get("txns", {}).get("h24", {})
    buys = int(safe_num(txns.get("buys")))
    sells = int(safe_num(txns.get("sells")))
    chain = html.escape(str(pair.get("chainId", "?")))
    addr = pair.get("pairAddress", "")
    link = f"https://dexscreener.com/{pair.get('chainId', '')}/{addr}"
    symbol = html.escape(str(pair.get("baseToken", {}).get("symbol", "?")))
    name = html.escape(str(pair.get("baseToken", {}).get("name", "?")))
    reason = html.escape(str(item.get("reason", "N/A")))
    liq = safe_num(pair.get("liquidity", {}).get("usd"))
    vol = safe_num(pair.get("volume", {}).get("h24"))
    pc = safe_num(pair.get("priceChange", {}).get("h6"))
    fdv = safe_num(pair.get("fdv"))

    msg = (
        f"🔥 <b>VIRAL TOKEN ALERT</b>\n\n"
        f"📌 <b>Trending:</b> {reason}\n"
        f"🪙 <b>Token:</b> {symbol} ({name})\n"
        f"⛓ <b>Chain:</b> {chain}\n"
        f"⏰ <b>Age:</b> {age_hours(pair)} hours\n\n"
        f"📊 <b>Safety Score: {score}/10</b>\n"
        f"💰 Liquidity: ${liq:,.0f}\n"
        f"📈 Volume 24h: ${vol:,.0f}\n"
        f"🔄 Buys: {buys:,} | Sells: {sells:,} (24h)\n"
        f"📈 6h Change: {pc}%\n"
        f"💎 FDV: ${fdv:,.0f}\n\n"
        f'🔗 <a href="{link}">View on DexScreener</a>\n\n'
        f"⚠️ DYOR — Not financial advice"
    )
    try:
        r = http_post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json_data={
                "chat_id": TELEGRAM_CHAT_ID,
                "parse_mode": "HTML",
                "text": msg,
            },
            retries=1,
        )
        if r and r.status_code == 200:
            log.info(f"  ✅ Telegram sent: {symbol}")
        else:
            status = r.status_code if r else "no response"
            body = r.text[:100] if r else ""
            log.error(f"  ❌ Telegram {status}: {body}")
    except Exception as e:
        log.error(f"  ❌ Telegram failed: {e}")


def log_to_sheets(item, pair, score):
    """Send data to Google Sheet via Apps Script webhook."""
    if not GOOGLE_SHEET_WEBHOOK:
        return
    chain = pair.get("chainId", "")
    addr = pair.get("pairAddress", "")
    row = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "topic": item.get("reason", ""),
        "symbol": pair.get("baseToken", {}).get("symbol", ""),
        "name": pair.get("baseToken", {}).get("name", ""),
        "chain": chain,
        "liquidity": safe_num(pair.get("liquidity", {}).get("usd")),
        "volume": safe_num(pair.get("volume", {}).get("h24")),
        "priceChange": safe_num(pair.get("priceChange", {}).get("h6")),
        "score": score,
        "link": f"https://dexscreener.com/{chain}/{addr}",
    }
    try:
        r = requests.post(GOOGLE_SHEET_WEBHOOK, json=row, timeout=15)
        if r.status_code < 400:  # 200 or 302 redirect — both mean success
            log.info("  ✅ Logged to Google Sheets")
        else:
            log.warning(f"  Sheets returned {r.status_code}")
    except Exception as e:
        log.error(f"  Sheets error: {e}")


# ============================================================================
# MAIN
# ============================================================================

def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log.info(f"{'='*55}")
    log.info(f"🚀 SCAN STARTED — {ts}")
    log.info(f"{'='*55}")

    if not CLAUDE_API_KEY:
        log.error("❌ CLAUDE_API_KEY not set. Add it in GitHub Secrets.")
        sys.exit(1)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("❌ Telegram secrets not set. Add them in GitHub Secrets.")
        sys.exit(1)

    # --- Phase 1: Reddit ---
    log.info("📱 Phase 1: Fetching Reddit...")
    all_posts = []
    for sub in CONFIG["subreddits"]:
        posts = fetch_reddit_posts(sub, CONFIG["posts_per_subreddit"])
        log.info(f"  r/{sub}: {len(posts)} posts")
        all_posts.extend(posts)
        time.sleep(2)

    if not all_posts:
        log.warning("No posts fetched. Done.")
        return

    viral = filter_viral(all_posts)
    log.info(f"  → {len(viral)} viral out of {len(all_posts)}")
    if not viral:
        log.info("No viral posts this cycle. Done.")
        return

    # --- Phase 2: Claude ---
    log.info("🤖 Phase 2: Claude AI extraction...")
    names = extract_trending_names(viral)
    log.info(f"  → {len(names)} trending names:")
    for n in names:
        log.info(f"    • {n.get('ticker','?')} — {n.get('reason','')}")
    if not names:
        log.info("Nothing extracted. Done.")
        return

    # --- Phase 3: DexScreener ---
    log.info("🔍 Phase 3: DexScreener search...")
    alerts = 0
    for item in names:
        ticker = item.get("ticker") or item.get("name") or ""
        ticker = ticker.strip()
        if not ticker or len(ticker) < 2:
            continue

        log.info(f"  Searching: {ticker}")
        pair = search_dexscreener(ticker)
        time.sleep(0.5)

        if not pair:
            log.info(f"    → No new token found")
            continue

        sym = pair.get("baseToken", {}).get("symbol", "?")
        chain_id = pair.get("chainId", "?")
        pair_addr = pair.get("pairAddress", "")
        log.info(f"    → Found: {sym} on {chain_id} (age: {age_hours(pair)}h)")

        # Quality filters
        ok, reason = passes_quality_filters(pair)
        if not ok:
            log.info(f"    → Filtered: {reason}")
            continue

        # Safety score
        sc = safety_score(pair)
        log.info(f"    → Score: {sc}/10")
        if sc < CONFIG["min_safety_score"]:
            log.info(f"    → Too low ({sc} < {CONFIG['min_safety_score']})")
            continue

        # Deduplication
        if already_alerted(pair_addr):
            log.info(f"    → Already alerted (skipping duplicate)")
            continue

        # --- Phase 4: Alert ---
        log.info(f"    🔥 ALERT: {sym}")
        send_telegram(item, pair, sc)
        log_to_sheets(item, pair, sc)
        mark_alerted(pair_addr)
        alerts += 1

    log.info(f"{'='*55}")
    log.info(f"✅ DONE — {alerts} alert(s) sent")
    log.info(f"{'='*55}")


if __name__ == "__main__":
    main()
